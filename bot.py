import logging
import os

import discord


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            k, _, v = line.strip().partition("=")
            if k and not k.startswith("#"):
                os.environ.setdefault(k, v)


def should_ignore(author_is_bot: bool, guild_id, channel_id, allowed_guild, allowed_channel) -> bool:
    """Ignore bots (including ourselves), DMs, and anything outside the test guild/channel."""
    return author_is_bot or guild_id != allowed_guild or channel_id != allowed_channel


class PlanBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
        self.channel_id = int(os.environ["DISCORD_CHANNEL_ID"])

    async def on_ready(self):
        logging.info("online as %s", self.user)

    async def on_message(self, m: discord.Message):
        if should_ignore(m.author.bot, m.guild and m.guild.id, m.channel.id, self.guild_id, self.channel_id):
            return
        if m.content.strip() == "!ping":
            await m.reply("pong")
        elif m.content.strip() == "!llm":
            import llm
            await m.reply(llm.complete("Reply with the single word: pong", max_tokens=10))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_env()
    PlanBot().run(os.environ["DISCORD_TOKEN"], log_handler=None)
