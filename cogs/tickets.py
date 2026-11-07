import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime, timezone
from io import BytesIO

DATA_FILE = "tickets.json"

BLUE = discord.Color.from_rgb(0, 45, 90)
GOLD = discord.Color.from_rgb(190, 155, 70)


def load_data():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_data(data):
    with open(DATA_FILE, "w") as file:
        json.dump(data, file, indent=4)


def current_time():
    return datetime.now(timezone.utc).isoformat()


def make_embed(title, description, color=BLUE):
    embed = discord.Embed(
        title=f"✈️ {title}",
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    embed.set_footer(
        text="OMAN AIR MANAGEMENT | Customer Support"
    )

    return embed


class CloseTicketModal(discord.ui.Modal, title="Close Ticket"):

    reason = discord.ui.TextInput(
        label="Reason for closing",
        placeholder="Enter the reason for closing this ticket...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):

        await self.cog.close_ticket(
            interaction,
            str(self.reason)
        )


class AssignTicketModal(discord.ui.Modal, title="Assign Ticket"):

    staff_id = discord.ui.TextInput(
        label="Staff Member Discord ID",
        placeholder="Enter the staff member's Discord ID",
        required=True,
        max_length=20
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):

        try:
            user_id = int(str(self.staff_id))
        except ValueError:
            await interaction.response.send_message(
                "Invalid Discord ID.",
                ephemeral=True
            )
            return

        member = interaction.guild.get_member(user_id)

        if member is None:
            await interaction.response.send_message(
                "That staff member is not in this server.",
                ephemeral=True
            )
            return

        if not self.cog.is_staff(interaction, member):
            await interaction.response.send_message(
                "That member is not an authorized ticket staff member.",
                ephemeral=True
            )
            return

        await self.cog.assign_ticket(
            interaction,
            member
        )


class AddMemberModal(discord.ui.Modal, title="Add Member to Ticket"):

    member_id = discord.ui.TextInput(
        label="Member Discord ID",
        placeholder="Enter the member's Discord ID",
        required=True,
        max_length=20
    )

    def __init__(self, cog, remove=False):
        super().__init__()
        self.cog = cog
        self.remove = remove

    async def on_submit(self, interaction: discord.Interaction):

        try:
            user_id = int(str(self.member_id))
        except ValueError:
            await interaction.response.send_message(
                "Invalid Discord ID.",
                ephemeral=True
            )
            return

        member = interaction.guild.get_member(user_id)

        if member is None:
            await interaction.response.send_message(
                "Member not found in this server.",
                ephemeral=True
            )
            return

        channel = interaction.channel

        if self.remove:

            await channel.set_permissions(
                member,
                overwrite=None
            )

            await interaction.response.send_message(
                f"Removed {member.mention} from the ticket.",
                ephemeral=True
            )

        else:

            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

            await interaction.response.send_message(
                f"Added {member.mention} to the ticket.",
                ephemeral=True
            )


class PrioritySelect(discord.ui.Select):

    def __init__(self, cog):

        self.cog = cog

        options = [
            discord.SelectOption(
                label="Low",
                value="Low",
                emoji="🟢"
            ),
            discord.SelectOption(
                label="Normal",
                value="Normal",
                emoji="🟡"
            ),
            discord.SelectOption(
                label="High",
                value="High",
                emoji="🟠"
            ),
            discord.SelectOption(
                label="Urgent",
                value="Urgent",
                emoji="🔴"
            )
        ]

        super().__init__(
            placeholder="Select ticket priority...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="oman_ticket_priority"
        )

    async def callback(self, interaction: discord.Interaction):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        priority = self.values[0]

        record = self.cog.get_ticket(interaction.channel.id)

        if record is None:
            await interaction.response.send_message(
                "Ticket not found.",
                ephemeral=True
            )
            return

        record["priority"] = priority
        record["updated_at"] = current_time()

        self.cog.save()
                priority_symbols = {
            "Low": "🟢",
            "Normal": "🟡",
            "High": "🟠",
            "Urgent": "🔴"
        }

        symbol = priority_symbols.get(priority, "⚪")

        channel_name = interaction.channel.name

        for old_symbol in priority_symbols.values():
            channel_name = channel_name.replace(f"{old_symbol}-", "")

        await interaction.channel.edit(
            name=f"{symbol}-{channel_name}"
        )

        embed = make_embed(
            "Ticket Priority Updated",
            f"Priority has been changed to **{priority}**."
        )

        await interaction.response.send_message(
            embed=embed
        )


class PriorityView(discord.ui.View):

    def __init__(self, cog):
        super().__init__(timeout=60)
        self.add_item(PrioritySelect(cog))


class TicketControls(discord.ui.View):

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Claim",
        emoji="🙋",
        style=discord.ButtonStyle.success,
        custom_id="oman_ticket_claim"
    )
    async def claim(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Only authorized staff can claim tickets.",
                ephemeral=True
            )
            return

        record = self.cog.get_ticket(interaction.channel.id)

        if record is None:
            await interaction.response.send_message(
                "Ticket record not found.",
                ephemeral=True
            )
            return

        if record.get("claimed_by"):
            await interaction.response.send_message(
                "This ticket is already claimed.",
                ephemeral=True
            )
            return

        record["claimed_by"] = interaction.user.id
        record["updated_at"] = current_time()

        self.cog.save()

        embed = make_embed(
            "Ticket Claimed",
            f"This ticket is now handled by {interaction.user.mention}."
        )

        await interaction.response.send_message(
            embed=embed
        )

    @discord.ui.button(
        label="Unclaim",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        custom_id="oman_ticket_unclaim"
    )
    async def unclaim(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        record = self.cog.get_ticket(interaction.channel.id)

        if record is None:
            await interaction.response.send_message(
                "Ticket record not found.",
                ephemeral=True
            )
            return

        record["claimed_by"] = None
        record["updated_at"] = current_time()

        self.cog.save()

        await interaction.response.send_message(
            embed=make_embed(
                "Ticket Unclaimed",
                "This ticket is now available for another staff member."
            )
        )

    @discord.ui.button(
        label="Assign",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        custom_id="oman_ticket_assign"
    )
    async def assign(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            AssignTicketModal(self.cog)
        )

    @discord.ui.button(
        label="Transfer",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        custom_id="oman_ticket_transfer"
    )
    async def transfer(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            AssignTicketModal(self.cog)
        )

    @discord.ui.button(
        label="Priority",
        emoji="🚦",
        style=discord.ButtonStyle.secondary,
        custom_id="oman_ticket_priority_button"
    )
    async def priority(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Choose the ticket priority:",
            view=PriorityView(self.cog),
            ephemeral=True
        )

    @discord.ui.button(
        label="Add Member",
        emoji="➕",
        style=discord.ButtonStyle.secondary,
        custom_id="oman_ticket_add_member"
    )
    async def add_member(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            AddMemberModal(self.cog)
        )

    @discord.ui.button(
        label="Remove Member",
        emoji="➖",
        style=discord.ButtonStyle.secondary,
        custom_id="oman_ticket_remove_member"
    )
    async def remove_member(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            AddMemberModal(self.cog, remove=True)
        )

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="oman_ticket_close"
    )
    async def close(self, interaction, button):

        if not self.cog.is_staff(interaction):
            await interaction.response.send_message(
                "Staff only.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            CloseTicketModal(self.cog)
        )


class TicketPanel(discord.ui.View):

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open Support Ticket",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
        custom_id="oman_ticket_open"
    )
    async def open_ticket(self, interaction, button):

        guild = interaction.guild
        user = interaction.user

        if not self.cog.is_configured(guild.id):
            await interaction.response.send_message(
                "The ticket system has not been configured yet.",
                ephemeral=True
            )
            return

        existing = self.cog.find_open_ticket(
            guild.id,
            user.id
        )

        if existing:
            channel = guild.get_channel(
                existing["channel_id"]
            )

            if channel:
                await interaction.response.send_message(
                    f"You already have an open ticket: {channel.mention}",
                    ephemeral=True
                )
                return

        config = self.cog.data[str(guild.id)]

        staff_role = guild.get_role(
            config["staff_role"]
        )

        category = guild.get_channel(
            config.get("category")
        )

        ticket_number = self.cog.next_ticket_number(guild.id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )
        }

        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

        try:

            channel = await guild.create_text_channel(
                name=f"ticket-{ticket_number:04d}",
                category=category,
                overwrites=overwrites,
                topic=f"Oman Air Support Ticket #{ticket_number}"
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "I lack permission to create ticket channels.",
                ephemeral=True
            )
            return

        record = {
            "ticket_number": ticket_number,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "creator_id": user.id,
            "claimed_by": None,
            "priority": "Normal",
            "status": "Open",
            "created_at": current_time(),
            "updated_at": current_time(),
            "closing_reason": None
        }

        self.cog.data.setdefault(str(guild.id), {}).setdefault(
            "tickets", {}
        )[str(channel.id)] = record

        self.cog.save()

        embed = make_embed(
            f"Support Ticket #{ticket_number}",
            f"Welcome {user.mention}!\n\n"
            "Thank you for contacting Oman Air Support.\n"
            "A member of our team will assist you shortly.\n\n"
            f"**Priority:** 🟡 Normal\n"
            f"**Status:** Open\n"
            f"**Assigned Staff:** Unclaimed"
        )

        await channel.send(
            content=user.mention,
            embed=embed,
            view=TicketControls(self.cog)
        )

        await interaction.response.send_message(
            f"Your ticket has been created: {channel.mention}",
            ephemeral=True
        )


