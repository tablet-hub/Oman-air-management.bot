import os
import logging
import json
from pathlib import Path

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
        self.user_id = discord.ui.TextInput(label="Discord user ID", placeholder="Enter the member's numeric ID", max_length=25)
        self.add_item(self.user_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        try:
            member_id = int(str(self.user_id.value).strip())
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
        self.user_id = discord.ui.TextInput(label="Staff member Discord ID", placeholder="Enter numeric member ID", max_length=25)
        self.add_item(self.user_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_staff(interaction):
            return
        try:
            staff_id = int(str(self.user_id.value).strip())
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
        try:
            await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:100])
        except discord.HTTPException:
            log.exception("Could not rename closed ticket")

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

class TicketStaffTools(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="oman_staff_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        if record.get("claimed_by"):
            return await interaction.response.send_message(f"Already claimed by <@{record['claimed_by']}>.", ephemeral=True)
        update_ticket(interaction.guild.id, interaction.channel_id, "claimed_by", interaction.user.id)
        await interaction.response.send_message(f"{interaction.user.mention} claimed this ticket.")
        
    @discord.ui.button(label="Unclaim", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="oman_staff_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        if not record.get("claimed_by"):
            return await interaction.response.send_message("This ticket is currently unclaimed.", ephemeral=True)
        update_ticket(interaction.guild.id, interaction.channel_id, "claimed_by", None)
        await interaction.response.send_message(f"Ticket unclaimed by {interaction.user.mention}.")

    @discord.ui.button(label="Assign", emoji="👤", style=discord.ButtonStyle.primary, custom_id="oman_staff_assign")
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(StaffIdModal("assign"))

    @discord.ui.button(label="Transfer", emoji="🔁", style=discord.ButtonStyle.primary, custom_id="oman_staff_transfer")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(StaffIdModal("transfer"))

    @discord.ui.button(label="Add Member", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="oman_staff_add_member")
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(UserIdModal("add"))

    @discord.ui.button(label="Remove Member", emoji="➖", style=discord.ButtonStyle.secondary, custom_id="oman_staff_remove_member")
    async def remove_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(UserIdModal("remove"))

    @discord.ui.button(label="Priority: Normal", emoji="⚑", style=discord.ButtonStyle.secondary, custom_id="oman_staff_priority")
    async def priority(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        levels = ["Low", "Normal", "High", "Urgent"]
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        current = record.get("priority", "Normal")
        next_level = levels[(levels.index(current) + 1) % len(levels)] if current in levels else "Normal"
        update_ticket(interaction.guild.id, interaction.channel_id, "priority", next_level)
        button.label = f"Priority: {next_level}"
        button.style = discord.ButtonStyle.danger if next_level == "Urgent" else (discord.ButtonStyle.primary if next_level == "High" else discord.ButtonStyle.secondary)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"Priority changed to **{next_level}**.", ephemeral=True)

    @discord.ui.button(label="Staff Reply", emoji="💬", style=discord.ButtonStyle.primary, custom_id="oman_staff_reply")
    async def reply(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_staff(interaction): return
        await interaction.response.send_modal(StaffReplyModal())

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="oman_staff_close")
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
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

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
        await interaction.response.send_message("Ticket closed.")
        try:
            await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:100])
            if not is_staff:
                await interaction.channel.set_permissions(interaction.user, send_messages=False)
        except discord.HTTPException:
            log.exception("Could not update closed ticket channel")

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

bot = TicketBot()

# Persistent component views are registered at startup.
async def setup_views():
    bot.add_view(TicketPanel())
    bot.add_view(TicketControls())
    bot.add_view(TicketStaffTools())

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
