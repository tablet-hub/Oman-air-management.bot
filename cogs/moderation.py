import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import timedelta

WARNINGS_FILE = "warnings.json"


def load_warnings():
    if not os.path.exists(WARNINGS_FILE):
        return {}

    try:
        with open(WARNINGS_FILE, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_warnings(warnings):
    with open(WARNINGS_FILE, "w") as file:
        json.dump(warnings, file, indent=4)


class Moderation(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.warnings = load_warnings()

    def create_embed(self, title, description):
        embed = discord.Embed(
            title=f"🛡️ {title}",
            description=description,
            color=discord.Color.blue()
        )

        embed.set_footer(
            text="OMAN AIR MANAGEMENT | Moderation"
        )

        return embed

    # BAN

    @app_commands.command(
        name="ban",
        description="Ban a member from the server."
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(
        member="The member to ban.",
        reason="The reason for the ban."
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided"
    ):

        if member == interaction.user:
            await interaction.response.send_message(
                "You cannot ban yourself.",
                ephemeral=True
            )
            return

        try:
            await member.ban(reason=reason)

            embed = self.create_embed(
                "Member Banned",
                f"**Member:** {member.mention}\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Reason:** {reason}"
            )

            await interaction.response.send_message(
                embed=embed
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to ban this member.",
                ephemeral=True
            )

    # KICK

    @app_commands.command(
        name="kick",
        description="Kick a member from the server."
    )
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.describe(
        member="The member to kick.",
        reason="The reason for the kick."
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided"
    ):

        if member == interaction.user:
            await interaction.response.send_message(
                "You cannot kick yourself.",
                ephemeral=True
            )
            return

        try:
            await member.kick(reason=reason)

            embed = self.create_embed(
                "Member Kicked",
                f"**Member:** {member.mention}\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Reason:** {reason}"
            )

            await interaction.response.send_message(
                embed=embed
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to kick this member.",
                ephemeral=True
            )

    # TIMEOUT

    @app_commands.command(
        name="timeout",
        description="Timeout a member."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(
        member="The member to timeout.",
        minutes="Duration in minutes.",
        reason="Reason for the timeout."
    )
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str = "No reason provided"
    ):

        try:
            await member.timeout(
                timedelta(minutes=minutes),
                reason=reason
            )

            embed = self.create_embed(
                "Member Timed Out",
                f"**Member:** {member.mention}\n"
                f"**Duration:** {minutes} minutes\n"
                f"**Moderator:** {interaction.user.mention}\n"
                f"**Reason:** {reason}"
            )

            await interaction.response.send_message(
                embed=embed
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot timeout this member. "
                "Check my permissions and role hierarchy.",
                ephemeral=True
            )

    # REMOVE TIMEOUT

    @app_commands.command(
        name="untimeout",
        description="Remove a member's timeout."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):

        try:
            await member.timeout(
                None,
                reason=f"Timeout removed by {interaction.user}"
            )

            embed = self.create_embed(
                "Timeout Removed",
                f"Timeout removed for {member.mention}."
            )

            await interaction.response.send_message(
                embed=embed
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot remove this timeout.",
                ephemeral=True
            )

    # WARN

    @app_commands.command(
        name="warn",
        description="Issue a warning to a member."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str
    ):

        guild_id = str(interaction.guild.id)
        member_id = str(member.id)

        if guild_id not in self.warnings:
            self.warnings[guild_id] = {}

        if member_id not in self.warnings[guild_id]:
            self.warnings[guild_id][member_id] = []

        warning = {
            "reason": reason,
            "moderator": str(interaction.user.id)
        }

        self.warnings[guild_id][member_id].append(warning)

        save_warnings(self.warnings)

        warning_count = len(
            self.warnings[guild_id][member_id]
        )

        embed = self.create_embed(
            "Member Warned",
            f"**Member:** {member.mention}\n"
            f"**Moderator:** {interaction.user.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Total Warnings:** {warning_count}"
        )

        await interaction.response.send_message(
            embed=embed
        )

    # VIEW WARNINGS

    @app_commands.command(
        name="warnings",
        description="View a member's warnings."
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):

        guild_id = str(interaction.guild.id)
        member_id = str(member.id)

        member_warnings = (
            self.warnings
            .get(guild_id, {})
            .get(member_id, [])
        )

        if not member_warnings:
            await interaction.response.send_message(
                f"{member.mention} has no warnings.",
                ephemeral=True
            )
            return

        description = ""

        for index, warning in enumerate(
            member_warnings,
            start=1
        ):

            description += (
                f"**Warning {index}**\n"
                f"Reason: {warning['reason']}\n\n"
            )

        embed = self.create_embed(
            f"Warnings for {member.name}",
            description
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    # CLEAR MESSAGES

    @app_commands.command(
        name="clear",
        description="Delete messages from a channel."
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="Number of messages to delete."
    )
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100]
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        deleted = await interaction.channel.purge(
            limit=amount
        )

        embed = self.create_embed(
            "Messages Cleared",
            f"Deleted **{len(deleted)} messages**.\n"
            f"Moderator: {interaction.user.mention}"
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )

    # SLOWMODE

    @app_commands.command(
        name="slowmode",
        description="Set channel slowmode."
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.describe(
        seconds="Slowmode duration in seconds. 0 disables it."
    )
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600]
    ):

        try:

            await interaction.channel.edit(
                slowmode_delay=seconds
            )

            embed = self.create_embed(
                "Slowmode Updated",
                f"Slowmode set to **{seconds} seconds**."
            )

            await interaction.response.send_message(
                embed=embed
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot change slowmode in this channel.",
                ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(Moderation(bot))
