import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import tasks

import db
import engine
import llm
import plan
import ui_polls
import venue


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            k, _, v = line.strip().partition("=")
            if k and not k.startswith("#"):
                os.environ.setdefault(k, v)


def should_ignore(author_is_bot: bool, guild_id, channel_id, allowed_guild, allowed_channel) -> bool:
    """Ignore bots (including ourselves), DMs, and anything outside the test guild/channel."""
    return author_is_bot or guild_id != allowed_guild or channel_id != allowed_channel


HELP = """**PlanBot** turns "wanna golf this weekend, then dinner?" into a group plan.
• Say what you'd like to do in this channel and I'll post a plan card. Tap **I'm in** per activity.
• Tell me your availability, budget, area, or what you can't do, in chat or by DM.
• Diet or allergies: DM me. The group only ever sees anonymous needs, never who has them.
• Vote with the buttons under the card: tap every option you could live with. An activity locks once everyone in it has voted.
• `/reschedule` asks the group to move an activity · `/status` links the current plan card · `/edit` changes the activities and `/cancel` cancels (organizer only) · `/help` shows this."""


async def ensure_pinned(msg: discord.Message):
    """Re-pin the card if someone unpinned it. Needs the Pin Messages permission."""
    if not msg.pinned:
        try:
            await msg.pin()
        except discord.HTTPException:
            logging.warning("could not re-pin card")


class EditModal(discord.ui.Modal, title="Edit plan"):
    kinds = discord.ui.TextInput(label="Activities in order, comma-separated (max 4)", max_length=80)

    def __init__(self, client, ev, current: str):
        super().__init__()
        self.client, self.ev = client, ev
        self.kinds.default = current

    async def on_submit(self, i: discord.Interaction):
        parsed = plan.parse_kinds(self.kinds.value)
        if i.user.id != self.ev["proposer_id"] or db.open_event(self.ev["channel_id"]) is None:
            await i.response.send_message("This plan can no longer be edited by you.", ephemeral=True)
        elif not parsed:
            await i.response.send_message("Use short activity names separated by commas, e.g. `golf, dinner`.",
                                          ephemeral=True)
        else:
            db.set_segments(self.ev["id"], parsed, i.user.id)
            await i.response.send_message(f"Plan updated: {' → '.join(parsed)}", ephemeral=True)
            await self.client.refresh_card(self.ev["id"])
            self.client.schedule_recompute(self.ev["id"])


def card_view(snap: dict) -> discord.ui.View:
    """Row 0: I'm in per activity. Then one row per open activity with up to 3 vote buttons (max 4 activities
    = 5 rows, 16 components, inside Discord's 5 rows / 25 components)."""
    v = discord.ui.View(timeout=None)
    for s in snap["segments"]:
        v.add_item(SegButton(s["id"], s["kind"]))
    row = 0
    for s in snap["segments"]:
        if s["locked"] or not s["options"] or row >= 4:
            continue
        row += 1
        tb = s["tiebreak"]
        for j, o in enumerate(s["options"][:3]):
            if tb and o["key"] not in tb["keys"]:
                continue
            cls = PickButton if tb and tb["round"] == 2 else VoteButton
            v.add_item(cls(s["id"], j, db.key_hash(o["key"]), s["kind"], row))
    return v


async def _redraw(i: discord.Interaction, eid: int):
    snap = db.snapshot(eid)
    client = i.client
    await i.response.edit_message(embed=plan.render(snap, client.card_status.get(eid)), view=card_view(snap))
    await ensure_pinned(i.message)


async def _stale(i: discord.Interaction, eid: int):
    await i.response.send_message("Options changed, please vote again.", ephemeral=True)
    await i.client.refresh_card(eid)


def _current(sid, ord_, h):
    """The option at this ord if it is still the one the button was made for, else None."""
    opts = db.options(sid)
    return opts[ord_] if ord_ < len(opts) and db.key_hash(db.option_key(opts[ord_])) == h else None


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vote:(?P<sid>\d+):(?P<ord>\d+):(?P<h>\w+)"):
    """Approval toggle for one option. The hash pins the button to the option it was drawn for."""
    label_fmt = "{kind} {letter}"

    def __init__(self, sid: int, ord_: int, h: str, kind: str = "", row: int = None):
        super().__init__(discord.ui.Button(label=self.label_fmt.format(kind=kind.title(), letter="ABC"[ord_]).strip(),
                                           custom_id=f"{self.prefix}:{sid}:{ord_}:{h}", row=row))
        self.sid, self.ord, self.h = sid, ord_, h

    prefix = "vote"

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sid"]), int(match["ord"]), match["h"])

    async def callback(self, i: discord.Interaction):
        eid = db.event_of_segment(self.sid)
        snap = db.snapshot(eid)
        seg = next(s for s in snap["segments"] if s["id"] == self.sid)
        if snap["status"] == "cancelled":
            await i.response.send_message("This plan was cancelled.", ephemeral=True)
        elif seg["locked"]:
            await _redraw(i, eid)  # stale card: show the lock
        elif (o := _current(self.sid, self.ord, self.h)) is None or (
                seg["tiebreak"] and db.option_key(o) not in seg["tiebreak"]["keys"]):
            await _stale(i, eid)
        elif i.user.id not in db.segment_participants(self.sid) and not (
                self.prefix == "pick" and i.user.id == snap["proposer_id"]):  # the organizer may break a tie from outside
            await i.response.send_message("You're not in this activity, tap I'm in first.", ephemeral=True)
        else:
            await self.act(i, eid, seg, db.option_key(o))

    async def act(self, i, eid, seg, key):
        db.vote_toggle(self.sid, key, i.user.id)
        db.settle(self.sid)
        await _redraw(i, eid)
        i.client.schedule_recompute(eid)  # a lock moves later activities