class Tickets(commands.Cog):

    def __init__(self, bot):

        self.bot = bot
        self.data = load_data()

    def save(self):
        save_data(self.data)

    def get_config(self, guild_id):
        return self.data.get(str(guild_id), {})

    def is_configured(self, guild_id):

        config = self.get_config(guild_id)

        return bool(config.get("staff_role"))

    def get_ticket(self, channel_id):

        for guild_data in self.data.values():

            tickets = guild_data.get("tickets", {})

            record = tickets.get(str(channel_id))

            if record:
                return record

        return None

    def find_open_ticket(self, guild_id, user_id):

        guild_data = self.get_config(guild_id)

        for record in guild_data.get("tickets", {}).values():

            if (
                record["creator_id"] == user_id
                and record["status"] == "Open"
            ):
                return record

        return None

    def next_ticket_number(self, guild_id):

        guild_data = self.get_config(guild_id)

        number = guild_data.get("ticket_counter", 0) + 1

        guild_data["ticket_counter"] = number

        self.save()

        return number

    def is_staff(self, interaction, member=None):

        member = member or interaction.user

        config = self.get_config(interaction.guild.id)

        staff_role = interaction.guild.get_role(
            config.get("staff_role", 0)
        )

        if member.guild_permissions.administrator:
            return True

        return bool(
            staff_role and staff_role in member.roles
        )

    async def assign_ticket(self, interaction, member):

        record = self.get_ticket(
            interaction.channel.id
        )

        if record is None:
            await interaction.response.send_message(
                "Ticket not found.",
                ephemeral=True
            )
            return

        record["claimed_by"] = member.id
        record["updated_at"] = current_time()

        self.save()

        await interaction.response.send_message(
            embed=make_embed(
                "Ticket Assigned",
                f"This ticket is assigned to {member.mention}."
            )
        )

    async def close_ticket(self, interaction, reason):

        record = self.get_ticket(
            interaction.channel.id
        )

        if record is None:
            await interaction.response.send_message(
                "Ticket not found.",
                ephemeral=True
            )
            return

        record["status"] = "Closed"
        record["closing_reason"] = reason
        record["updated_at"] = current_time()

        self.save()

        channel = interaction.channel

        creator = interaction.guild.get_member(
            record["creator_id"]
        )

        if creator:

            await channel.set_permissions(
                creator,
                view_channel=True,
                send_messages=False,
                read_message_history=True
            )

        embed = make_embed(
            "Ticket Closed",
            f"**Ticket:** #{record['ticket_number']}\n"
            f"**Closed By:** {interaction.user.mention}\n"
            f"**Reason:** {reason}\n\n"
            "This ticket has been closed."
        )

        await interaction.response.send_message(
            embed=embed
        )

        config = self.get_config(
            interaction.guild.id
        )

        log_channel = interaction.guild.get_channel(
            config.get("log_channel", 0)
        )

        if log_channel:

            await log_channel.send(
                embed=embed
            )

    @app_commands.command(
        name="ticketsetup",
        description="Configure Oman Air ticket support."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        staff_role="The role that manages support tickets.",
        log_channel="The channel for ticket logs.",
        category="The category where ticket channels are created."
    )
    async def ticketsetup(
        self,
        interaction: discord.Interaction,
        staff_role: discord.Role,
        log_channel: discord.TextChannel,
        category: discord.CategoryChannel
    ):

        guild_id = str(interaction.guild.id)

        self.data.setdefault(guild_id, {})

        self.data[guild_id]["staff_role"] = staff_role.id
        self.data[guild_id]["log_channel"] = log_channel.id
        self.data[guild_id]["category"] = category.id

        self.data[guild_id].setdefault(
            "ticket_counter", 0
        )

        self.data[guild_id].setdefault(
            "tickets", {}
        )

        self.save()

        await interaction.response.send_message(
            embed=make_embed(
                "Ticket System Configured",
                f"**Staff Role:** {staff_role.mention}\n"
                f"**Log Channel:** {log_channel.mention}\n"
                f"**Ticket Category:** {category.name}"
            ),
            ephemeral=True
        )

    @app_commands.command(
        name="ticketpanel",
        description="Post the Oman Air support ticket panel."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ticketpanel(
        self,
        interaction: discord.Interaction
    ):

        if not self.is_configured(
            interaction.guild.id
        ):
            await interaction.response.send_message(
                "Run /ticketsetup first.",
                ephemeral=True
            )
            return

        embed = make_embed(
            "Oman Air Customer Support",
            "Welcome to the Oman Air Support Centre.\n\n"
            "Our support team is available to assist with:\n\n"
            "✈️ Flight Operations\n"
            "🎫 Passenger Bookings\n"
            "👨‍✈️ Pilot and Crew Support\n"
            "🏢 Airline Management\n"
            "📋 General Enquiries\n\n"
            "Press the button below to open a private support ticket."
        )

        embed.set_thumbnail(
            url=interaction.guild.icon.url
            if interaction.guild.icon else None
        )

        await interaction.channel.send(
            embed=embed,
            view=TicketPanel(self)
        )

        await interaction.response.send_message(
            "Oman Air ticket panel posted successfully.",
            ephemeral=True
        )


async def setup(bot):

    cog = Tickets(bot)

    await bot.add_cog(cog)

    bot.add_view(TicketPanel(cog))
    bot.add_view(TicketControls(cog))
