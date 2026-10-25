import asyncio
import logging
import os

import discord
from discord import app_commands

import db
import llm
import plan


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
• `/status` links the current plan card · `/cancel` cancels it (organizer only) · `/help` shows this."""


def card_view(snap: dict) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for s in snap["segments"]:
        v.add_item(SegButton(s["id"], s["kind"]))
    return v


class SegButton(discord.ui.DynamicItem[discord.ui.Button], template=r"seg:(?P<sid>\d+)"):
    """Per-segment "I'm in" toggle. Dynamic so buttons on old cards survive a restart."""

    def __init__(self, sid: int, kind: str):
        super().__init__(discord.ui.Button(label=f"I'm in: {kind}", custom_id=f"seg:{sid}"))
        self.sid = sid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sid"]), (item.label or "").removeprefix("I'm in: "))

    async def callback(self, interaction: discord.Interaction):
        if db.snapshot(db.event_of_segment(self.sid))["status"] == "cancelled":
            await interaction.response.send_message("This plan was cancelled.", ephemeral=True)
            return
        eid = db.toggle(self.sid, interaction.user.id, interaction.user.display_name)
        snap = db.snapshot(eid)
        await interaction.response.edit_message(embed=plan.render(snap), view=card_view(snap))


class PlanBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
        self.channel_id = int(os.environ["DISCORD_CHANNEL_ID"])

    async def setup_hook(self):
        db.init()
        self.add_dynamic_items(SegButton)
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
        await self.refresh_card(event_id)
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
            await msg.edit(embed=plan.render(snap), view=card_view(snap))
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
