
import os
import logging
from pathlib import Path

import discord
from discord.ext import commands

# -----------------------------
# CONFIGURATION
# -----------------------------

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from Railway variables.")

COG_FOLDER = Path("cogs")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

# -----------------------------
# LOGGING
# -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("OmanAirBot")

# -----------------------------
# BOT SETUP
# -----------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

class OmanAirBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):

        logger.info("Loading Oman Air modules...")

        if not COG_FOLDER.exists():
            logger.error("Cogs folder not found!")
            return

        for file in COG_FOLDER.glob("*.py"):

            if file.name.startswith("_"):
                continue

            extension = f"cogs.{file.stem}"

            try:
                await self.load_extension(extension)
                logger.info(f"Loaded: {extension}")

            except Exception:
                logger.exception(f"Failed to load: {extension}")

        # Remove stale global registrations, then sync only to the configured server.
        try:
            if not GUILD_ID:
                logger.error("GUILD_ID is missing. Set the Discord server ID in Railway variables.")
                return

            guild = discord.Object(id=GUILD_ID)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} server commands to guild {GUILD_ID}.")

        except Exception:
            logger.exception("Slash command sync failed.")

    async def on_ready(self):

        logger.info(f"Logged in as {self.user}")
        logger.info(f"Connected to {len(self.guilds)} server(s).")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Oman Air Operations ✈️"
            )
        )

# -----------------------------
# START BOT
# -----------------------------

bot = OmanAirBot()

bot.run(TOKEN)
