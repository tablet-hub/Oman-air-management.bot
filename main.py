import os
import logging
import json
import uuid
from pathlib import Path
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oman-air")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
DATA_PATH = Path("tickets.json")

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

@bot.tree.command(name="welcomesetup", description="Set the channel for automatic member welcome messages")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel where new members should be welcomed")
async def welcomesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    data = load_data()
    cfg = data.setdefault(str(interaction.guild_id), {})
    cfg["welcome_channel_id"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"Automatic welcome messages are now enabled in {channel.mention}.",
        ephemeral=True
    )

@bot.event
async def on_member_join(member: discord.Member):
    cfg = guild_data(member.guild.id)
    channel_id = cfg.get("welcome_channel_id")
    if not channel_id:
        return
    channel = member.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Configured welcome channel %s was not found in guild %s", channel_id, member.guild.id)
        return
    embed = discord.Embed(
        title="✈️ Welcome to Oman Air!",
        description=(
            f"Welcome aboard, {member.mention}! We're glad you've joined **{member.guild.name}**.\n\n"
            "Please take a moment to read the server rules and get familiar with our community. "
            "If you need assistance, our support ticket system is here to help."
        ),
        color=discord.Color.from_rgb(0, 45, 90)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="Member number", value=str(member.guild.member_count or "—"), inline=True)
    embed.set_footer(text="Oman Air • Virtual Aviation Community")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.error("Missing permission to send welcome message in channel %s", channel.id)

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
    cfg = guild_data(guild.id)
    channel_id = cfg.get("flight_announcement_channel_id")
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return False
    await channel.send(embed=flight_embed(guild, flight), view=BookFlightView(flight["flight_number"]))
    return True

@bot.tree.command(name="flightsetup", description="Configure the flight announcement channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel for official flight announcements")
async def flightsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    save_guild_config(interaction.guild_id, "flight_announcement_channel_id", channel.id)
    await interaction.response.send_message(f"Official flight announcements will be posted in {channel.mention}.", ephemeral=True)

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
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(interaction.guild.id), {})
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
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(interaction.guild.id), {})
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
    save_data(data)
    channel_id = cfg.get("flight_announcement_channel_id")
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Flight updated in storage, but announcement channel is not configured. Run /flightsetup.", ephemeral=True)
    embed = flight_embed(interaction.guild, flight, title=f"📣 OMAN AIR | FLIGHT UPDATE — {key}")
    await channel.send(embed=embed)
    await interaction.response.send_message(f"Published update for **{key}**: **{normalized}**.", ephemeral=True)

@flight_group.command(name="list", description="List scheduled flights")
@app_commands.default_permissions(manage_guild=True)
async def flight_list(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    flights = guild_data(interaction.guild.id).get("flights", {})
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
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in your server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(interaction.guild.id), {})
    flight = cfg.setdefault("flights", {}).get(flight_number.strip().upper())
    if not flight:
        return await interaction.response.send_message("That flight was not found.", ephemeral=True)
    flight["status"] = "Cancelled"
    flight["cancellation_reason"] = reason.strip()[:500]
    flight["updated_at"] = discord.utils.utcnow().isoformat()
    save_data(data)
    channel_id = cfg.get("flight_announcement_channel_id")
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        embed = flight_embed(interaction.guild, flight, title=f"❌ OMAN AIR | FLIGHT CANCELLED — {flight['flight_number']}")
        embed.add_field(name="Cancellation reason", value=reason.strip()[:1024], inline=False)
        await channel.send(embed=embed)
    await interaction.response.send_message(f"Flight **{flight['flight_number']}** marked as cancelled.", ephemeral=True)

