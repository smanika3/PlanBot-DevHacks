import asyncio
import logging
import os

import discord

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
        p = await asyncio.to_thread(plan.listen, m.content)
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
