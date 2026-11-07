import discord
from discord.ext import commands
from discord import app_commands
import json
import os

CONFIG_FILE = "welcome_config.json"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}

    try:
        with open(CONFIG_FILE, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(config):
    with open(CONFIG_FILE, "w") as file:
        json.dump(config, file, indent=4)


class Welcome(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()

    @app_commands.command(
        name="setwelcome",
        description="Set the Oman Air welcome channel."
    )
    @app_commands.describe(
        channel="The channel where welcome messages will be sent."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):

        guild_id = str(interaction.guild.id)

        self.config[guild_id] = {
            "welcome_channel": channel.id
        }

        save_config(self.config)

        embed = discord.Embed(
            title="✈️ Oman Air Welcome System",
            description=(
                f"Welcome messages are now enabled in {channel.mention}."
            ),
            color=discord.Color.blue()
        )

        embed.set_footer(
            text="OMAN AIR MANAGEMENT | System Configuration"
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_member_join(self, member):

        guild_id = str(member.guild.id)

        guild_config = self.config.get(guild_id)

        if not guild_config:
            return

        channel_id = guild_config.get("welcome_channel")

        if not channel_id:
            return

        channel = member.guild.get_channel(channel_id)

        if channel is None:
            return

        embed = discord.Embed(
            title="✈️ Welcome Aboard Oman Air!",
            description=(
                f"Welcome to **{member.guild.name}**, {member.mention}!\n\n"
                "We are delighted to have you aboard.\n\n"
                "Please review our server rules and enjoy your journey "
                "with the Oman Air community."
            ),
            color=discord.Color.blue()
        )

        embed.set_thumbnail(
            url=member.display_avatar.url
        )

        embed.add_field(
            name="👤 Passenger",
            value=member.name,
            inline=True
        )

        embed.add_field(
            name="🎫 Passenger Number",
            value=str(member.id),
            inline=True
        )

        embed.add_field(
            name="👥 Total Members",
            value=str(member.guild.member_count),
            inline=True
        )

        embed.set_footer(
            text="OMAN AIR MANAGEMENT | Welcome Aboard"
        )

        try:
            await channel.send(
                content=f"Welcome aboard {member.mention}!",
                embed=embed
            )
        except discord.Forbidden:
            print(
                f"Missing permissions to send welcome message "
                f"in {channel.name}"
            )

    @app_commands.command(
        name="testwelcome",
        description="Preview the Oman Air welcome message."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def testwelcome(self, interaction: discord.Interaction):

        member = interaction.user

        embed = discord.Embed(
            title="✈️ Welcome Aboard Oman Air!",
            description=(
                f"Welcome to **{interaction.guild.name}**, "
                f"{member.mention}!\n\n"
                "We are delighted to have you aboard.\n\n"
                "Please review our server rules and enjoy your journey "
                "with the Oman Air community."
            ),
            color=discord.Color.blue()
        )

        embed.set_thumbnail(
            url=member.display_avatar.url
        )

        embed.add_field(
            name="👤 Passenger",
            value=member.name,
            inline=True
        )

        embed.add_field(
            name="👥 Total Members",
            value=str(interaction.guild.member_count),
            inline=True
        )

        embed.set_footer(
            text="OMAN AIR MANAGEMENT | Welcome Aboard"
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Welcome(bot))
