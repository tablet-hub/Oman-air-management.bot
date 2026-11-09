import os
import logging
import json
import uuid
import asyncio
import secrets
import urllib.request
import urllib.error
from pathlib import Path
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oman-air")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
STAFF_GUILD_ID = int(os.getenv("STAFF_GUILD_ID", "1552678194996449394") or "1552678194996449394")
MAIN_GUILD_ID = int(os.getenv("MAIN_GUILD_ID", "1552773061894348952") or "1552773061894348952")
MAIN_ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("MAIN_ANNOUNCEMENT_CHANNEL_ID", "1554496632890986498") or "1554496632890986498")
DATA_PATH = Path("tickets.json")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
CHAT_HISTORY = {}

# Add consistent Oman Air branding to every message sent by the bot.
BRAND_SIGNATURE = "Oman Air • A world of possibilities • a oneworld partner"
OMAN_AIR_LOGO_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/Oman_Air_logo.png"
ONEWORLD_LOGO_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/Oneworld_logo.svg"

def _brand_message_kwargs(kwargs):
    kwargs = dict(kwargs)
    content = kwargs.get("content")
    if isinstance(content, str) and content.strip() and BRAND_SIGNATURE not in content:
        kwargs["content"] = content + "\n\n" + BRAND_SIGNATURE
    embed = kwargs.get("embed")
    if isinstance(embed, discord.Embed):
        embed.set_footer(text="Oman Air", icon_url=OMAN_AIR_LOGO_URL)
        embed.set_author(name="A oneworld partner", icon_url=ONEWORLD_LOGO_URL)
    embeds = kwargs.get("embeds")
    if embeds:
        for item in embeds:
            if isinstance(item, discord.Embed):
                item.set_footer(text="Oman Air", icon_url=OMAN_AIR_LOGO_URL)
                item.set_author(name="A oneworld partner", icon_url=ONEWORLD_LOGO_URL)
    return kwargs

_original_messageable_send = discord.abc.Messageable.send
async def _branded_messageable_send(self, *args, **kwargs):
    if args and isinstance(args[0], str) and BRAND_SIGNATURE not in args[0]:
        args = (args[0] + "\n\n" + BRAND_SIGNATURE,) + args[1:]
    return await _original_messageable_send(self, *args, **_brand_message_kwargs(kwargs))
discord.abc.Messageable.send = _branded_messageable_send

_original_interaction_send = discord.InteractionResponse.send_message
async def _branded_interaction_send(self, *args, **kwargs):
    if args and isinstance(args[0], str) and BRAND_SIGNATURE not in args[0]:
        args = (args[0] + "\n\n" + BRAND_SIGNATURE,) + args[1:]
    return await _original_interaction_send(self, *args, **_brand_message_kwargs(kwargs))
discord.InteractionResponse.send_message = _branded_interaction_send

_original_webhook_send = discord.Webhook.send
async def _branded_webhook_send(self, *args, **kwargs):
    if args and isinstance(args[0], str) and BRAND_SIGNATURE not in args[0]:
        args = (args[0] + "\n\n" + BRAND_SIGNATURE,) + args[1:]
    return await _original_webhook_send(self, *args, **_brand_message_kwargs(kwargs))
discord.Webhook.send = _branded_webhook_send


if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
if not GUILD_ID:
    raise RuntimeError("Missing GUILD_ID environment variable. Set it to your Discord server ID.")

# Create the bot before registering slash commands.
intents = discord.Intents.default()
intents.members = True  # Required to receive member-join events.
bot = commands.Bot(command_prefix="!", intents=intents)

def load_data():
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def save_data(data):
    DATA_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

def guild_data(guild_id):
    return load_data().setdefault(str(guild_id), {})

def operations_data(data=None):
    store = data if data is not None else load_data()
    return store.setdefault(str(STAFF_GUILD_ID), {})

def staff_member(member: discord.Member, cfg: dict) -> bool:
    role = member.guild.get_role(cfg.get("staff_role_id", 0))
    return member.guild_permissions.administrator or bool(role and role in member.roles)

def ticket_record(guild_id: int, channel_id: int):
    return load_data().get(str(guild_id), {}).get("tickets", {}).get(str(channel_id))

def update_ticket(guild_id, channel_id, key, value):
    data = load_data()
    record = data.setdefault(str(guild_id), {}).setdefault("tickets", {}).setdefault(str(channel_id), {})
    record[key] = value
    save_data(data)
    return record