class PickButton(VoteButton, template=r"pick:(?P<sid>\d+):(?P<ord>\d+):(?P<h>\w+)"):
    """Organizer-only tie break."""
    label_fmt = "Pick {kind} {letter}"
    prefix = "pick"

    async def act(self, i, eid, seg, key):
        if i.user.id != db.snapshot(eid)["proposer_id"]:
            await i.response.send_message("Only the organizer can break this tie.", ephemeral=True)
            return
        db.pick_tie(self.sid, key)
        await _redraw(i, eid)
        i.client.schedule_recompute(eid)


class SegButton(discord.ui.DynamicItem[discord.ui.Button], template=r"seg:(?P<sid>\d+)"):
    """Per-segment "I'm in" toggle. Dynamic so buttons on old cards survive a restart."""

    def __init__(self, sid: int, kind: str):
        super().__init__(discord.ui.Button(label=f"I'm in: {kind}", custom_id=f"seg:{sid}", row=0))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sid"]), (item.label or "").removeprefix("I'm in: "))

    async def callback(self, interaction: discord.Interaction):
        if db.snapshot(db.event_of_segment(self.sid))["status"] == "cancelled":
            await interaction.response.send_message("This plan was cancelled.", ephemeral=True)
            return
        eid = db.toggle(self.sid, interaction.user.id, interaction.user.display_name)
        db.settle_event(eid)  # someone leaving can complete a vote; a locked segment never reopens here
        await _redraw(interaction, eid)
        interaction.client.schedule_recompute(eid)


class PlanBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        # no pings by default: names from chat can't trigger @everyone or user mentions; send sites opt in explicitly
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
        self.channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
        self.venues = venue.load()
        self.card_status = {}  # event id -> live per-role status lines shown on the card
        self.running, self.dirty = set(), set()

    def schedule_recompute(self, event_id: int):
        """Recompute options in the background. A change during a run triggers one more run, never a pile-up."""
        if event_id in self.running:
            self.dirty.add(event_id)
        else:
            self.running.add(event_id)
            asyncio.create_task(self._recompute(event_id))

    async def _recompute(self, event_id: int):
        try:
            while True:
                self.dirty.discard(event_id)
                if not engine.ready(db.snapshot(event_id)) or db.snapshot(event_id)["status"] == "cancelled":
                    break
                self.card_status[event_id] = {"Preferences": "refreshing…", "Schedule": "ranking slots…",
                                         "Venue": "checking opening hours…"}
                await self.refresh_card(event_id)
                try:
                    self.card_status[event_id] = await asyncio.to_thread(engine.run, event_id, engine.local_now(), self.venues)
                except Exception:
                    logging.exception("recompute failed")
                    self.card_status[event_id] = {"Venue": "check failed, will retry on the next change"}
                await self.refresh_card(event_id)
                if event_id not in self.dirty:
                    break
        finally:
            self.running.discard(event_id)
        # after the finally, so a recompute it triggers is not swallowed by the still-set `running` flag
        try:
            await ui_polls.on_recompute_done(self, event_id)
        except Exception:
            logging.exception("conflict detection failed")

    @tasks.loop(seconds=60)
    async def poll_sweeper(self):
        try:
            await ui_polls.sweep(self)  # closes polls past their deadline; deadlines live in the DB
        except Exception:
            logging.exception("poll sweep failed")

    async def setup_hook(self):
        db.init()
        self.add_dynamic_items(SegButton, VoteButton, PickButton)
        ui_polls.register(self)
        self.poll_sweeper.start()
        tree = app_commands.CommandTree(self)
        guild = discord.Object(id=self.guild_id)

        @tree.command(name="help", description="What PlanBot does and how to use it", guild=guild)
        async def help_(i: discord.Interaction):
            await i.response.send_message(HELP, ephemeral=True)

        @tree.command(name="status", description="Link to this channel's current plan card", guild=guild)
        async def status(i: discord.Interaction):
            ev = db.open_event(i.channel_id)
            if not ev:
                await i.response.send_message("No open plan in this channel.", ephemeral=True)
                return
            url = f"https://discord.com/channels/{i.guild_id}/{ev['channel_id']}/{ev['card_message_id']}"
            await i.response.send_message(f"Current plan: {url}", ephemeral=True)

        @tree.command(name="edit", description="Change the activities in the plan (organizer only)", guild=guild)
        async def edit(i: discord.Interaction):
            ev = db.open_event(i.channel_id)
            if not ev:
                await i.response.send_message("No open plan in this channel.", ephemeral=True)
            elif i.user.id != ev["proposer_id"]:
                await i.response.send_message(f"Only the organizer (<@{ev['proposer_id']}>) can edit this plan.",
                                              ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                current = ", ".join(s["kind"] for s in db.snapshot(ev["id"])["segments"])
                await i.response.send_modal(EditModal(self, ev, current))

        @tree.command(name="reschedule", description="Ask the group to move an activity", guild=guild)
        @app_commands.describe(activity="Which activity (only needed if you are in several)")
        async def reschedule(i: discord.Interaction, activity: str = None):
            await ui_polls.reschedule_command(self, i, activity)

        @tree.command(name="cancel", description="Cancel the current plan (organizer only)", guild=guild)
        async def cancel(i: discord.Interaction):
            ev = db.open_event(i.channel_id)
            if not ev:
                await i.response.send_message("No open plan in this channel.", ephemeral=True)
            elif i.user.id != ev["proposer_id"]:
                await i.response.send_message(f"Only the organizer (<@{ev['proposer_id']}>) can cancel this plan.",
                                              ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await i.response.send_message(f"Plan cancelled by <@{i.user.id}>.")
                await self.cancel_event(ev)

        await tree.sync(guild=guild)  # guild commands show up instantly

    async def on_ready(self):
        logging.info("online as %s", self.user)
        ev = db.open_event(self.channel_id)
        if ev:  # heal the card after downtime (re-pins it if someone unpinned it)
            await self.refresh_card(ev["id"])
            self.schedule_recompute(ev["id"])

    async def on_message(self, m: discord.Message):
        if m.guild is None:  # DM intake (diet etc.), only for people already in an open plan
            if not m.author.bot:
                ev = db.event_for_person(m.author.id)
                if ev:
                    await self.listen(m, ev["id"], private=True)
                else:
                    await m.reply("I'm not tracking a plan for you yet. Tap an \"I'm in\" button on the plan card first.")
            return
        if should_ignore(m.author.bot, m.guild.id, m.channel.id, self.guild_id, self.channel_id):
            return
        text = m.content.strip()
        ev = db.open_event(m.channel.id)  # ponytail: one open event per channel, enough for the demo
        if text == "!ping":
            await m.reply("pong")
        elif text == "!llm":
            await m.reply(await asyncio.to_thread(llm.complete, "Reply with the single word: pong", "", 10))
        elif ev:
            await self.listen(m, ev["id"])
        else:
            kinds = await asyncio.to_thread(plan.detect, text)
            if kinds:
                await self.kickoff(m, kinds)

    async def listen(self, m: discord.Message, event_id: int, private=False):
        p = await asyncio.to_thread(plan.listen, m.content, not private)
        if not p:
            return
        db.apply(event_id, m.author.id, m.author.display_name, p)
        db.settle_event(event_id)
        await self.refresh_card(event_id)
        self.schedule_recompute(event_id)
        pub, priv = plan.echo(p)
        if private:
            await m.reply((pub + "\n" + priv).strip())
            return
        if pub:
            await m.reply(pub, mention_author=False)
        if priv:  # diet never goes to the channel
            try:
                await m.author.send(f"Noted privately: {priv}")
            except discord.Forbidden:
                logging.info("DMs closed for a user; diet echo skipped")

    async def cancel_event(self, ev):
        db.cancel(ev["id"])
        e = plan.render(db.snapshot(ev["id"]))
        e.title = "Cancelled · " + e.title
        e.colour = discord.Colour.dark_grey()
        e.set_footer(text="Cancelled by the organizer")
        try:
            msg = await self.get_channel(ev["channel_id"]).fetch_message(ev["card_message_id"])
            await msg.edit(embed=e, view=None)
            await msg.unpin()
        except discord.HTTPException:
            logging.warning("could not update cancelled card")

    async def refresh_card(self, event_id: int):
        snap = db.snapshot(event_id)
        ev = db.con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        try:
            msg = await self.get_channel(ev["channel_id"]).fetch_message(ev["card_message_id"])
            await msg.edit(embed=plan.render(snap, self.card_status.get(event_id)), view=card_view(snap))
            await ensure_pinned(msg)
        except discord.HTTPException:
            logging.warning("could not refresh card")

    async def kickoff(self, m: discord.Message, kinds: list[str]):
        eid = db.create_event(m.channel.id, m.author.id, m.author.display_name, kinds)
        snap = db.snapshot(eid)
        card = await m.channel.send(embed=plan.render(snap), view=card_view(snap))
        db.set_card(eid, card.id)
        try:
            await card.pin()
        except discord.HTTPException:  # missing Pin Messages permission or pin limit
            logging.warning("could not pin card")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_env()
    PlanBot().run(os.environ["DISCORD_TOKEN"], log_handler=None)
