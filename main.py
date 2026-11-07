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
    data = load_data()
    return data.setdefault(str(guild_id), {})

def staff_member(member: discord.Member, cfg: dict) -> bool:
    role = member.guild.get_role(cfg.get("staff_role_id", 0))
    return member.guild_permissions.administrator or bool(role and role in member.roles)

def ticket_record(guild_id: int, channel_id: int):
    data = load_data()
    return data.get(str(guild_id), {}).get("tickets", {}).get(str(channel_id))

class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d application commands to guild %s", len(synced), GUILD_ID)
        self.add_view(TicketPanel())
        self.add_view(TicketControls())

    async def on_ready(self):
        log.info("Bot connected as %s (%s)", self.user, self.user.id)
        log.info("Connected guilds: %s", [g.id for g in self.guilds])
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="Oman Air Support ✈️"))

bot = TicketBot()

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Support Ticket", emoji="🎫",
                       style=discord.ButtonStyle.primary, custom_id="oman_ticket_open_v1")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use this button in the server.", ephemeral=True)
        data = load_data()
        cfg = data.setdefault(str(interaction.guild.id), {})
        category_id = cfg.get("category_id")
        staff_role_id = cfg.get("staff_role_id")
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
            overwrites[interaction.guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
        try:
            channel = await interaction.guild.create_text_channel(
                name=f"ticket-{number:04d}", category=category, overwrites=overwrites,
                topic=f"Oman Air support ticket {number} | creator {interaction.user.id}")
        except discord.Forbidden:
            return await interaction.response.send_message("Bot lacks Manage Channels permission.", ephemeral=True)
        except discord.HTTPException as exc:
            log.exception("Channel creation failed")
            return await interaction.response.send_message(f"Discord could not create the channel: {exc}", ephemeral=True)

        tickets[str(channel.id)] = {
            "ticket_number": number, "channel_id": channel.id,
            "creator_id": interaction.user.id, "status": "open", "claimed_by": None
        }
        save_data(data)
        await channel.send(
            f"Welcome {interaction.user.mention} to Oman Air Support.\n"
            "Please describe your issue clearly and include relevant details.\n"
            "A support staff member will respond as soon as possible.",
            view=TicketControls())
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒",
                       style=discord.ButtonStyle.danger, custom_id="oman_ticket_close_v1")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Server-only action.", ephemeral=True)
        cfg = guild_data(interaction.guild.id)
        record = ticket_record(interaction.guild.id, interaction.channel_id)
        if not record:
            return await interaction.response.send_message("This channel is not a tracked ticket.", ephemeral=True)
        authorized = staff_member(interaction.user, cfg)
        creator = record.get("creator_id") == interaction.user.id
        if not authorized and not creator:
            return await interaction.response.send_message("Only the ticket owner or support staff can close this ticket.", ephemeral=True)
        data = load_data()
        data[str(interaction.guild.id)]["tickets"][str(interaction.channel_id)]["status"] = "closed"
        save_data(data)
        await interaction.response.send_message(f"Ticket closed by {interaction.user.mention}.")
        try:
            await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:100])
            if creator and not authorized:
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
    await interaction.response.send_message(
        f"Ticket system configured. Staff role: {staff_role.mention} | Category: {category.name}",
        ephemeral=True)

@bot.tree.command(name="ticketpanel", description="Post the extended support ticket panel")
@app_commands.default_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    cfg = guild_data(interaction.guild_id)
    if not cfg.get("staff_role_id") or not cfg.get("category_id"):
        return await interaction.response.send_message("Run /ticketsetup first.", ephemeral=True)
    embed = discord.Embed(
        title="✈️ Oman Air | Customer Support Center",
        description=(
            "Welcome to the Oman Air Support Center. Our team is here to assist with "
            "flight-management questions, booking support, account concerns, and other "
            "server-related matters.\n\n"
            "**Before opening a ticket**\n"
            "• Check whether your question has already been answered.\n"
            "• Choose a clear and descriptive explanation when your ticket opens.\n"
            "• Please keep all support conversations inside your private ticket.\n"
            "• Do not share passwords, tokens, or other confidential information.\n\n"
            "Press **Open Support Ticket** below to create a private channel visible "
            "to you and the support team."
        ),
        color=discord.Color.from_rgb(0, 45, 90))
    embed.add_field(name="🎫 Ticket process", value="1. Open a ticket\n2. Explain your request\n3. Staff will reply\n4. Close the ticket when resolved", inline=False)
    embed.add_field(name="🧑‍✈️ Support team", value="Support staff can claim tickets and help resolve your request. Please be respectful and patient.", inline=False)
    embed.add_field(name="🔐 Privacy", value="Ticket channels are private to the ticket creator and configured staff role. Avoid posting sensitive personal information.", inline=False)
    embed.set_footer(text="Oman Air Support • Virtual server support system")
    await interaction.channel.send(embed=embed, view=TicketPanel())
    await interaction.response.send_message("Extended ticket panel posted.", ephemeral=True)

@bot.tree.command(name="ticketclaim", description="Staff: claim the ticket in this channel")
async def ticketclaim(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in a server ticket channel.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured support staff can claim tickets.", ephemeral=True)
    record = ticket_record(interaction.guild.id, interaction.channel_id)
    if not record or record.get("status") != "open":
        return await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
    if record.get("claimed_by"):
        return await interaction.response.send_message(f"Already claimed by <@{record['claimed_by']}>.", ephemeral=True)
    data = load_data()
    data[str(interaction.guild.id)]["tickets"][str(interaction.channel_id)]["claimed_by"] = interaction.user.id
    save_data(data)
    await interaction.response.send_message(f"🧑‍✈️ Ticket claimed by {interaction.user.mention}. The team has been notified.")

@bot.tree.command(name="ticketstats", description="Staff: view support ticket statistics")
async def ticketstats(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in the server.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured support staff can view ticket stats.", ephemeral=True)
    tickets = cfg.get("tickets", {})
    total = len(tickets)
    opened = sum(1 for t in tickets.values() if t.get("status") == "open")
    closed = sum(1 for t in tickets.values() if t.get("status") == "closed")
    claimed = sum(1 for t in tickets.values() if t.get("claimed_by"))
    embed = discord.Embed(title="📊 Oman Air Support Statistics", color=discord.Color.blue())
    embed.add_field(name="Total tickets", value=str(total), inline=True)
    embed.add_field(name="Open tickets", value=str(opened), inline=True)
    embed.add_field(name="Closed tickets", value=str(closed), inline=True)
    embed.add_field(name="Claimed tickets", value=str(claimed), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ticketclose", description="Staff: close and archive the ticket in this channel")
async def ticketclose(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in a server ticket channel.", ephemeral=True)
    cfg = guild_data(interaction.guild.id)
    if not staff_member(interaction.user, cfg):
        return await interaction.response.send_message("Only configured support staff can use this command.", ephemeral=True)
    record = ticket_record(interaction.guild.id, interaction.channel_id)
    if not record or record.get("status") != "open":
        return await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
    data = load_data()
    data[str(interaction.guild.id)]["tickets"][str(interaction.channel_id)]["status"] = "closed"
    save_data(data)
    await interaction.response.send_message(f"🔒 Ticket closed by staff member {interaction.user.mention}.")
    try:
        await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:100])
    except discord.HTTPException:
        log.exception("Could not rename closed ticket")

bot.run(TOKEN)