@bot.tree.command(name="announcement", description="Staff: post a professional Oman Air announcement embed")
@app_commands.describe(title="Announcement heading", message="Announcement details", channel="Channel to publish in (optional)")
async def announcement(interaction: discord.Interaction, title: str, message: str, channel: discord.TextChannel = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured Oman Air staff can post announcements.", ephemeral=True)
    target = channel
    if target is None:
        channel_id = cfg.get("flight_announcement_channel_id")
        target = interaction.guild.get_channel(channel_id) if channel_id else None
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Please choose a channel or configure the announcement channel with /flightsetup.", ephemeral=True)
    title = title.strip()[:256]
    message = message.strip()[:4000]
    if not title or not message:
        return await interaction.response.send_message("The title and message cannot be empty.", ephemeral=True)
    embed = discord.Embed(
        title=f"📢 {title}",
        description=message,
        color=discord.Color.from_rgb(0, 45, 90),
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name="OMAN AIR | OFFICIAL ANNOUNCEMENT", icon_url=interaction.guild.icon.url if interaction.guild.icon else discord.Embed.Empty)
    embed.set_footer(text=f"{interaction.guild.name} • Oman Air Virtual Operations")
    try:
        await target.send(embed=embed)
    except discord.Forbidden:
        return await interaction.response.send_message("I cannot post in that channel. Check my channel permissions.", ephemeral=True)
    await interaction.response.send_message(f"Professional announcement published in {target.mention}.", ephemeral=True)

# ---------------- Passenger bookings, check-in, boarding, dashboard and staff ranks ----------------

def booking_store(cfg: dict) -> dict:
    return cfg.setdefault("bookings", {})

def audit_store(cfg: dict) -> list:
    return cfg.setdefault("staff_activity_logs", [])

async def audit_log(guild: discord.Guild, actor: discord.abc.User, action: str, details: str):
    data = load_data()
    cfg = data.setdefault(str(guild.id), {})
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

async def process_flight_booking(interaction: discord.Interaction, flight_number: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this button or command in the server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(interaction.guild.id), {})
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
    record = {"reference": reference, "flight_number": flight["flight_number"], "passenger_id": interaction.user.id, "status": "Booked", "booked_at": discord.utils.utcnow().isoformat(), "checked_in": False, "boarded": False}
    bookings[reference] = record
    save_data(data)
    embed = discord.Embed(title="🎟️ Oman Air | Booking Confirmation", description=f"**Booking reference:** {reference}\n**Passenger:** {interaction.user.mention}\n**Flight:** {flight['flight_number']}\n**Route:** {flight['origin']} ➜ {flight['destination']}", color=discord.Color.from_rgb(0, 45, 90))
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

@bot.tree.command(name="checkin", description="Member: check in using your booking reference")
@app_commands.describe(booking_reference="Your booking reference")
async def checkin(interaction: discord.Interaction, booking_reference: str):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    data = load_data()
    cfg = data.setdefault(str(interaction.guild.id), {})
    booking = get_booking(cfg, booking_reference)
    if not booking or booking.get("passenger_id") != interaction.user.id:
        return await interaction.response.send_message("Booking not found for your account. Check the reference.", ephemeral=True)
    flight = get_flight(cfg, booking["flight_number"])
    if not flight or flight.get("status") in ("Cancelled", "Departed", "Arrived"):
        return await interaction.response.send_message("Check-in is unavailable for this flight.", ephemeral=True)
    if booking.get("checked_in"):
        return await interaction.response.send_message("You are already checked in for this flight.", ephemeral=True)
    booking["checked_in"] = True
    booking["checked_in_at"] = discord.utils.utcnow().isoformat()
    booking["status"] = "Checked in"
    save_data(data)
    embed = discord.Embed(title="🛂 Oman Air | Boarding Pass", description=f"**Passenger:** {interaction.user.mention}\n**Reference:** {booking['reference']}\n**Flight:** {flight['flight_number']}\n**Route:** {flight['origin']} ➜ {flight['destination']}", color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="🧳 Check-in", value=flight.get("check_in", "TBA"), inline=False)
    embed.add_field(name="🛫 Departure", value=flight.get("departure", "TBA"), inline=True)
    embed.add_field(name="🛬 Arrival", value=flight.get("arrival", "TBA"), inline=True)
    embed.add_field(name="🚪 Gate", value=flight.get("gate", "TBA"), inline=True)
    embed.add_field(name="Boarding status", value="CHECKED IN • Awaiting boarding", inline=False)
    embed.set_footer(text="Present this digital boarding pass to virtual airline staff.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="manifest", description="Staff: view the passenger manifest for a flight")
@app_commands.describe(flight_number="Flight number")
async def manifest(interaction: discord.Interaction, flight_number: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
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
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
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
    data.setdefault(str(interaction.guild.id), {}).setdefault("bookings", {})[booking["reference"]] = booking
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "Passenger boarded", f"Flight {booking['flight_number']} • Booking {booking['reference']} • Passenger <@{booking['passenger_id']}>")
    await interaction.response.send_message(f"Passenger <@{booking['passenger_id']}> marked as boarded for **{booking['flight_number']}**.", ephemeral=True)

@bot.tree.command(name="flightdashboard", description="Staff: view the Oman Air flight operations dashboard")
async def flightdashboard(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
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
        embed.add_field(name=f"{f['flight_number']} • {f.get('status', 'Scheduled')}", value=f"{f['origin']} ➜ {f['destination']}\nDeparture: {f.get('departure', 'TBA')} • Gate: {f.get('gate', 'TBA')}\nPassengers: {len(flight_bookings)}/{f.get('capacity', 180)}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="flightbooking", description="Staff: open or close bookings for a flight")
@app_commands.describe(flight_number="Flight number", bookings_open="True to open bookings, false to close")
async def flightbooking(interaction: discord.Interaction, flight_number: str, bookings_open: bool):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can manage flight bookings.", ephemeral=True)
    flight = get_flight(cfg, flight_number)
    if not flight:
        return await interaction.response.send_message("Flight not found.", ephemeral=True)
    flight["booking_open"] = bookings_open
    data = load_data()
    data.setdefault(str(interaction.guild.id), {}).setdefault("flights", {})[flight["flight_number"]] = flight
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "Booking availability changed", f"{flight['flight_number']} bookings {'opened' if bookings_open else 'closed'}")
    await interaction.response.send_message(f"Bookings for **{flight['flight_number']}** are now {'open' if bookings_open else 'closed'}.", ephemeral=True)

@bot.tree.command(name="stafflogsetup", description="Admin: configure the private staff activity log channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel where staff activity logs should be posted")
async def stafflogsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    save_guild_config(interaction.guild_id, "staff_log_channel_id", channel.id)
    await interaction.response.send_message(f"Staff activity logs will be posted in {channel.mention}.", ephemeral=True)

@bot.tree.command(name="staffrank", description="Staff: assign or update a virtual airline staff rank")
@app_commands.describe(member="Staff member", rank="Rank title (e.g. Cabin Crew, First Officer, Captain, Operations Manager)")
async def staffrank(interaction: discord.Interaction, member: discord.Member, rank: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can manage staff ranks.", ephemeral=True)
    if not staff_member(member, cfg):
        return await interaction.response.send_message("That member does not have the configured staff role.", ephemeral=True)
    rank = rank.strip()[:80]
    if not rank:
        return await interaction.response.send_message("Rank cannot be empty.", ephemeral=True)
    data = load_data()
    guild_cfg = data.setdefault(str(interaction.guild.id), {})
    ranks = guild_cfg.setdefault("staff_ranks", {})
    ranks[str(member.id)] = {"rank": rank, "assigned_by": interaction.user.id, "updated_at": discord.utils.utcnow().isoformat()}
    save_data(data)
    await audit_log(interaction.guild, interaction.user, "Staff rank updated", f"{member.mention} assigned rank: **{rank}**")
    await interaction.response.send_message(f"Updated {member.mention}'s virtual airline rank to **{rank}**.", ephemeral=True)

@bot.tree.command(name="staffpromotions", description="Staff: view the virtual airline staff rank list")
async def staffpromotions(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
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
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured staff can view activity logs.", ephemeral=True)
    entries = audit_store(cfg)[-limit:][::-1]
    lines = [f"• <@{e['actor_id']}> — **{e['action']}**\n{e['details']}\n{e['timestamp']}" for e in entries]
    embed = discord.Embed(title="🧾 Oman Air | Recent Staff Activity", description="\n\n".join(lines)[:4000] or "No activity recorded yet.", color=discord.Color.from_rgb(0, 45, 90))
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(flight_group)

# Persistent component views are registered at startup.
async def setup_views():
    bot.add_view(TicketPanel())
    bot.add_view(TicketControls())
    bot.add_view(TicketStaffTools())
    # Register each flight's persistent Book Flight button after bot restarts.
    for guild in bot.guilds:
        for flight in guild_data(guild.id).get("flights", {}).values():
            bot.add_view(BookFlightView(flight["flight_number"]))

# Ensure classes/commands are defined before the bot starts.
_original_setup_hook = bot.setup_hook
async def setup_hook_with_views():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    log.info("Synced %d commands to guild %s", len(synced), GUILD_ID)
    await setup_views()
bot.setup_hook = setup_hook_with_views

@bot.event
async def on_ready():
    log.info("Bot connected as %s (%s)", bot.user, bot.user.id)
    log.info("Connected guilds: %s", [g.id for g in bot.guilds])
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Oman Air Support ✈️"))

bot.run(TOKEN)
