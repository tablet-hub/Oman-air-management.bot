# ============================================
# ✈️ OMAN AIR MANAGEMENT
# Official Airline Discord Management System
# ============================================

import os
import logging
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands

# ============================================
# CONFIGURATION
# ============================================

BOT_NAME = "OMAN AIR MANAGEMENT"
BOT_VERSION = "1.0.0"

TOKEN = os.getenv("DISCORD_TOKEN")

# ============================================
# LOGGING SYSTEM
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("OmanAirManagement")

# ============================================
# DISCORD INTENTS
# ============================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

# ============================================
# BOT ENGINE
# ============================================

class OmanAirManagement(commands.Bot):

    def __init__(self):

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):

        logger.info("Loading Oman Air Management modules...")

        cog_directory = Path("cogs")

        if cog_directory.exists():

            for file in cog_directory.glob("*.py"):

                if file.name.startswith("_"):
                    continue

                extension = f"cogs.{file.stem}"

                try:

                    await self.load_extension(extension)

                    logger.info(
                        f"Module loaded successfully: {extension}"
                    )

                except Exception:

                    logger.exception(
                        f"Failed to load module: {extension}"
                    )

        else:

            logger.warning(
                "Cogs directory not found. No modules loaded."
            )

        try:

            synced_commands = await self.tree.sync()

            logger.info(
                f"Successfully synchronized "
                f"{len(synced_commands)} slash commands."
            )

        except Exception:

            logger.exception(
                "Failed to synchronize slash commands."
            )

    async def on_ready(self):

        logger.info("----------------------------------")

        logger.info(f"Bot Name: {BOT_NAME}")

        logger.info(f"Bot Version: {BOT_VERSION}")

        logger.info(f"Logged in as: {self.user}")

        logger.info(f"Bot ID: {self.user.id}")

        logger.info(
            f"Connected Servers: {len(self.guilds)}"
        )

        logger.info("----------------------------------")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Oman Air Operations ✈️"
            ),
            status=discord.Status.online
        )

# ============================================
# CREATE BOT INSTANCE
# ============================================

bot = OmanAirManagement()

# ============================================
# GLOBAL SLASH COMMAND ERROR HANDLER
# ============================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    logger.error(
        f"Command Error: {error}",
        exc_info=error
    )

    message = (
        "An unexpected error occurred. "
        "Please contact Oman Air Management staff."
    )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except discord.HTTPException:

        logger.exception(
            "Could not send error message to user."
        )

# ============================================
# START BOT
# ============================================

if __name__ == "__main__":

    if not TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is missing. "
            "Please configure it in Railway Variables."
        )

    bot.run(TOKEN)
