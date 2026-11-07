# Oman Air Management Discord Ticket Bot

A minimal standalone Discord support-ticket bot using discord.py 2.x. Legacy cogs were removed; the bot runs from `main.py`.

## Railway setup

Set these Railway service variables:
- `DISCORD_TOKEN`: your bot token (keep it private; never commit it)
- `GUILD_ID`: the numeric ID of your Discord server

Railway deploy command should run:
```bash
python main.py
```

## Discord setup

1. Invite the bot to your server with OAuth scopes **bot** and **applications.commands**.
2. Give it permissions: View Channels, Send Messages, Embed Links, Read Message History, Manage Channels, Manage Roles (Manage Channels is required to create ticket rooms).
3. In your server, run `/ticketsetup` as an administrator. Choose a staff role and a ticket category.
4. Run `/ticketpanel` in the channel where members should open tickets.
5. Members press **Open Support Ticket**. Ticket rooms are private to the member, the staff role, and the bot.

## Notes

- Commands sync to the server named by `GUILD_ID`, so they should show quickly.
- Ticket data is stored in `tickets.json`. Railway ephemeral filesystem may reset it after redeploy; attach a persistent volume if you need long-term storage.
- If logs show missing token or server ID, fix Railway Variables and redeploy.