async def require_staff(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this in a server ticket channel.", ephemeral=True)
        return False
    if not staff_member(interaction.user, guild_data(interaction.guild.id)):
        await interaction.response.send_message("This tool is only available to configured support staff.", ephemeral=True)
        return False
    record = ticket_record(interaction.guild.id, interaction.channel_id)
    if not record:
        await interaction.response.send_message("This channel is not a tracked ticket.", ephemeral=True)
        return False
    return True

class UserIdModal(discord.ui.Modal):
    def __init__(self, action):
        super().__init__(title=f"{action.title()} ticket member")
        self.action = action
        self.user_id = discord.ui.TextInput(label="Mention the member", placeholder="@Member or paste their mention", max_length=100)
        self.add_item(self.user_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        try:
            mention = str(self.user_id.value).strip()
            member_id = int(mention.replace("<@", "").replace(">", "").replace("!", ""))
            member = interaction.guild.get_member(member_id) or await interaction.guild.fetch_member(member_id)
        except (ValueError, discord.NotFound, discord.HTTPException):
            return await interaction.response.send_message("I couldn't find that member in this server. Check the ID.", ephemeral=True)

        channel = interaction.channel
        if self.action == "add":
            await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
            await interaction.response.send_message(f"Added {member.mention} to this ticket.", ephemeral=True)
        else:
            record = ticket_record(interaction.guild.id, channel.id)
            if record and record.get("creator_id") == member.id:
                return await interaction.response.send_message("You can't remove the ticket creator.", ephemeral=True)
            await channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(f"Removed {member.mention} from this ticket.", ephemeral=True)

class StaffIdModal(discord.ui.Modal):
    def __init__(self, action):
        super().__init__(title=f"{action.title()} ticket staff")
        self.action = action
        self.user_id = discord.ui.TextInput(label="Mention the staff member", placeholder="@Staff member or paste their mention", max_length=100)
        self.add_item(self.user_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        try:
            mention = str(self.user_id.value).strip()
            staff_id = int(mention.replace("<@", "").replace(">", "").replace("!", ""))
            member = interaction.guild.get_member(staff_id) or await interaction.guild.fetch_member(staff_id)
        except (ValueError, discord.NotFound, discord.HTTPException):
            return await interaction.response.send_message("I couldn't find that member in this server.", ephemeral=True)
        if not staff_member(member, guild_data(interaction.guild.id)):
            return await interaction.response.send_message("That member is not part of the configured staff team.", ephemeral=True)
        key = "claimed_by" if self.action == "assign" else "transferred_to"
        update_ticket(interaction.guild.id, interaction.channel_id, key, member.id)
        await interaction.response.send_message(f"Ticket {self.action}ed to {member.mention}.", ephemeral=True)
        embed = discord.Embed(title=f"Ticket {self.action.title()}", description=f"{interaction.user.mention} {self.action}ed this ticket to {member.mention}.", color=discord.Color.blue())
        await interaction.channel.send(embed=embed)

class CloseReasonModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Close ticket with reason")
        self.reason = discord.ui.TextInput(label="Closing reason", style=discord.TextStyle.paragraph, placeholder="Explain why this ticket is being closed", max_length=900, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        reason = str(self.reason.value).strip()
        update_ticket(interaction.guild.id, interaction.channel_id, "status", "closed")
        update_ticket(interaction.guild.id, interaction.channel_id, "close_reason", reason)
        embed = discord.Embed(title="🔒 Ticket Closed", description=f"Closed by {interaction.user.mention}\n**Reason:** {reason}", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user} | Reason: {reason}")

class StaffReplyModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Send an embedded staff reply")
        self.message = discord.ui.TextInput(label="Message to ticket member", style=discord.TextStyle.paragraph, placeholder="Write your response...", max_length=1800, required=True)
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        embed = discord.Embed(title="✈️ Oman Air Support | Staff Reply", description=str(self.message.value), color=discord.Color.from_rgb(0, 45, 90))
        embed.set_author(name=f"Support staff: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="Oman Air Support")
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("Your embedded reply was sent.", ephemeral=True)

class PrioritySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Low", value="Low", description="Low-priority ticket"),
            discord.SelectOption(label="Normal", value="Normal", description="Standard priority"),
            discord.SelectOption(label="High", value="High", description="High-priority ticket"),
            discord.SelectOption(label="Urgent", value="Urgent", description="Urgent attention required"),
        ]
        super().__init__(placeholder="Choose a ticket priority...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        selected = self.values[0]
        update_ticket(interaction.guild.id, interaction.channel_id, "priority", selected)
        channel_name = interaction.channel.name
        suffixes = ["low", "normal", "high", "urgent"]
        parts = channel_name.rsplit("-", 1)
        base_name = parts[0] if len(parts) == 2 and parts[1].lower() in suffixes else channel_name
        await interaction.channel.edit(name=f"{base_name}-{selected.lower()}"[:100])
        await interaction.response.edit_message(content=f"Ticket priority changed to **{selected}**. You can use the Change Priority button again at any time.", view=None)

class PrioritySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(PrioritySelect())

class TicketStaffTools(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="oman_staff_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        if record.get("claimed_by"):
            return await interaction.response.send_message(f"Already claimed by <@{record['claimed_by']}>.", ephemeral=True)
        update_ticket(interaction.guild.id, interaction.channel_id, "claimed_by", interaction.user.id)
        await interaction.response.send_message(f"{interaction.user.mention} claimed this ticket.")
        
    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="oman_staff_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        if not record.get("claimed_by"):
            return await interaction.response.send_message("This ticket is currently unclaimed.", ephemeral=True)
        update_ticket(interaction.guild.id, interaction.channel_id, "claimed_by", None)
        await interaction.response.send_message(f"Ticket unclaimed by {interaction.user.mention}.")

    @discord.ui.button(label="Assign", style=discord.ButtonStyle.primary, custom_id="oman_staff_assign")
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(StaffIdModal("assign"))

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.primary, custom_id="oman_staff_transfer")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(StaffIdModal("transfer"))

    @discord.ui.button(label="Add Member", style=discord.ButtonStyle.secondary, custom_id="oman_staff_add_member")
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(UserIdModal("add"))

    @discord.ui.button(label="Remove Member", style=discord.ButtonStyle.secondary, custom_id="oman_staff_remove_member")
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(UserIdModal("remove"))

    @discord.ui.button(label="Change Priority", style=discord.ButtonStyle.secondary, custom_id="oman_staff_priority")
    async def priority(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction):
            return
        await interaction.response.send_message("Select the new priority. You can change it again whenever needed.", view=PrioritySelectView(), ephemeral=True)

    @discord.ui.button(label="Staff Reply", style=discord.ButtonStyle.primary, custom_id="oman_staff_reply")
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(StaffReplyModal())

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="oman_staff_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(CloseReasonModal())

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Support Ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="oman_ticket_open_v1")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use this button in the server.", ephemeral=True)
        data = load_data()
        cfg = data.setdefault(str(interaction.guild.id), {})
        category_id, staff_role_id = cfg.get("category_id"), cfg.get("staff_role_id")
        if not category_id or not staff_role_id:
            return await interaction.response.send_message("Admin must run /ticketsetup first.", ephemeral=True)
        tickets = cfg.setdefault("tickets", {})
        for record in tickets.values():
            if record.get("creator_id") == interaction.user.id and record.get("status") == "open":
                channel = interaction.guild.get_channel(record.get("channel_id", 0))
                if channel:
                    return await interaction.response.send_message(f"You already have a ticket: {channel.mention}", ephemeral=True)
        category = interaction.guild.get_channel(category_id)
        staff_role = interaction.guild.get_role(staff_role_id)
        if not isinstance(category, discord.CategoryChannel) or not staff_role:
            return await interaction.response.send_message("Setup IDs are invalid. Run /ticketsetup again.", ephemeral=True)
        number = int(cfg.get("counter", 0)) + 1
        cfg["counter"] = number
        save_data(data)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if interaction.guild.me:
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
        try:
            channel = await interaction.guild.create_text_channel(name=f"ticket-{number:04d}", category=category, overwrites=overwrites, topic=f"Oman Air support ticket {number} | creator {interaction.user.id}")
        except discord.Forbidden:
            return await interaction.response.send_message("Bot lacks Manage Channels permission.", ephemeral=True)
        except discord.HTTPException as exc:
            log.exception("Channel creation failed")
            return await interaction.response.send_message(f"Discord could not create the channel: {exc}", ephemeral=True)
        tickets[str(channel.id)] = {"ticket_number": number, "channel_id": channel.id, "creator_id": interaction.user.id, "status": "open", "claimed_by": None, "transferred_to": None, "priority": "Normal", "close_reason": None}
        save_data(data)
        welcome = discord.Embed(title=f"🎫 Oman Air Support Ticket #{number}", description=f"Welcome {interaction.user.mention}. Describe your support request below.\n\nA staff member will assist you soon.", color=discord.Color.from_rgb(0, 45, 90))
        welcome.add_field(name="Ticket privacy", value="This channel is private to you and support staff.", inline=False)
        await channel.send(embed=welcome, view=TicketStaffTools())
        confirmation = discord.Embed(title="✅ Oman Air Support Ticket Created", description=f"Your private support ticket is ready: {channel.mention}\n\nPlease open the ticket channel and describe your request. Support staff will assist you soon.", color=discord.Color.from_rgb(0, 45, 90))
        confirmation.add_field(name="Ticket number", value=f"#{number}", inline=True)
        confirmation.add_field(name="Privacy", value="Only you and support staff can view the ticket channel.", inline=True)
        confirmation.set_footer(text="Oman Air Support")
        await interaction.response.send_message(embed=confirmation, ephemeral=True)

class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="oman_ticket_close_v1")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Server-only action.", ephemeral=True)
        cfg = guild_data(interaction.guild.id)
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        if not record:
            return await interaction.response.send_message("This channel is not a tracked ticket.", ephemeral=True)
        is_staff = staff_member(interaction.user, cfg)
        if not is_staff and record.get("creator_id") != interaction.user.id:
            return await interaction.response.send_message("Only the ticket owner or support staff can close this ticket.", ephemeral=True)
        update_ticket(interaction.guild.id, interaction.channel_id, "status", "closed")
        update_ticket(interaction.guild.id, interaction.channel_id, "close_reason", "Closed by ticket owner")
        await interaction.response.send_message("Ticket closed. This channel will be deleted shortly.")
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")

@bot.tree.command(name="welcomesetup", description="Configure automatic welcome messages for new members")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Text channel where new members should be welcomed")
async def welcomesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        return await interaction.response.send_message("Run this command inside your server.", ephemeral=True)
    if channel.guild.id != interaction.guild.id:
        return await interaction.response.send_message("Choose a channel from this server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(interaction.guild.id), {})
    cfg["welcome_channel_id"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ **Welcome messages configured!** New members will receive the Oman Air welcome embed in {channel.mention}.",
        ephemeral=True
    )

@bot.event
async def on_member_join(member: discord.Member):
    try:
        # Automatically assign ECO and MEMBERS roles to new members in the main server.
        if member.guild.id == MAIN_GUILD_ID:
            for role_name in ("ECO", "MEMBERS"):
                role = discord.utils.get(member.guild.roles, name=role_name)
                if role is None:
                    log.error("%s role was not found in main guild %s", role_name, member.guild.id)
                    continue
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Automatic new-member roles")
                        log.info("Assigned %s role to new member %s (%s)", role_name, member, member.id)
                    except (discord.Forbidden, discord.HTTPException):
                        log.exception("Could not assign %s role to new member %s (%s)", role_name, member, member.id)

        cfg = load_data().get(str(member.guild.id), {})
        channel_id = cfg.get("welcome_channel_id")
        if not channel_id:
            log.info("No welcome channel configured for guild %s", member.guild.id)
            return

        channel = member.guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await member.guild.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.exception("Unable to resolve welcome channel %s in guild %s", channel_id, member.guild.id)
                return

        if not isinstance(channel, discord.TextChannel):
            log.error("Configured welcome destination %s is not a text channel.", channel_id)
            return

        embed = discord.Embed(
            title="✈️ Welcome Aboard Oman Air!",
            description=(
                f"Hello {member.mention}, and welcome to **{member.guild.name}**!\n\n"
                "We're delighted to have you join our virtual aviation community. "
                "Please review the server rules, explore the channels, and get ready for your journey.\n\n"
                "Need help with bookings or the community? Open a support ticket and our team will assist you."
            ),
            color=discord.Color.from_rgb(0, 45, 90),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="👤 New Member", value=member.mention, inline=True)
        embed.add_field(name="🛫 Member Number", value=str(member.guild.member_count or "—"), inline=True)
        embed.add_field(name="📌 Getting Started", value="Read the rules • Check announcements • Say hello", inline=False)
        if member.guild.icon:
            embed.set_author(name="Oman Air • Virtual Aviation", icon_url=member.guild.icon.url)
        embed.set_footer(text="Oman Air • Virtual Aviation Community")

        await channel.send(
            content=f"Welcome {member.mention}!",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
        )
        log.info("Welcome message sent for member %s (%s) in guild %s", member, member.id, member.guild.id)
    except discord.Forbidden:
        log.error("Missing permission to send welcome message in configured channel for guild %s", member.guild.id)
    except discord.HTTPException:
        log.exception("Discord API error while sending welcome message for member %s", member.id)
# ---------------- Moderation system ----------------

def is_moderator(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages or perms.moderate_members or perms.kick_members or perms.ban_members

async def require_moderator(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this command inside the server.", ephemeral=True)
        return False
    if not is_moderator(interaction.user):
        await interaction.response.send_message("You need a moderation permission to use this command.", ephemeral=True)
        return False
    return True

def get_warnings(guild_id: int, user_id: int):
    data = load_data()
    return data.get(str(guild_id), {}).get("warnings", {}).get(str(user_id), [])

def save_warning(guild_id: int, user_id: int, entry: dict):
    data = load_data()
    guild_cfg = data.setdefault(str(guild_id), {})
    warnings = guild_cfg.setdefault("warnings", {}).setdefault(str(user_id), [])
    warnings.append(entry)
    save_data(data)
    return len(warnings)

async def send_modlog(guild: discord.Guild, title: str, description: str, moderator: discord.Member):
    cfg = guild_data(guild.id)
    channel_id = cfg.get("modlog_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    embed = discord.Embed(title=f"🛡️ {title}", description=description, color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="Moderator", value=moderator.mention, inline=True)
    embed.timestamp = discord.utils.utcnow()
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("Cannot send moderation log in channel %s", channel.id)

@bot.tree.command(name="modlogsetup", description="Set the channel for moderation logs")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel for moderation action logs")
async def modlogsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    data = load_data()
    cfg = data.setdefault(str(interaction.guild_id), {})
    cfg["modlog_channel_id"] = channel.id
    save_data(data)
    await interaction.response.send_message(f"Moderation logs will be posted in {channel.mention}.", ephemeral=True)

@bot.tree.command(name="warn", description="Issue a warning to a member")
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(member="Member to warn", reason="Reason for the warning")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await require_moderator(interaction): return
    if member.id == interaction.user.id:
        return await interaction.response.send_message("You cannot warn yourself.", ephemeral=True)
    count = save_warning(interaction.guild.id, member.id, {
        "reason": reason[:500], "moderator_id": interaction.user.id,
        "timestamp": discord.utils.utcnow().isoformat()
    })
    embed = discord.Embed(title="⚠️ Member Warning", description=f"{member.mention} has been warned.", color=discord.Color.orange())
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    embed.add_field(name="Total warnings", value=str(count), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await send_modlog(interaction.guild, "Member Warned", f"Member: {member.mention}\nReason: {reason}\nWarnings: {count}", interaction.user)
    try:
        await member.send(f"You received a warning in **{interaction.guild.name}**.\nReason: {reason}")
    except discord.Forbidden:
        pass

@bot.tree.command(name="warnings", description="View a member's warnings")
@app_commands.default_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    if not await require_moderator(interaction): return
    entries = get_warnings(interaction.guild.id, member.id)
    if not entries:
        return await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
    lines = [f"**{i}.** {entry.get('reason', 'No reason')} — <@{entry.get('moderator_id', 0)}> — {entry.get('timestamp', '')[:10]}" for i, entry in enumerate(entries, 1)]
    embed = discord.Embed(title=f"Warnings for {member}", description="\n".join(lines)[:4000], color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clearwarnings", description="Clear all warnings for a member")
@app_commands.default_permissions(moderate_members=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    if not await require_moderator(interaction): return
    data = load_data()
    guild_cfg = data.setdefault(str(interaction.guild.id), {})
    user_warnings = guild_cfg.setdefault("warnings", {})
    count = len(user_warnings.get(str(member.id), []))
    user_warnings.pop(str(member.id), None)
    save_data(data)
    await interaction.response.send_message(f"Cleared {count} warning(s) for {member.mention}.", ephemeral=True)
    await send_modlog(interaction.guild, "Warnings Cleared", f"Member: {member.mention}\nCount cleared: {count}", interaction.user)

@bot.tree.command(name="timeout", description="Timeout a member for a number of minutes")
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(member="Member to timeout", minutes="Duration in minutes (1–40320)", reason="Reason")
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str):
    if not await require_moderator(interaction): return
    if member.id == interaction.user.id or member.guild_permissions.administrator:
        return await interaction.response.send_message("You cannot timeout yourself or an administrator.", ephemeral=True)
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"Timed out {member.mention} for {minutes} minute(s).", ephemeral=True)
    await send_modlog(interaction.guild, "Member Timed Out", f"Member: {member.mention}\nDuration: {minutes} minutes\nReason: {reason}", interaction.user)
    try:
        await member.send(f"You were timed out in **{interaction.guild.name}** for {minutes} minute(s).\nReason: {reason}")
    except discord.Forbidden:
        pass

@bot.tree.command(name="untimeout", description="Remove a member's timeout")
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(member="Member to remove timeout from", reason="Reason")
async def untimeout(interaction: discord.Interaction, member: discord.Member, reason: str = "Timeout removed by staff"):
    if not await require_moderator(interaction): return
    await member.timeout(None, reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"Removed timeout for {member.mention}.", ephemeral=True)
    await send_modlog(interaction.guild, "Timeout Removed", f"Member: {member.mention}\nReason: {reason}", interaction.user)

@bot.tree.command(name="kick", description="Kick a member from the server")
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await require_moderator(interaction): return
    if member.id == interaction.user.id or member.guild_permissions.administrator:
        return await interaction.response.send_message("You cannot kick yourself or an administrator.", ephemeral=True)
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(f"Kicked {member.mention}.", ephemeral=True)
    await send_modlog(interaction.guild, "Member Kicked", f"Member: {member.mention}\nReason: {reason}", interaction.user)

@bot.tree.command(name="ban", description="Ban a member from the server")
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(member="Member to ban", reason="Reason")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await require_moderator(interaction): return
    if member.id == interaction.user.id or member.guild_permissions.administrator:
        return await interaction.response.send_message("You cannot ban yourself or an administrator.", ephemeral=True)
    await member.ban(reason=f"{interaction.user}: {reason}", delete_message_seconds=0)
    await interaction.response.send_message(f"Banned {member.mention}.", ephemeral=True)
    await send_modlog(interaction.guild, "Member Banned", f"Member: {member.mention}\nReason: {reason}", interaction.user)

@bot.tree.command(name="unban", description="Unban a user by their Discord ID")
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(user_id="Discord user ID", reason="Reason")
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "Unban requested by staff"):
    if not await require_moderator(interaction): return
    try:
        target = discord.Object(id=int(user_id.strip()))
        await interaction.guild.unban(target, reason=f"{interaction.user}: {reason}")
    except (ValueError, discord.NotFound):
        return await interaction.response.send_message("That user ID is invalid or not currently banned.", ephemeral=True)
    await interaction.response.send_message(f"Unbanned user ID {user_id}.", ephemeral=True)
    await send_modlog(interaction.guild, "Member Unbanned", f"User ID: {user_id}\nReason: {reason}", interaction.user)

@bot.tree.command(name="purge", description="Delete recent messages in this channel")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(amount="Number of messages to delete (1–100)")
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not await require_moderator(interaction): return
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this command in a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount, reason=f"Purged by {interaction.user}")
    await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)
    await send_modlog(interaction.guild, "Messages Purged", f"Channel: {interaction.channel.mention}\nDeleted: {len(deleted)} message(s)", interaction.user)

@bot.tree.command(name="ticketsetup", description="Configure the support ticket system")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(staff_role="Role that can manage tickets", category="Category where tickets are created")
async def ticketsetup(interaction: discord.Interaction, staff_role: discord.Role, category: discord.CategoryChannel):
    data = load_data()
    cfg = data.setdefault(str(interaction.guild_id), {})
    cfg["staff_role_id"] = staff_role.id
    cfg["category_id"] = category.id
    cfg.setdefault("counter", 0)
    cfg.setdefault("tickets", {})
    save_data(data)
    await interaction.response.send_message(f"Ticket system configured. Staff role: {staff_role.mention} | Category: {category.name}", ephemeral=True)

@bot.tree.command(name="ticketpanel", description="Post the expanded support ticket panel")
@app_commands.default_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    cfg = guild_data(interaction.guild_id)
    if not cfg.get("staff_role_id") or not cfg.get("category_id"):
        return await interaction.response.send_message("Run /ticketsetup first.", ephemeral=True)
    embed = discord.Embed(title="✈️ Oman Air | Customer Support Center", description=(
        "Welcome to the Oman Air Support Center. Our team can assist with flight-management questions, booking support, account concerns, and server matters.\n\n"
        "**Before opening a ticket**\n• Check whether your question has already been answered.\n• Explain your issue clearly.\n• Keep support conversations in your private ticket.\n• Never share passwords, bot tokens, or confidential information.\n\n"
        "Press **Open Support Ticket** to create a private channel visible to you and the support team."
    ), color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="🎫 Ticket process", value="1. Open a ticket\n2. Explain your request\n3. Staff assists you\n4. Close when resolved", inline=False)
    embed.add_field(name="🧑‍✈️ Staff tools", value="Staff can claim, unclaim, assign, transfer, adjust priority, add or remove members, send embedded replies, and close tickets with a reason.", inline=False)
    embed.add_field(name="🔐 Privacy", value="Ticket channels are restricted to the ticket creator and support staff. Avoid posting sensitive personal information.", inline=False)
    embed.set_footer(text="Oman Air Support • Virtual server support system")
    await interaction.channel.send(embed=embed, view=TicketPanel())
    await interaction.response.send_message("Expanded support panel posted.", ephemeral=True)

@bot.tree.command(name="ticketstats", description="Staff: view support ticket statistics")
async def ticketstats(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view stats.", ephemeral=True)
    tickets = cfg.get("tickets", {})
    embed = discord.Embed(title="📊 Oman Air Support Statistics", color=discord.Color.blue())
    embed.add_field(name="Total", value=str(len(tickets)), inline=True)
    embed.add_field(name="Open", value=str(sum(t.get("status") == "open" for t in tickets.values())), inline=True)
    embed.add_field(name="Closed", value=str(sum(t.get("status") == "closed" for t in tickets.values())), inline=True)
    embed.add_field(name="Claimed", value=str(sum(bool(t.get("claimed_by")) for t in tickets.values())), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ---------------- Professional flight announcement system ----------------

flight_group = app_commands.Group(name="flight", description="Manage Oman Air flight announcements")

aircraft_group = app_commands.Group(name="aircraft", description="Manage the Oman Air virtual aircraft fleet")

crew_group = app_commands.Group(name="crew", description="Manage Oman Air virtual crew members")

def crew_store(cfg: dict) -> dict:
    return cfg.setdefault("crew", {})

@crew_group.command(name="add", description="Register a crew member in the staff roster")
@app_commands.describe(member="Discord member to register", role="Crew position, e.g. Pilot, Cabin Crew, Dispatcher", employee_id="Optional virtual employee ID")
async def crew_add(interaction: discord.Interaction, member: discord.Member, role: str, employee_id: str = None):
    if not await require_flight_staff(interaction):
        return
    role = role.strip()
    employee_id = (employee_id or "").strip().upper()
    if not role or len(role) > 60 or len(employee_id) > 24:
        return await interaction.response.send_message("Enter a crew position (1–60 characters) and an employee ID up to 24 characters.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    roster = crew_store(cfg)
    member_key = str(member.id)
    if member_key in roster:
        return await interaction.response.send_message(f"{member.mention} is already registered. Use /crew profile to view their record.", ephemeral=True)
    if employee_id and any(str(record.get("employee_id", "")).upper() == employee_id for record in roster.values()):
        return await interaction.response.send_message(f"Employee ID **{employee_id}** is already in use.", ephemeral=True)
    roster[member_key] = {"member_id": member.id, "display_name": member.display_name, "role": role, "employee_id": employee_id or "Not assigned", "availability": "Available", "assigned_flights": [], "registered_by": interaction.user.id, "registered_at": discord.utils.utcnow().isoformat()}
    save_data(data)
    await interaction.response.send_message(f"Registered {member.mention} as **{role}**. Employee ID: **{employee_id or 'Not assigned'}**.", ephemeral=True)

@crew_group.command(name="list", description="View the registered crew roster")
async def crew_list(interaction: discord.Interaction):
    if not await require_flight_staff(interaction):
        return
    roster = load_data().get(str(STAFF_GUILD_ID), {}).get("crew", {})
    if not roster:
        return await interaction.response.send_message("The crew roster is empty. Use /crew add to register crew.", ephemeral=True)
    embed = discord.Embed(title="✈️ Oman Air | Crew Roster", description=f"Registered crew: **{len(roster)}**", color=discord.Color.from_rgb(0, 45, 90))
    for record in list(roster.values())[:25]:
        embed.add_field(name=f"{record.get('display_name', 'Crew member')} • {record.get('role', 'Unassigned')}", value=f"Employee ID: {record.get('employee_id', 'Not assigned')}\nAvailability: {record.get('availability', 'Available')}\nDiscord: <@{record.get('member_id', 0)}>", inline=False)
    if len(roster) > 25:
        embed.set_footer(text=f"Showing 25 of {len(roster)} crew members.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@crew_group.command(name="profile", description="View a crew member's roster profile")
@app_commands.describe(member="Crew member to view")
async def crew_profile(interaction: discord.Interaction, member: discord.Member):
    if not await require_flight_staff(interaction):
        return
    record = load_data().get(str(STAFF_GUILD_ID), {}).get("crew", {}).get(str(member.id))
    if not record:
        return await interaction.response.send_message(f"{member.mention} is not registered in the crew roster.", ephemeral=True)
    embed = discord.Embed(title=f"✈️ Crew Profile | {record.get('display_name', member.display_name)}", color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="Position", value=record.get("role", "Unassigned"), inline=True)
    embed.add_field(name="Employee ID", value=record.get("employee_id", "Not assigned"), inline=True)
    embed.add_field(name="Availability", value=record.get("availability", "Available"), inline=True)
    embed.add_field(name="Assigned flights", value=", ".join(record.get("assigned_flights", [])) or "None", inline=False)
    embed.add_field(name="Registered", value=record.get("registered_at", "Unknown")[:10], inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


def aircraft_store(cfg: dict) -> dict:
    return cfg.setdefault("aircraft", {})

async def require_flight_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID:
        await interaction.response.send_message("Use this command in the Oman Air staff server.", ephemeral=True)
        return False
    if not isinstance(interaction.user, discord.Member) or not (
        interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild
    ):
        await interaction.response.send_message("You need Administrator or Manage Server permission to manage the fleet.", ephemeral=True)
        return False
    return True

@aircraft_group.command(name="add", description="Register an aircraft in the virtual fleet")
@app_commands.describe(registration="Unique aircraft registration, e.g. A4O-BA", model="Aircraft model", capacity="Passenger seat capacity", routes="Allowed routes separated by commas, e.g. MCT-LHR, MCT-DXB")
async def aircraft_add(interaction: discord.Interaction, registration: str, model: str, capacity: app_commands.Range[int, 1, 1000], routes: str = "Not specified"):
    if not await require_flight_staff(interaction):
        return
    registration = registration.strip().upper()
    model = model.strip()
    if not registration or len(registration) > 16 or not model or len(model) > 100:
        return await interaction.response.send_message("Enter a registration (1–16 characters) and model (1–100 characters).", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    fleet = aircraft_store(cfg)
    if registration in fleet:
        return await interaction.response.send_message(f"Aircraft **{registration}** is already registered.", ephemeral=True)
    fleet[registration] = {
        "registration": registration, "model": model, "capacity": capacity,
        "routes": [route.strip().upper() for route in routes.split(",") if route.strip()][:20] if routes.strip().lower() != "not specified" else [],
        "status": "Available", "created_by": interaction.user.id,
        "created_at": discord.utils.utcnow().isoformat()
    }
    save_data(data)
    await interaction.response.send_message(f"Registered **{registration}** — **{model}**, capacity **{capacity}**. Status: **Available**.", ephemeral=True)

@aircraft_group.command(name="list", description="Publish the aircraft fleet in a channel for everyone to view")
@app_commands.describe(channel="Channel in the main server where the fleet list will be posted")
async def aircraft_list(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use this command in the Oman Air main server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (
        interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild
    ):
        return await interaction.response.send_message("You need Administrator or Manage Server permission in the main server to publish the fleet.", ephemeral=True)
    if channel.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Choose a text channel in the Oman Air main server.", ephemeral=True)
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.send_messages or not permissions.embed_links:
        return await interaction.response.send_message("I need Send Messages and Embed Links permissions in that channel.", ephemeral=True)

    data = load_data()
    fleet = data.setdefault(str(STAFF_GUILD_ID), {}).get("aircraft", {})
    if not fleet:
        return await interaction.response.send_message("No aircraft are registered yet. Fleet staff must use /aircraft add in the staff server.", ephemeral=True)

    aircraft_items = list(fleet.values())
    model_counts = {}
    for aircraft in aircraft_items:
        model = aircraft.get("model", "Unspecified model")
        model_counts[model] = model_counts.get(model, 0) + 1
    model_summary = "\n".join(
        f"• **{model}** — {count} aircraft" for model, count in sorted(model_counts.items())
    )
    embed = discord.Embed(
        title="✈️ Oman Air | Official Virtual Aircraft Fleet",
        description=f"**Total aircraft:** {len(aircraft_items)}\n**Aircraft by model:**\n{model_summary}",
        color=discord.Color.from_rgb(0, 45, 90),
        timestamp=discord.utils.utcnow()
    )
    for aircraft in aircraft_items[:24]:
        embed.add_field(
            name=f"{aircraft.get('registration', 'Unknown')} • {aircraft.get('status', 'Unknown')}",
            value=f"**Model:** {aircraft.get('model', 'Not set')}\n**Seat capacity:** {aircraft.get('capacity', 'Not set')}\n**Routes:** {', '.join(aircraft.get('routes', [])) or 'Not specified'}",
            inline=False
        )
    if len(aircraft_items) > 24:
        embed.set_footer(text=f"Showing 24 of {len(aircraft_items)} aircraft. Contact fleet staff for the complete register.")
    else:
        embed.set_footer(text="Oman Air • Virtual Fleet Register")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        return await interaction.response.send_message("I cannot post in that channel. Check my channel permissions.", ephemeral=True)
    except discord.HTTPException:
        log.exception("Could not publish aircraft fleet in channel %s", channel.id)
        return await interaction.response.send_message("Discord could not publish the fleet list. Please try again.", ephemeral=True)
    await interaction.response.send_message(f"Published the public aircraft fleet in {channel.mention}.", ephemeral=True)

@aircraft_group.command(name="update", description="Update an aircraft's operational status or seat capacity")
@app_commands.describe(registration="Aircraft registration", status="New operational status", capacity="New seat capacity (optional)")
async def aircraft_update(interaction: discord.Interaction, registration: str, status: str, capacity: app_commands.Range[int, 1, 1000] = None):
    if not await require_flight_staff(interaction):
        return
    key = registration.strip().upper()
    allowed = {"Available", "Maintenance", "Out of Service"}
    normalized = status.strip().title()
    if normalized not in allowed:
        return await interaction.response.send_message("Status must be Available, Maintenance, or Out of Service.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    aircraft = aircraft_store(cfg).get(key)
    if not aircraft:
        return await interaction.response.send_message(f"Aircraft **{key}** was not found.", ephemeral=True)
    if normalized != "Available":
        assigned = [f.get("flight_number") for f in cfg.get("flights", {}).values()
                    if f.get("aircraft", "").strip().upper() == key
                    and f.get("status") not in ("Cancelled", "Arrived")]
        if assigned:
            return await interaction.response.send_message(
                f"Cannot mark **{key}** unavailable while assigned to active flight(s): {', '.join(assigned[:10])}. Reassign those flights first.",
                ephemeral=True
            )
    aircraft["status"] = normalized
    if capacity is not None:
        aircraft["capacity"] = capacity
    aircraft["updated_at"] = discord.utils.utcnow().isoformat()
    aircraft["updated_by"] = interaction.user.id
    save_data(data)
    await interaction.response.send_message(f"Updated **{key}**: status **{normalized}**, capacity **{aircraft.get('capacity')}**.", ephemeral=True)

@aircraft_group.command(name="routes", description="Set the routes an aircraft is approved for")
@app_commands.describe(registration="Aircraft registration", routes="Comma-separated routes, e.g. MCT-LHR, MCT-DXB")
async def aircraft_routes(interaction: discord.Interaction, registration: str, routes: str):
    if not await require_flight_staff(interaction):
        return
    key = registration.strip().upper()
    parsed_routes = [route.strip().upper() for route in routes.split(",") if route.strip()]
    if len(parsed_routes) > 20 or any(len(route) > 40 for route in parsed_routes):
        return await interaction.response.send_message("Enter up to 20 routes, each no longer than 40 characters.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    aircraft = aircraft_store(cfg).get(key)
    if not aircraft:
        return await interaction.response.send_message(f"Aircraft **{key}** was not found.", ephemeral=True)
    aircraft["routes"] = parsed_routes
    aircraft["updated_at"] = discord.utils.utcnow().isoformat()
    aircraft["updated_by"] = interaction.user.id
    save_data(data)
    await interaction.response.send_message(f"Updated routes for **{key}**: {', '.join(parsed_routes) if parsed_routes else 'No routes listed'}.", ephemeral=True)

@aircraft_group.command(name="assign", description="Assign an available aircraft to an existing flight")
@app_commands.describe(flight_number="Flight to assign", registration="Registered aircraft")
async def aircraft_assign(interaction: discord.Interaction, flight_number: str, registration: str):
    if not await require_flight_staff(interaction):
        return
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    flight_key = flight_number.strip().upper()
    registration_key = registration.strip().upper()
    flight = cfg.setdefault("flights", {}).get(flight_key)
    aircraft = aircraft_store(cfg).get(registration_key)
    if not flight:
        return await interaction.response.send_message(f"Flight **{flight_key}** was not found.", ephemeral=True)
    if not aircraft:
        return await interaction.response.send_message(f"Aircraft **{registration_key}** is not registered.", ephemeral=True)
    if flight.get("status") in ("Cancelled", "Departed", "Arrived"):
        return await interaction.response.send_message("Aircraft cannot be assigned to a cancelled or departed flight.", ephemeral=True)
    if aircraft.get("status") != "Available":
        return await interaction.response.send_message(f"Aircraft **{registration_key}** is currently **{aircraft.get('status')}**.", ephemeral=True)
    allowed_routes = aircraft.get("routes", [])
    if allowed_routes:
        actual_route = f"{flight.get('origin', '').strip().upper()}-{flight.get('destination', '').strip().upper()}"
        if actual_route not in allowed_routes:
            return await interaction.response.send_message(f"Route **{actual_route}** is not listed for aircraft **{registration_key}**. Allowed routes: {', '.join(allowed_routes)}.", ephemeral=True)
    conflict = next((f for f in cfg.get("flights", {}).values()
                     if f.get("flight_number") != flight_key
                     and f.get("aircraft", "").strip().upper() == registration_key
                     and f.get("status") not in ("Cancelled", "Arrived")), None)
    if conflict:
        return await interaction.response.send_message(f"Aircraft **{registration_key}** is already assigned to **{conflict.get('flight_number')}**.", ephemeral=True)
    flight["aircraft"] = registration_key
    flight["capacity"] = int(aircraft.get("capacity", 180))
    flight["aircraft_model"] = aircraft.get("model", "Unknown")
    flight["updated_at"] = discord.utils.utcnow().isoformat()
    flight["updated_by"] = interaction.user.id
    save_data(data)
    await interaction.response.send_message(
        f"Assigned **{registration_key}** ({aircraft.get('model')}) to **{flight_key}**. Booking capacity set to **{flight['capacity']}**.",
        ephemeral=True
    )

@aircraft_group.command(name="remove", description="Remove an aircraft from the virtual fleet")
@app_commands.describe(registration="Aircraft registration to remove")
async def aircraft_remove(interaction: discord.Interaction, registration: str):
    if not await require_flight_staff(interaction):
        return
    key = registration.strip().upper()
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    fleet = aircraft_store(cfg)
    if key not in fleet:
        return await interaction.response.send_message(f"Aircraft **{key}** was not found.", ephemeral=True)
    assigned = [f.get("flight_number") for f in cfg.get("flights", {}).values()
                if f.get("aircraft", "").strip().upper() == key
                and f.get("status") not in ("Cancelled", "Arrived")]
    if assigned:
        return await interaction.response.send_message(
            f"Cannot remove **{key}** while assigned to active flight(s): {', '.join(assigned[:10])}. Reassign them first.",
            ephemeral=True
        )
    del fleet[key]
    save_data(data)
    await interaction.response.send_message(f"Removed **{key}** from the virtual fleet.", ephemeral=True)


def save_guild_config(guild_id: int, key: str, value):
    data = load_data()
    cfg = data.setdefault(str(guild_id), {})
    cfg[key] = value
    save_data(data)
    return cfg

def flight_embed(guild: discord.Guild, flight: dict, title: str = "✈️ OMAN AIR | FLIGHT ANNOUNCEMENT"):
    embed = discord.Embed(
        title=title,
        description=f"**{flight['flight_number']}**\n{flight['origin']}  ➜  {flight['destination']}",
        color=discord.Color.from_rgb(0, 45, 90)
    )
    embed.add_field(name="🧳 Check-in", value=flight.get("check_in") or "To be announced", inline=True)
    embed.add_field(name="🛫 Departure", value=flight["departure"], inline=True)
    embed.add_field(name="🛬 Arrival", value=flight["arrival"], inline=True)
    embed.add_field(name="🛩️ Aircraft", value=flight["aircraft"], inline=True)
    embed.add_field(name="🚪 Gate", value=flight.get("gate") or "To be announced", inline=True)
    embed.add_field(name="📌 Flight Status", value=flight.get("status", "Scheduled"), inline=True)
    embed.add_field(name="📢 Important", value="Please arrive early and monitor this channel for operational updates.", inline=False)
    embed.set_footer(text=f"{guild.name} • Oman Air Virtual Operations")
    embed.timestamp = discord.utils.utcnow()
    return embed

class BookFlightView(discord.ui.View):
    def __init__(self, flight_number: str):
        super().__init__(timeout=None)
        self.flight_number = flight_number.upper()
        self.book_button.custom_id = f"oman_book_flight:{self.flight_number}"

    @discord.ui.button(label="Book Flight", emoji="🎟️", style=discord.ButtonStyle.success)
    async def book_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_flight_booking(interaction, self.flight_number)


async def post_flight_announcement(guild: discord.Guild, flight: dict):
    main_guild = bot.get_guild(MAIN_GUILD_ID)
    channel = main_guild.get_channel(MAIN_ANNOUNCEMENT_CHANNEL_ID) if main_guild else None
    if not isinstance(channel, discord.TextChannel):
        return False
    await channel.send(content="@everyone", embed=flight_embed(main_guild, flight), view=BookFlightView(flight["flight_number"]), allowed_mentions=discord.AllowedMentions(everyone=True))
    return True

@bot.tree.command(name="clear", description="Staff: clear recent messages from this channel")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(amount="Number of recent messages to delete (1–100)")
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in a server channel.", ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("You need the Manage Messages permission to use this command.", ephemeral=True)
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("This command only works in text channels.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
    except discord.Forbidden:
        return await interaction.followup.send("I need the Manage Messages permission to delete messages.", ephemeral=True)
    except discord.HTTPException:
        return await interaction.followup.send("Discord could not delete the messages. Please try again.", ephemeral=True)
    await interaction.followup.send(f"🧹 Cleared **{len(deleted)}** messages.", ephemeral=True)


@bot.tree.command(name="flightsetup", description="Configure the flight announcement channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel for official flight announcements")
async def flightsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild_id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Run this setup command in the staff server.", ephemeral=True)
    if channel.guild.id != MAIN_GUILD_ID or channel.id != MAIN_ANNOUNCEMENT_CHANNEL_ID:
        return await interaction.response.send_message("The announcement channel is fixed to the configured main-server channel.", ephemeral=True)
    await interaction.response.send_message(f"Main-server flight announcements are configured for {channel.mention}.", ephemeral=True)

@flight_group.command(name="create", description="Create and publish a professional flight announcement")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    flight_number="Official flight number, e.g. WY201",
    origin="Departure airport/city",
    destination="Arrival airport/city",
    departure="Departure date and time, include timezone (e.g. 12 Nov 2026, 14:30 UTC)",
    arrival="Arrival date and time, include timezone",
    aircraft="Aircraft type",
    check_in="Check-in opening/closing date and time, include timezone (e.g. 12 Nov 2026, 12:30–13:30 UTC)",
    gate="Departure gate or TBA"
)
async def flight_create(interaction: discord.Interaction, flight_number: str, origin: str, destination: str, departure: str, arrival: str, aircraft: str, check_in: str = "TBA", gate: str = "TBA"):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    if interaction.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Flight management is only available in the staff server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    flights = cfg.setdefault("flights", {})
    flight_number = flight_number.strip().upper()
    if flight_number in flights and flights[flight_number].get("status") not in ("Cancelled", "Completed"):
        return await interaction.response.send_message(f"{flight_number} already exists. Use /flightupdate to update it.", ephemeral=True)
    flight = {
        "flight_number": flight_number,
        "origin": origin.strip()[:100],
        "destination": destination.strip()[:100],
        "departure": departure.strip()[:100],
        "arrival": arrival.strip()[:100],
        "check_in": check_in.strip()[:100],
        "aircraft": aircraft.strip()[:100],
        "gate": gate.strip()[:50],
        "status": "Scheduled",
        "created_by": interaction.user.id,
        "created_at": discord.utils.utcnow().isoformat()
    }
    flights[flight_number] = flight
    save_data(data)
    if not await post_flight_announcement(interaction.guild, flight):
        return await interaction.response.send_message("Flight saved, but no announcement channel is configured. Run /flightsetup first.", ephemeral=True)
    await interaction.response.send_message(f"Published the announcement for **{flight_number}**.", ephemeral=True)

@bot.tree.command(name="flightupdate", description="Update a flight's status or gate and publish an update")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(flight_number="Flight to update", status="New status", gate="New gate (optional)")
async def flightupdate(interaction: discord.Interaction, flight_number: str, status: str, gate: str = ""):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    flights = cfg.setdefault("flights", {})
    key = flight_number.strip().upper()
    flight = flights.get(key)
    if not flight:
        return await interaction.response.send_message(f"Flight **{key}** was not found.", ephemeral=True)
    allowed_statuses = ["Scheduled", "Boarding", "Delayed", "Departed", "Arrived", "Cancelled"]
    normalized = status.strip().title()
    if normalized not in allowed_statuses:
        return await interaction.response.send_message("Status must be Scheduled, Boarding, Delayed, Departed, Arrived, or Cancelled.", ephemeral=True)
    flight["status"] = normalized
    if gate.strip():
        flight["gate"] = gate.strip()[:50]
    flight["updated_at"] = discord.utils.utcnow().isoformat()
    flight["updated_by"] = interaction.user.id
    miles_awarded_now = 0
    if normalized == "Arrived":
        for booking in cfg.setdefault("bookings", {}).values():
            if booking.get("flight_number") == key and booking.get("status") != "Cancelled" and not booking.get("skymiles_awarded"):
                passenger_id = str(booking.get("passenger_id"))
                balances = cfg.setdefault("skymiles", {})
                balances[passenger_id] = int(balances.get(passenger_id, 0)) + 1000
                booking["skymiles_awarded"] = 1000
                booking["skymiles_awarded_at"] = discord.utils.utcnow().isoformat()
                cfg.setdefault("miles_transactions", []).append({
                    "member_id": int(passenger_id), "amount": 1000, "type": "Flight reward",
                    "description": f"{key} arrived • booking {booking.get('reference', 'N/A')}",
                    "timestamp": discord.utils.utcnow().isoformat()
                })
                miles_awarded_now += 1
    save_data(data)
    main_guild = bot.get_guild(MAIN_GUILD_ID)
    channel = main_guild.get_channel(MAIN_ANNOUNCEMENT_CHANNEL_ID) if main_guild else None
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Main-server announcement channel is unavailable. Check the bot is in both servers and can post there.", ephemeral=True)
    embed = flight_embed(main_guild, flight, title=f"📣 OMAN AIR | FLIGHT UPDATE — {key}")
    await channel.send(content="@everyone", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
    miles_note = f" Awarded 1,000 SkyMiles to {miles_awarded_now} passenger(s)." if miles_awarded_now else ""
    await interaction.response.send_message(f"Published update for **{key}**: **{normalized}**.{miles_note}", ephemeral=True)

@flight_group.command(name="list", description="List scheduled flights")
@app_commands.default_permissions(manage_guild=True)
async def flight_list(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    if interaction.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Flight management is only available in the staff server.", ephemeral=True)
    flights = operations_data().get("flights", {})
    active = [f for f in flights.values() if f.get("status") not in ("Cancelled", "Arrived")]
    if not active:
        return await interaction.response.send_message("There are no active flights recorded.", ephemeral=True)
    embed = discord.Embed(title="🗂️ Oman Air | Active Flight Schedule", color=discord.Color.from_rgb(0, 45, 90))
    for f in active[:25]:
        embed.add_field(name=f"{f['flight_number']} • {f['status']}", value=f"{f['origin']} ➜ {f['destination']}\nCheck-in: {f.get('check_in', 'TBA')}\nDeparture: {f['departure']}\nArrival: {f['arrival']}\nGate: {f.get('gate', 'TBA')}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@flight_group.command(name="cancel", description="Cancel a flight and publish a cancellation notice")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(flight_number="Flight to cancel", reason="Cancellation reason")
async def flight_cancel(interaction: discord.Interaction, flight_number: str, reason: str):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    flight = cfg.setdefault("flights", {}).get(flight_number.strip().upper())
    if not flight:
        return await interaction.response.send_message("That flight was not found.", ephemeral=True)
    flight["status"] = "Cancelled"
    flight["cancellation_reason"] = reason.strip()[:500]
    flight["updated_at"] = discord.utils.utcnow().isoformat()
    save_data(data)
    main_guild = bot.get_guild(MAIN_GUILD_ID)
    channel = main_guild.get_channel(MAIN_ANNOUNCEMENT_CHANNEL_ID) if main_guild else None
    if isinstance(channel, discord.TextChannel):
        embed = flight_embed(main_guild, flight, title=f"❌ OMAN AIR | FLIGHT CANCELLED — {flight['flight_number']}")
        embed.add_field(name="Cancellation reason", value=reason.strip()[:1024], inline=False)
        await channel.send(content="@everyone", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
    await interaction.response.send_message(f"Flight **{flight['flight_number']}** marked as cancelled.", ephemeral=True)

@bot.tree.command(name="announcement", description="Staff: post a professional Oman Air announcement in the main server")
@app_commands.describe(title="Announcement heading", message="Announcement details", channel="Any text channel in the main server (defaults to this channel)")
async def announcement(interaction: discord.Interaction, title: str, message: str, channel: discord.TextChannel = None):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Run this command in the main server and choose a channel there.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("This command is only available to server members.", ephemeral=True)

    staff_guild = bot.get_guild(STAFF_GUILD_ID)
    if staff_guild is None:
        return await interaction.response.send_message("The Oman Air staff server is unavailable to the bot.", ephemeral=True)
    try:
        staff_member_obj = staff_guild.get_member(interaction.user.id) or await staff_guild.fetch_member(interaction.user.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return await interaction.response.send_message("You must be a member of the Oman Air staff server to publish announcements.", ephemeral=True)

    staff_cfg = guild_data(STAFF_GUILD_ID)
    if not staff_member(staff_member_obj, staff_cfg):
        return await interaction.response.send_message("Only configured Oman Air staff can post announcements.", ephemeral=True)

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel) or target.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Choose any text channel in the main server.", ephemeral=True)

    title = title.strip()[:256]
    message = message.strip()[:4000]
    if not title or not message:
        return await interaction.response.send_message("The title and message cannot be empty.", ephemeral=True)

    main_guild = interaction.guild
    embed = discord.Embed(
        title=f"📢 {title}",
        description=message,
        color=discord.Color.from_rgb(0, 45, 90),
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name="OMAN AIR | OFFICIAL ANNOUNCEMENT", icon_url=main_guild.icon.url if main_guild.icon else discord.Embed.Empty)
    embed.set_footer(text=f"Oman Air • {main_guild.name}")
    try:
        await target.send(embed=embed)
    except discord.Forbidden:
        return await interaction.response.send_message("I cannot post in that channel. Check my channel permissions.", ephemeral=True)
    except discord.HTTPException:
        log.exception("Failed to send announcement to channel %s", target.id)
        return await interaction.response.send_message("Discord couldn't publish the announcement. Please try again.", ephemeral=True)
    await interaction.response.send_message(f"Professional announcement published in {target.mention}.", ephemeral=True)

# ---------------- Passenger bookings, check-in, boarding, dashboard and staff ranks ----------------

def booking_store(cfg: dict) -> dict:
    return cfg.setdefault("bookings", {})

def audit_store(cfg: dict) -> list:
    return cfg.setdefault("staff_activity_logs", [])

async def audit_log(guild: discord.Guild, actor: discord.abc.User, action: str, details: str):
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    audit_store(cfg).append({
        "actor_id": actor.id,
        "action": action[:100],
        "details": details[:900],
        "timestamp": discord.utils.utcnow().isoformat()
    })
    cfg["staff_activity_logs"] = cfg["staff_activity_logs"][-500:]
    save_data(data)
    log_channel_id = cfg.get("staff_log_channel_id")
    channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if isinstance(channel, discord.TextChannel):
        embed = discord.Embed(title="🧾 Oman Air | Staff Activity", description=f"**Action:** {action}\n**Details:** {details}", color=discord.Color.from_rgb(0, 45, 90), timestamp=discord.utils.utcnow())
        embed.add_field(name="Staff member", value=actor.mention, inline=True)
        embed.set_footer(text="Oman Air Staff Audit")
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Cannot post staff log in channel %s", log_channel_id)

def get_flight(cfg: dict, flight_number: str):
    return cfg.get("flights", {}).get(flight_number.strip().upper())

def get_booking(cfg: dict, reference: str):
    return cfg.get("bookings", {}).get(reference.strip().upper())

class FlightClassSelect(discord.ui.Select):
    def __init__(self, flight_number: str):
        self.flight_number = flight_number.upper()
        options = [
            discord.SelectOption(label="Economy Class", value="ECO", description="Requires the ECO role"),
            discord.SelectOption(label="Business Class", value="BUSINESS", description="Requires the BUSINESS role"),
            discord.SelectOption(label="First Class", value="FIRST", description="Requires the FIRST role"),
        ]
        super().__init__(placeholder="Choose your travel class…", min_values=1, max_values=1, options=options, custom_id=f"oman_flight_class:{self.flight_number}")

    async def callback(self, interaction: discord.Interaction):
        await process_flight_booking(interaction, self.flight_number, self.values[0])


class FlightClassSelectView(discord.ui.View):
    def __init__(self, flight_number: str):
        super().__init__(timeout=180)
        self.add_item(FlightClassSelect(flight_number))


async def process_flight_booking(interaction: discord.Interaction, flight_number: str, flight_class: str = None):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use this button or command in the server.", ephemeral=True)
    if flight_class is None:
        return await interaction.response.send_message("Choose your travel class. I will verify your Discord role before booking.", view=FlightClassSelectView(flight_number), ephemeral=True)
    if flight_class not in ("ECO", "BUSINESS", "FIRST"):
        return await interaction.response.send_message("That travel class is not valid. Please try booking again.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("I could not verify your server roles. Please try again inside the main server.", ephemeral=True)
    required_role = interaction.guild.get_role(next((role.id for role in interaction.guild.roles if role.name == flight_class), 0))
    if not required_role or required_role not in interaction.user.roles:
        return await interaction.response.send_message(f"❌ You selected **{flight_class} Class**, but you do not have the required **{flight_class}** role. Please choose a class matching your role.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    flight = get_flight(cfg, flight_number)
    if not flight:
        return await interaction.response.send_message("That flight was not found. Check the flight number.", ephemeral=True)
    if flight.get("status") in ("Cancelled", "Departed", "Arrived"):
        return await interaction.response.send_message("Bookings are not available for this flight right now.", ephemeral=True)
    bookings = booking_store(cfg)
    if flight.get("booking_open", True) is False:
        return await interaction.response.send_message("Bookings for this flight are currently closed.", ephemeral=True)
    existing = next((b for b in bookings.values() if b.get("flight_number") == flight["flight_number"] and b.get("passenger_id") == interaction.user.id and b.get("status") != "Cancelled"), None)
    if existing:
        return await interaction.response.send_message(f"You already booked this flight. Your reference is **{existing['reference']}**.", ephemeral=True)
    capacity = int(flight.get("capacity", 180))
    taken = sum(b.get("flight_number") == flight["flight_number"] and b.get("status") != "Cancelled" for b in bookings.values())
    if taken >= capacity:
        return await interaction.response.send_message("This flight is fully booked.", ephemeral=True)
    reference = ("WY" + uuid.uuid4().hex[:8]).upper()
    record = {"reference": reference, "flight_number": flight["flight_number"], "passenger_id": interaction.user.id, "status": "Booked", "class": flight_class, "booked_at": discord.utils.utcnow().isoformat(), "checked_in": False, "boarded": False}
    bookings[reference] = record
    save_data(data)
    embed = discord.Embed(title="🎟️ Oman Air | Booking Confirmation", description=f"**Booking reference:** {reference}\n**Passenger:** {interaction.user.mention}\n**Flight:** {flight['flight_number']}\n**Route:** {flight['origin']} ➜ {flight['destination']}", color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="Travel Class", value=flight_class, inline=True)
    embed.add_field(name="🧳 Check-in", value=flight.get("check_in", "TBA"), inline=False)
    embed.add_field(name="🛫 Departure", value=flight.get("departure", "TBA"), inline=True)
    embed.add_field(name="🛬 Arrival", value=flight.get("arrival", "TBA"), inline=True)
    embed.add_field(name="🚪 Gate", value=flight.get("gate", "TBA"), inline=True)
    embed.add_field(name="Status", value="Booked • Not checked in", inline=False)
    embed.set_footer(text="Keep your booking reference for check-in and boarding.")
    try:
        await interaction.user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return await interaction.response.send_message("Your seat is booked, but I couldn't DM you. Please enable DMs from this server. Here is your confirmation:", embed=embed, ephemeral=True)
    await interaction.response.send_message(f"✅ Booked **{flight['flight_number']}**! I've sent your confirmation and reference **{reference}** in a DM.", ephemeral=True)


@bot.tree.command(name="bookflight", description="Member: privately book a seat on an Oman Air flight")
@app_commands.describe(flight_number="Flight number to book")
async def bookflight(interaction: discord.Interaction, flight_number: str):
    await process_flight_booking(interaction, flight_number)

async def process_flight_checkin(interaction: discord.Interaction, flight_number: str):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use this button in the server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    flight = get_flight(cfg, flight_number)
    if not flight or not flight.get("checkin_open"):
        return await interaction.response.send_message("Check-in is not currently open for this flight.", ephemeral=True)
    booking = next((b for b in cfg.get("bookings", {}).values() if b.get("flight_number") == flight["flight_number"] and b.get("passenger_id") == interaction.user.id and b.get("status") != "Cancelled"), None)
    if not booking:
        return await interaction.response.send_message("I couldn't find a booking for you on this flight. You must book a seat first.", ephemeral=True)
    if booking.get("checked_in"):
        return await interaction.response.send_message(f"You are already checked in. Your reference is **{booking['reference']}**.", ephemeral=True)
    if flight.get("status") in ("Cancelled", "Departed", "Arrived"):
        return await interaction.response.send_message("Check-in is unavailable for this flight.", ephemeral=True)
    booking["checked_in"] = True
    booking["checked_in_at"] = discord.utils.utcnow().isoformat()
    booking["status"] = "Checked in"
    save_data(data)
    embed = discord.Embed(title="🛂 Oman Air | Digital Boarding Pass", description=f"**Passenger:** {interaction.user.mention}\n**Reference:** {booking['reference']}\n**Flight:** {flight['flight_number']}\n**Route:** {flight['origin']} ➜ {flight['destination']}", color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="🧳 Check-in", value=flight.get("check_in", "TBA"), inline=False)
    embed.add_field(name="🛫 Departure", value=flight.get("departure", "TBA"), inline=True)
    embed.add_field(name="🛬 Arrival", value=flight.get("arrival", "TBA"), inline=True)
    embed.add_field(name="🚪 Gate", value=flight.get("gate", "TBA"), inline=True)
    embed.add_field(name="Boarding status", value="CHECKED IN • Awaiting boarding", inline=False)
    embed.set_footer(text="Present this digital boarding pass to virtual airline staff.")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    try:
        await interaction.user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.info("Could not DM boarding pass to user %s", interaction.user.id)


class CheckInFlightView(discord.ui.View):
    def __init__(self, flight_number: str):
        super().__init__(timeout=None)
        self.flight_number = flight_number.upper()
        self.check_in_button.custom_id = f"oman_checkin_flight:{self.flight_number}"

    @discord.ui.button(label="Check In", emoji="🛂", style=discord.ButtonStyle.primary)
    async def check_in_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_flight_checkin(interaction, self.flight_number)


@bot.tree.command(name="checkinstart", description="Staff: announce check-in opening for a flight with a Check In button")
@app_commands.describe(flight_number="Flight number", channel="Channel for the check-in announcement (optional)")
async def checkinstart(interaction: discord.Interaction, flight_number: str, channel: discord.TextChannel = None):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured Oman Air staff can open check-in.", ephemeral=True)
    flight = get_flight(cfg, flight_number)
    if not flight:
        return await interaction.response.send_message("Flight not found. Check the flight number.", ephemeral=True)
    if flight.get("status") in ("Cancelled", "Departed", "Arrived"):
        return await interaction.response.send_message("Check-in cannot be opened for this flight status.", ephemeral=True)
    main_guild = bot.get_guild(MAIN_GUILD_ID)
    target = main_guild.get_channel(MAIN_ANNOUNCEMENT_CHANNEL_ID) if main_guild else None
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("The configured main-server announcement channel is unavailable.", ephemeral=True)
    flight["checkin_open"] = True
    flight["checkin_opened_at"] = discord.utils.utcnow().isoformat()
    data = load_data()
    data.setdefault(str(STAFF_GUILD_ID), {}).setdefault("flights", {})[flight["flight_number"]] = flight
    save_data(data)
    embed = discord.Embed(title="🛂 OMAN AIR | CHECK-IN NOW OPEN", description=f"Check-in has started for **{flight['flight_number']}**\n**Route:** {flight['origin']} ➜ {flight['destination']}\n**Check-in:** {flight.get('check_in', 'TBA')}\n**Departure:** {flight.get('departure', 'TBA')}\n\nPassengers who booked this flight, press **Check In** below. The bot will verify your booking automatically and privately issue your boarding pass.", color=discord.Color.from_rgb(0, 45, 90), timestamp=discord.utils.utcnow())
    embed.set_footer(text=f"{interaction.guild.name} • Oman Air Virtual Operations")
    await target.send(content="@everyone", embed=embed, view=CheckInFlightView(flight["flight_number"]), allowed_mentions=discord.AllowedMentions(everyone=True))
    await interaction.response.send_message(f"Check-in announcement posted in {target.mention} for **{flight['flight_number']}**.", ephemeral=True)


@bot.tree.command(name="manifest", description="Staff: view the passenger manifest for a flight")
@app_commands.describe(flight_number="Flight number")
async def manifest(interaction: discord.Interaction, flight_number: str):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view passenger manifests.", ephemeral=True)
    flight = get_flight(cfg, flight_number)
    if not flight:
        return await interaction.response.send_message("Flight not found.", ephemeral=True)
    passengers = [b for b in cfg.get("bookings", {}).values() if b.get("flight_number") == flight["flight_number"] and b.get("status") != "Cancelled"]
    lines = [f"• <@{b['passenger_id']}> — {b['reference']} — {'Boarded' if b.get('boarded') else 'Checked in' if b.get('checked_in') else 'Booked'}" for b in passengers]
    embed = discord.Embed(title=f"📋 Oman Air | Passenger Manifest — {flight['flight_number']}", description="\n".join(lines)[:4000] or "No passengers booked.", color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="Passenger count", value=f"{len(passengers)} / {int(flight.get('capacity', 180))}", inline=True)
    embed.add_field(name="Route", value=f"{flight['origin']} ➜ {flight['destination']}", inline=True)
    embed.set_footer(text="Confidential staff view")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="boardpassenger", description="Staff: mark a checked-in passenger as boarded")
@app_commands.describe(booking_reference="Passenger booking reference")
async def boardpassenger(interaction: discord.Interaction, booking_reference: str):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can manage boarding.", ephemeral=True)
    booking = get_booking(cfg, booking_reference)
    if not booking:
        return await interaction.response.send_message("Booking reference not found.", ephemeral=True)
    if not booking.get("checked_in"):
        return await interaction.response.send_message("Passenger must check in before boarding.", ephemeral=True)
    if booking.get("boarded"):
        return await interaction.response.send_message("Passenger is already marked as boarded.", ephemeral=True)
    booking["boarded"] = True
    booking["boarded_at"] = discord.utils.utcnow().isoformat()
    booking["status"] = "Boarded"
    data = load_data()
    data.setdefault(str(STAFF_GUILD_ID), {}).setdefault("bookings", {})[booking["reference"]] = booking
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "Passenger boarded", f"Flight {booking['flight_number']} • Booking {booking['reference']} • Passenger <@{booking['passenger_id']}>")
    await interaction.response.send_message(f"Passenger <@{booking['passenger_id']}> marked as boarded for **{booking['flight_number']}**.", ephemeral=True)

@bot.tree.command(name="mymiles", description="Member: view your Oman Air SkyMiles balance")
async def mymiles(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    cfg = operations_data()
    balance = int(cfg.get("skymiles", {}).get(str(interaction.user.id), 0))
    embed = discord.Embed(title="💳 Oman Air | SkyMiles", description=f"{interaction.user.mention}, you have **{balance:,} SkyMiles**.", color=discord.Color.from_rgb(0, 45, 90))
    embed.set_footer(text="Earn 1,000 SkyMiles when a flight you booked is marked Arrived.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="mileshistory", description="Member: view your recent SkyMiles transactions privately")
async def mileshistory(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    cfg = operations_data()
    entries = [t for t in cfg.get("miles_transactions", []) if int(t.get("member_id", 0)) == interaction.user.id]
    entries = entries[-10:][::-1]
    lines = [f"**{int(t.get('amount', 0)):+,} miles** • {t.get('type', 'Transaction')}\n{t.get('description', '—')}\n<t:{int(discord.utils.parse_time(t.get('timestamp')).timestamp())}:R>" for t in entries if discord.utils.parse_time(t.get("timestamp"))]
    embed = discord.Embed(title="🧾 Oman Air | SkyMiles History", description="\n\n".join(lines)[:4000] or "No transactions recorded yet.", color=discord.Color.from_rgb(0, 45, 90))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="redeemmiles", description="Member: redeem SkyMiles for virtual rewards")
@app_commands.describe(reward="Choose a reward to redeem")
@app_commands.choices(reward=[
    app_commands.Choice(name="5,000 miles — Lounge access", value="lounge"),
    app_commands.Choice(name="10,000 miles — Upgrade voucher", value="upgrade"),
    app_commands.Choice(name="25,000 miles — Award ticket voucher", value="ticket"),
])
async def redeemmiles(interaction: discord.Interaction, reward: app_commands.Choice[str]):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    rewards = {
        "lounge": (5000, "Virtual lounge access voucher"),
        "upgrade": (10000, "Virtual upgrade voucher"),
        "ticket": (25000, "Virtual award ticket voucher"),
    }
    cost, title = rewards[reward.value]
    data = load_data()
    cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    balances = cfg.setdefault("skymiles", {})
    member_key = str(interaction.user.id)
    balance = int(balances.get(member_key, 0))
    if balance < cost:
        return await interaction.response.send_message(f"You need **{cost - balance:,} more SkyMiles** for {title}.", ephemeral=True)
    balances[member_key] = balance - cost
    cfg.setdefault("miles_transactions", []).append({
        "member_id": interaction.user.id, "amount": -cost, "type": "Redemption",
        "description": title, "timestamp": discord.utils.utcnow().isoformat()
    })
    cfg.setdefault("miles_redemptions", []).append({
        "member_id": interaction.user.id, "reward": title, "cost": cost,
        "status": "Pending staff fulfilment", "redeemed_at": discord.utils.utcnow().isoformat()
    })
    save_data(data)
    embed = discord.Embed(title="🎁 SkyMiles Redemption Submitted", description=f"You redeemed **{cost:,} SkyMiles** for **{title}**.\n\nYour reward is **Pending staff fulfilment**. Contact Oman Air staff if needed.", color=discord.Color.from_rgb(0, 45, 90))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="milesredemptions", description="Staff: view recent SkyMiles reward redemptions")
async def milesredemptions(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the staff server.", ephemeral=True)
    cfg = operations_data()
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view redemptions.", ephemeral=True)
    entries = cfg.get("miles_redemptions", [])[-15:][::-1]
    lines = [f"<@{e.get('member_id')}> • **{e.get('reward')}** • {int(e.get('cost', 0)):,} miles • {e.get('status', 'Pending')}" for e in entries]
    embed = discord.Embed(title="🎁 Oman Air | SkyMiles Redemptions", description="\n".join(lines)[:4000] or "No redemptions yet.", color=discord.Color.from_rgb(0, 45, 90))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="milesadjust", description="Staff: add or remove SkyMiles from a passenger account")
@app_commands.describe(member="Passenger account", amount="Miles to add or remove (positive or negative)", reason="Reason for the adjustment")
async def milesadjust(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, -1000000, 1000000], reason: str):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured Oman Air staff can adjust SkyMiles.", ephemeral=True)
    balances = cfg.setdefault("skymiles", {})
    key = str(member.id)
    current = int(balances.get(key, 0))
    updated = current + amount
    if updated < 0:
        return await interaction.response.send_message("That adjustment would make the passenger's balance negative.", ephemeral=True)
    balances[key] = updated
    data = load_data()
    staff_cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    staff_cfg["skymiles"] = balances
    staff_cfg.setdefault("miles_transactions", []).append({
        "member_id": member.id, "amount": amount, "type": "Staff adjustment",
        "description": reason.strip()[:300], "actor_id": interaction.user.id,
        "timestamp": discord.utils.utcnow().isoformat()
    })
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "SkyMiles adjusted", f"{amount:+,} miles for {member} ({member.id}). Reason: {reason.strip()[:300]}")
    await interaction.response.send_message(f"Updated {member.mention}'s balance: **{updated:,} SkyMiles** ({amount:+,}).", ephemeral=True)


@bot.tree.command(name="flightdashboard", description="Staff: view the Oman Air flight operations dashboard")
async def flightdashboard(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view the operations dashboard.", ephemeral=True)
    flights = list(cfg.get("flights", {}).values())
    bookings = list(cfg.get("bookings", {}).values())
    active = [f for f in flights if f.get("status") not in ("Cancelled", "Arrived")]
    embed = discord.Embed(title="🖥️ Oman Air | Flight Operations Dashboard", color=discord.Color.from_rgb(0, 45, 90), timestamp=discord.utils.utcnow())
    embed.add_field(name="Active flights", value=str(len(active)), inline=True)
    embed.add_field(name="Total bookings", value=str(sum(b.get("status") != "Cancelled" for b in bookings)), inline=True)
    embed.add_field(name="Checked in", value=str(sum(bool(b.get("checked_in")) for b in bookings)), inline=True)
    embed.add_field(name="Boarded", value=str(sum(bool(b.get("boarded")) for b in bookings)), inline=True)
    for f in active[:20]:
        flight_bookings = [b for b in bookings if b.get("flight_number") == f["flight_number"] and b.get("status") != "Cancelled"]
        passenger_lines = [f"• <@{b.get('passenger_id')}> — {b.get('status', 'Booked')}" for b in flight_bookings if b.get("passenger_id")]
        passenger_text = "\n".join(passenger_lines) if passenger_lines else "No passengers booked yet."
        if len(passenger_text) > 850:
            shown_lines = []
            used = 0
            for line in passenger_lines:
                if used + len(line) + 1 > 800:
                    break
                shown_lines.append(line)
                used += len(line) + 1
            remaining = len(passenger_lines) - len(shown_lines)
            passenger_text = "\n".join(shown_lines) + f"\n…and {remaining} more passenger(s)." if remaining else "\n".join(shown_lines)
        dashboard_value = f"{f['origin']} ➜ {f['destination']}\nDeparture: {f.get('departure', 'TBA')} • Gate: {f.get('gate', 'TBA')}\nPassengers: {len(flight_bookings)}/{f.get('capacity', 180)}\n\n**Passenger names:**\n{passenger_text}"
        embed.add_field(name=f"{f['flight_number']} • {f.get('status', 'Scheduled')}", value=dashboard_value[:1024], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="flightbooking", description="Staff: open or close bookings for a flight")
@app_commands.describe(flight_number="Flight number", bookings_open="True to open bookings, false to close")
async def flightbooking(interaction: discord.Interaction, flight_number: str, bookings_open: bool):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can manage flight bookings.", ephemeral=True)
    flight = get_flight(cfg, flight_number)
    if not flight:
        return await interaction.response.send_message("Flight not found.", ephemeral=True)
    flight["booking_open"] = bookings_open
    data = load_data()
    data.setdefault(str(STAFF_GUILD_ID), {}).setdefault("flights", {})[flight["flight_number"]] = flight
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "Booking availability changed", f"{flight['flight_number']} bookings {'opened' if bookings_open else 'closed'}")
    await interaction.response.send_message(f"Bookings for **{flight['flight_number']}** are now {'open' if bookings_open else 'closed'}.", ephemeral=True)

@bot.tree.command(name="stafflogsetup", description="Admin: configure the private staff activity log channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel where staff activity logs should be posted")
async def stafflogsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Run this command in the staff server.", ephemeral=True)
    cfg = operations_data()
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured Oman Air staff can set the staff log channel.", ephemeral=True)
    if channel.guild.id != STAFF_GUILD_ID:
        return await interaction.response.send_message("Choose a channel in the staff server.", ephemeral=True)
    save_guild_config(STAFF_GUILD_ID, "staff_log_channel_id", channel.id)
    await interaction.response.send_message(f"Staff activity logs will be posted in {channel.mention}.", ephemeral=True)

@bot.tree.command(name="staffrank", description="Staff: assign or update a virtual airline staff rank")
@app_commands.describe(member="Staff member", rank="Rank title (e.g. Cabin Crew, First Officer, Captain, Operations Manager)")
async def staffrank(interaction: discord.Interaction, member: discord.Member, rank: str):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can manage staff ranks.", ephemeral=True)
    if not staff_member(member, cfg):
        return await interaction.response.send_message("That member does not have the configured staff role.", ephemeral=True)
    rank = rank.strip()[:80]
    if not rank:
        return await interaction.response.send_message("Rank cannot be empty.", ephemeral=True)
    data = load_data()
    guild_cfg = data.setdefault(str(STAFF_GUILD_ID), {})
    ranks = guild_cfg.setdefault("staff_ranks", {})
    ranks[str(member.id)] = {"rank": rank, "assigned_by": interaction.user.id, "updated_at": discord.utils.utcnow().isoformat()}
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "Staff rank updated", f"{member.mention} assigned rank: **{rank}**")
    await interaction.response.send_message(f"Updated {member.mention}'s virtual airline rank to **{rank}**.", ephemeral=True)

@bot.tree.command(name="staffpromotions", description="Staff: view the virtual airline staff rank list")
async def staffpromotions(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != STAFF_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view this list.", ephemeral=True)
    ranks = cfg.get("staff_ranks", {})
    lines = [f"• <@{member_id}> — **{record.get('rank', 'Unassigned')}**" for member_id, record in ranks.items()]
    embed = discord.Embed(title="🧑‍✈️ Oman Air | Staff Rank & Promotion Register", description="\n".join(lines)[:4000] or "No staff ranks assigned yet.", color=discord.Color.from_rgb(0, 45, 90))
    embed.set_footer(text="Virtual airline staff register")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="staffactivity", description="Staff: view recent staff activity log")
@app_commands.describe(limit="Number of recent entries (1–15)")
async def staffactivity(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 15] = 10):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = operations_data()
    if interaction.guild.id != STAFF_GUILD_ID or not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view activity logs.", ephemeral=True)
    entries = audit_store(cfg)[-limit:][::-1]
    lines = [f"• <@{e['actor_id']}> — **{e['action']}**\n{e['details']}\n{e['timestamp']}" for e in entries]
    embed = discord.Embed(title="🧾 Oman Air | Recent Staff Activity", description="\n\n".join(lines)[:4000] or "No activity recorded yet.", color=discord.Color.from_rgb(0, 45, 90))
    await interaction.response.send_message(embed=embed, ephemeral=True)


0
# ---------------- Oman Air AI chatbot ----------------

CHATBOT_INSTRUCTIONS = """
You are the Oman Air Community Assistant, a friendly and helpful chatbot inside a Discord community.
Answer clearly and briefly. Help with general questions and explain how this community's bot features work,
including flight announcements, booking commands, ticket support, polls, events and SkyMiles.
Do not invent flight schedules, ticket prices, booking status, member balances, server policies or staff decisions.
You do not have access to live airline systems or private member records. If a question needs account-specific
information, tell the member to use the relevant bot command or contact staff through a support ticket.
Make clear that this is a community assistant and not an official Oman Air customer-service channel.
If you are unsure, say so rather than guessing. Never ask for passwords, API keys, tokens or account cookies.
"""

@bot.tree.command(name="ask", description="Ask the Oman Air AI community assistant a question")
@app_commands.describe(question="Ask a general question about the community, bot or other topics")
async def ask_oman_air(interaction: discord.Interaction, question: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command inside the Oman Air Discord server.", ephemeral=True)
    question = question.strip()
    if not question or len(question) > 1200:
        return await interaction.response.send_message("Please enter a question of 1–1,200 characters.", ephemeral=True)
    if gemini_client is None:
        return await interaction.response.send_message(
            "The AI assistant is not configured yet. A server administrator needs to add the GEMINI_API_KEY variable in Railway.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True, thinking=True)
    history_key = (interaction.guild.id, interaction.user.id)
    history = CHAT_HISTORY.get(history_key, [])
    conversation = history[-8:] + [{"role": "user", "parts": [{"text": question}]}]

    try:
        response = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=conversation,
                config=types.GenerateContentConfig(
                    system_instruction=CHATBOT_INSTRUCTIONS,
                    max_output_tokens=450,
                    thinking_config=types.ThinkingConfig(thinking_level="LOW")
                )
            ),
            timeout=60
        )
        answer = (response.text or "").strip()
        if not answer:
            answer = "I couldn't create an answer just now. Please try again."
        CHAT_HISTORY[history_key] = (history + [
            {"role": "user", "parts": [{"text": question}]},
            {"role": "model", "parts": [{"text": answer}]}
        ])[-10:]
        await interaction.followup.send(answer[:1900], ephemeral=True)
    except asyncio.TimeoutError:
        log.error("Oman Air Gemini chatbot request timed out after 60 seconds")
        await interaction.followup.send(
            "Gemini took too long to respond (60 seconds). Please try again. Staff should check Railway logs and Gemini API settings if this repeats.",
            ephemeral=True
        )
    except Exception:
        log.exception("Oman Air Gemini chatbot request failed")
        await interaction.followup.send(
            "Sorry, the AI assistant request failed. Staff should check Railway logs for the Gemini error (API key, model access, or quota).",
            ephemeral=True
        )


# ---------------- Roblox account verification ----------------

def _roblox_api_json(url, payload=None):
    headers = {"User-Agent": "OmanAirDiscordBot/1.0", "Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))

async def _roblox_json(url, payload=None):
    return await asyncio.to_thread(_roblox_api_json, url, payload)

class RobloxUsernameModal(discord.ui.Modal, title="Roblox Account Verification"):
    username = discord.ui.TextInput(label="Roblox username", placeholder="Enter your Roblox username", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
            return await interaction.response.send_message("Use this panel in the main Oman Air server.", ephemeral=True)
        username = str(self.username.value).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await _roblox_json("https://users.roblox.com/v1/usernames/users", {"usernames": [username], "excludeBannedUsers": True})
            users = result.get("data", [])
            if not users:
                return await interaction.followup.send("I couldn't find that Roblox username. Check the spelling and try again.", ephemeral=True)
            roblox_user = users[0]
            if not isinstance(interaction.user, discord.Member):
                return await interaction.followup.send("I couldn't identify your server membership. Please try again in the main server.", ephemeral=True)
            verified_role = discord.utils.get(interaction.guild.roles, name="VERIFIED")
            if verified_role is None:
                return await interaction.followup.send("The Discord role `VERIFIED` doesn't exist. Ask a server admin to create it.", ephemeral=True)
            try:
                await interaction.user.add_roles(verified_role, reason=f"Roblox username exists: {roblox_user.get('name', username)}")
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Unable to assign VERIFIED role after Roblox username lookup")
                return await interaction.followup.send("I found the Roblox account, but couldn't assign VERIFIED. The bot needs Manage Roles and its role must be above VERIFIED.", ephemeral=True)
            nickname = f"[ECO] {interaction.user.name} | VERIFIED"[:32]
            nickname_note = ""
            try:
                await interaction.user.edit(nick=nickname, reason="Roblox username lookup completed")
            except (discord.Forbidden, discord.HTTPException):
                nickname_note = " I couldn't update your nickname; check Manage Nicknames and the bot's role position."
                log.exception("Unable to update nickname after Roblox username lookup")
            data = load_data()
            guild_cfg = data.setdefault(str(MAIN_GUILD_ID), {})
            guild_cfg.setdefault("verified_roblox_accounts", {})[str(interaction.user.id)] = {
                "roblox_id": int(roblox_user["id"]),
                "username": roblox_user.get("name", username),
                "verification_type": "username_exists_only",
                "verified_at": discord.utils.utcnow().isoformat()
            }
            save_data(data)
            await interaction.followup.send(
                f"Roblox username **{roblox_user.get('name', username)}** was found. You received **VERIFIED** and your nickname is now **{nickname}**.{nickname_note}\\n\\n"
                "**Important:** this only confirms that the Roblox account exists. It does not prove that you own it.",
                ephemeral=True
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, discord.HTTPException):
            log.exception("Roblox panel verification start failed")
            await interaction.followup.send("Roblox's verification service is unavailable right now. Please try again later.", ephemeral=True)

class RobloxVerificationPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify Roblox Account", emoji="🔐", style=discord.ButtonStyle.success, custom_id="oman_roblox:begin")
    async def begin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RobloxUsernameModal())

@bot.tree.command(name="robloxpanel", description="Admin: publish the Roblox verification panel")
@app_commands.default_permissions(administrator=True)
async def robloxpanel(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Run this command in the main Oman Air server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Only server administrators can publish this panel.", ephemeral=True)
    embed = discord.Embed(
        title="✈️ Oman Air | Roblox Member Verification",
        description=("Welcome aboard! Check your Roblox username to unlock the Oman Air community.\\n\\n"
                     "**How it works**\\n"
                     "1. Press **Verify Roblox Account** and enter your Roblox username.\\n"
                     "2. The bot checks Roblox to see whether that username exists.\\n"
                     "3. If found, the bot saves the account details, grants **VERIFIED**, and updates your nickname.\\n\\n"
                     "**Note:** this is an account-existence check only. It does not prove account ownership."),
        color=discord.Color.from_rgb(0, 45, 90)
    )
    embed.set_footer(text="Never share your Roblox password or account cookie.")
    await interaction.channel.send(embed=embed, view=RobloxVerificationPanel())
    await interaction.response.send_message("Roblox verification panel published.", ephemeral=True)

@bot.tree.command(name="robloxverify", description="Start Roblox account verification")
@app_commands.describe(username="Your Roblox username (not your display name)")
async def robloxverify(interaction: discord.Interaction, username: str):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Use Roblox verification in the main Oman Air server.", ephemeral=True)
    username = username.strip()
    if not username or len(username) > 20:
        return await interaction.response.send_message("Enter a valid Roblox username.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await _roblox_json("https://users.roblox.com/v1/usernames/users", {"usernames": [username], "excludeBannedUsers": True})
        users = result.get("data", [])
        if not users:
            return await interaction.followup.send("I couldn't find that Roblox username. Check the spelling and try again.", ephemeral=True)
        roblox_user = users[0]
        code = "OMAN-" + secrets.token_hex(4).upper()
        data = load_data()
        guild_cfg = data.setdefault(str(MAIN_GUILD_ID), {})
        pending = guild_cfg.setdefault("roblox_verifications", {})
        pending[str(interaction.user.id)] = {
            "roblox_id": int(roblox_user["id"]),
            "username": roblox_user.get("name", username),
            "code": code,
            "created_at": discord.utils.utcnow().isoformat()
        }
        save_data(data)
        await interaction.followup.send(
            f"**Step 1:** Open your Roblox profile for **{roblox_user.get('name', username)}**.\n"
            f"**Step 2:** Temporarily put this code in your profile About/Description: `{code}`\n"
            "**Step 3:** Save your profile, then run `/robloxconfirm` here.\n\n"
            "Do not share your Roblox password or cookie. You can remove the code after verification.",
            ephemeral=True
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, discord.HTTPException):
        log.exception("Roblox verification start failed")
        await interaction.followup.send("Roblox's verification service couldn't be reached right now. Please try again later.", ephemeral=True)

@bot.tree.command(name="robloxconfirm", description="Confirm your Roblox profile verification code")
async def robloxconfirm(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this in the main Oman Air server.", ephemeral=True)
    data = load_data()
    guild_cfg = data.setdefault(str(MAIN_GUILD_ID), {})
    pending = guild_cfg.setdefault("roblox_verifications", {})
    record = pending.get(str(interaction.user.id))
    if not record:
        return await interaction.response.send_message("First run `/robloxverify` with your Roblox username.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        profile = await _roblox_json(f"https://users.roblox.com/v1/users/{int(record['roblox_id'])}")
        description = str(profile.get("description", ""))
        if record["code"] not in description:
            return await interaction.followup.send("I can't see your code in that Roblox profile description yet. Save it on Roblox, then try `/robloxconfirm` again.", ephemeral=True)
        verified_role = discord.utils.get(interaction.guild.roles, name="VERIFIED")
        if verified_role is None:
            return await interaction.followup.send("The Discord role `VERIFIED` doesn't exist yet. Ask a server admin to create it.", ephemeral=True)
        try:
            await interaction.user.add_roles(verified_role, reason=f"Roblox account verified: {record.get('username')}")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Unable to assign VERIFIED role")
            return await interaction.followup.send("I verified your Roblox profile, but can't assign the VERIFIED role. Admins must give the bot Manage Roles and place its role above VERIFIED.", ephemeral=True)
        guild_cfg.setdefault("verified_roblox_accounts", {})[str(interaction.user.id)] = {
            "roblox_id": int(record["roblox_id"]), "username": record.get("username"),
            "verified_at": discord.utils.utcnow().isoformat()
        }
        pending.pop(str(interaction.user.id), None)
        save_data(data)
        await interaction.followup.send(f"Verification successful! **{record.get('username')}** is linked and you received the **VERIFIED** role.", ephemeral=True)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, discord.HTTPException):
        log.exception("Roblox verification confirmation failed")
        await interaction.followup.send("Roblox's verification service couldn't be reached right now. Please try again later.", ephemeral=True)

@app_commands.guilds(discord.Object(id=MAIN_GUILD_ID))
@bot.tree.command(name="poll", description="Create a poll with reaction voting")
@app_commands.describe(
    question="What should members vote on?",
    option1="First choice",
    option2="Second choice",
    option3="Third choice (optional)",
    option4="Fourth choice (optional)",
    channel="Channel to publish the poll (defaults to this channel)"
)
async def create_poll(
    interaction: discord.Interaction,
    question: str,
    option1: str,
    option2: str,
    option3: str = None,
    option4: str = None,
    channel: discord.TextChannel = None
):
    if not interaction.guild:
        return await interaction.response.send_message("Create polls from a server channel.", ephemeral=True)
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel) or target.guild.id != interaction.guild.id:
        return await interaction.response.send_message("Choose a text channel in this server.", ephemeral=True)
    if not question.strip() or len(question) > 240:
        return await interaction.response.send_message("The poll question must be 1–240 characters.", ephemeral=True)
    options = [option.strip() for option in (option1, option2, option3, option4) if option and option.strip()]
    if len(options) < 2 or any(len(option) > 100 for option in options):
        return await interaction.response.send_message("Provide 2–4 choices, each no longer than 100 characters.", ephemeral=True)
    if len(set(option.casefold() for option in options)) != len(options):
        return await interaction.response.send_message("Each poll choice must be different.", ephemeral=True)
    perms = target.permissions_for(interaction.guild.me)
    if not perms.send_messages or not perms.embed_links or not perms.add_reactions:
        return await interaction.response.send_message("I need Send Messages, Embed Links, and Add Reactions permissions in that channel.", ephemeral=True)

    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    embed = discord.Embed(
        title="📊 " + question.strip(),
        description="\n".join(f"{emojis[index]} {choice}" for index, choice in enumerate(options)),
        color=discord.Color.from_rgb(0, 45, 90),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Voting", value="React with the emoji beside your choice. One reaction per poll.", inline=False)
    embed.set_footer(text=f"Poll created by {interaction.user.display_name} • Oman Air")
    try:
        message = await target.send(embed=embed)
        for emoji in emojis[:len(options)]:
            await message.add_reaction(emoji)
    except discord.Forbidden:
        return await interaction.response.send_message("I cannot publish or add reactions in that channel. Check my permissions.", ephemeral=True)
    except discord.HTTPException:
        log.exception("Poll publishing failed")
        return await interaction.response.send_message("Discord couldn't publish the poll. Please try again.", ephemeral=True)
    await interaction.response.send_message(f"Poll published in {target.mention}!", ephemeral=True)

bot.tree.add_command(flight_group)
bot.tree.add_command(aircraft_group)
bot.tree.add_command(crew_group)

# ---------------- SkyMiles Shop Panel ----------------

MILES_SHOP_ITEMS = {
    "blue": {"name": "Sindbad Blue Class", "cost": 2500, "description": "Virtual Blue tier • member welcome perks"},
    "silver": {"name": "Sindbad Silver Class", "cost": 6000, "description": "Virtual Silver tier • priority support and bonus perks"},
    "gold": {"name": "Sindbad Gold Class", "cost": 10000, "description": "Virtual Gold tier • premium member perks"},
    "lounge": {"name": "Oman Air Lounge Pass", "cost": 4000, "description": "Virtual lounge access voucher"},
    "priority": {"name": "Priority Service Pass", "cost": 7500, "description": "Virtual priority check-in and support pass"},
    "business": {"name": "OneFlight Business Forever", "cost": 50000, "description": "Virtual lifetime business-class status in this community"},
    "extra_bag": {"name": "Extra Baggage Voucher", "cost": 3000, "description": "Virtual extra-baggage reward voucher"},
}

class MilesShopSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=item["name"], description=f"{item['cost']:,} SkyMiles • {item['description']}"[:100], value=key)
            for key, item in MILES_SHOP_ITEMS.items()
        ]
        super().__init__(placeholder="Choose a reward to redeem…", min_values=1, max_values=1, options=options, custom_id="oman_miles_shop:select")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
            return await interaction.response.send_message("Use the SkyMiles Shop in the main server.", ephemeral=True)
        key = self.values[0]
        item = MILES_SHOP_ITEMS[key]
        data = load_data()
        cfg = data.setdefault(str(STAFF_GUILD_ID), {})
        balances = cfg.setdefault("skymiles", {})
        member_key = str(interaction.user.id)
        balance = int(balances.get(member_key, 0))
        if balance < item["cost"]:
            return await interaction.response.send_message(f"You have **{balance:,} SkyMiles**. You need **{item['cost'] - balance:,} more** to redeem **{item['name']}**.", ephemeral=True)
        balances[member_key] = balance - item["cost"]
        cfg.setdefault("miles_transactions", []).append({
            "member_id": interaction.user.id, "amount": -item["cost"], "type": "Shop redemption",
            "description": item["name"], "timestamp": discord.utils.utcnow().isoformat()
        })
        cfg.setdefault("miles_redemptions", []).append({
            "member_id": interaction.user.id, "reward": item["name"], "cost": item["cost"],
            "status": "Pending staff fulfilment", "redeemed_at": discord.utils.utcnow().isoformat()
        })
        save_data(data)
        embed = discord.Embed(title="🛍️ SkyMiles Shop | Redemption Submitted", description=f"You redeemed **{item['name']}** for **{item['cost']:,} SkyMiles**.\n\nRemaining balance: **{balances[member_key]:,} SkyMiles**\nStatus: **Pending staff fulfilment**\n\nThese are virtual community rewards, not real airline benefits.", color=discord.Color.from_rgb(0, 45, 90))
        await interaction.response.send_message(embed=embed, ephemeral=True)

class MilesShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(MilesShopSelect())

class MilesShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open SkyMiles Shop", emoji="🛍️", style=discord.ButtonStyle.success, custom_id="oman_miles_shop:open")
    async def open_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
            return await interaction.response.send_message("Use the shop panel in the main server.", ephemeral=True)
        cfg = operations_data()
        balance = int(cfg.get("skymiles", {}).get(str(interaction.user.id), 0))
        embed = discord.Embed(title="🛍️ Oman Air | SkyMiles Shop", description=f"Your balance: **{balance:,} SkyMiles**\nChoose a reward from the menu below. Redeemptions are virtual community perks and are fulfilled by staff.", color=discord.Color.from_rgb(0, 45, 90))
        for item in MILES_SHOP_ITEMS.values():
            embed.add_field(name=f"{item['name']} • {item['cost']:,} miles", value=item["description"], inline=False)
        await interaction.response.send_message(embed=embed, view=MilesShopView(), ephemeral=True)

@bot.tree.command(name="mileshopanel", description="Staff: publish the persistent SkyMiles Shop panel")
@app_commands.default_permissions(manage_guild=True)
async def mileshopanel(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return await interaction.response.send_message("Run this command in the main server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need Manage Server permission to publish the shop panel.", ephemeral=True)
    embed = discord.Embed(title="🛍️ Oman Air | SkyMiles Shop", description="Exchange your SkyMiles for Sindbad-inspired membership tiers and virtual community rewards.\n\nPress **Open SkyMiles Shop** to view the catalogue and redeem privately. Staff fulfilment may be required. These are community roleplay rewards, not actual airline benefits.", color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="Sindbad-inspired tiers", value="🔵 Blue Class — 2,500 points\n⚪ Silver Class — 6,000 points\n🟡 Gold Class — 10,000 points", inline=False)
    embed.add_field(name="Featured reward", value="💼 OneFlight Business Forever — 50,000 points", inline=False)
    await interaction.channel.send(embed=embed, view=MilesShopPanelView())
    await interaction.response.send_message("SkyMiles Shop panel published.", ephemeral=True)


# Persistent component views are registered at startup.
async def setup_views():
    bot.add_view(RobloxVerificationPanel())
    bot.add_view(MilesShopPanelView())
    bot.add_view(MilesShopView())
    bot.add_view(TicketPanel())
    bot.add_view(TicketControls())
    bot.add_view(TicketStaffTools())
    # Register persistent booking buttons from saved flights before Discord connects.
    cfg = load_data().get(str(STAFF_GUILD_ID), {})
    for flight in cfg.get("flights", {}).values():
        bot.add_view(BookFlightView(flight["flight_number"]))
        bot.add_view(CheckInFlightView(flight["flight_number"]))

# Ensure classes/commands are defined before the bot starts.
_original_setup_hook = bot.setup_hook
async def setup_hook_with_views():
    for guild_id in (STAFF_GUILD_ID, MAIN_GUILD_ID):
        guild = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("Synced %d commands to guild %s", len(synced), guild_id)
    await setup_views()
bot.setup_hook = setup_hook_with_views

@bot.event
async def on_ready():
    log.info("Bot connected as %s (%s)", bot.user, bot.user.id)
    log.info("Connected guilds: %s", [g.id for g in bot.guilds])
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Oman Air Support ✈️"))

# Main-server app commands are limited to verification until a member has VERIFIED.
async def require_roblox_verification(interaction: discord.Interaction) -> bool:
    if not interaction.guild or interaction.guild.id != MAIN_GUILD_ID:
        return True
    if interaction.command and interaction.command.name in {"robloxverify", "robloxconfirm", "robloxpanel"}:
        return True
    if isinstance(interaction.user, discord.Member) and discord.utils.get(interaction.user.roles, name="VERIFIED"):
        return True
    if interaction.response.is_done():
        await interaction.followup.send("You must complete Roblox verification first with `/robloxverify` and `/robloxconfirm`.", ephemeral=True)
    else:
        await interaction.response.send_message("You must complete Roblox verification first with `/robloxverify` and `/robloxconfirm`.", ephemeral=True)
    return False

bot.tree.interaction_check = require_roblox_verification

bot.run(TOKEN)
