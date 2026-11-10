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


## Oman Air virtual flight operations

The bot also includes flight announcements and booking, private booking lookup/cancellation, check-in and digital boarding passes, passenger manifests and boarding control, aircraft/crew management, crew shifts, SkyMiles, shop redemptions, an operations dashboard, staff applications and training, aircraft maintenance records, and seat selection.

### Required Railway variables

- `DISCORD_TOKEN`: Discord bot token (secret).
- `GUILD_ID`: numeric ID of the primary server used for command sync.
- `STAFF_GUILD_ID`: staff server ID (defaults to the configured Oman Air staff server).
- `MAIN_GUILD_ID`: passenger community server ID.
- `MAIN_ANNOUNCEMENT_CHANNEL_ID`: flight announcement channel ID in the passenger server.
- `GEMINI_API_KEY`: optional; the AI helper is not required for flight operations.

The bot must be invited to both the staff server and passenger server. Enable the **Server Members Intent** in the Discord Developer Portal. Give it the permissions it needs for the channels and roles it manages (View Channels, Send Messages, Embed Links, Read Message History, Manage Channels, and role/member access as appropriate).

### Important: persistent storage on Railway

All flights, bookings, crew, aircraft, tickets, SkyMiles, applications and training records are stored in `tickets.json`. Railway's container filesystem can be replaced during redeploys. Attach a Railway Volume and mount it at the service working directory (the directory containing `main.py`) so `tickets.json` survives restarts and deployments. Do not attach multiple bot replicas to the same JSON file; JSON storage is intended for a single running instance.

### Useful commands

- Flight operations: `/flight create`, `/flight list`, `/flightupdate`, `/flight cancel`, `/flightnotify`, `/opscenter`, `/opsreport`.
- Passenger services: `/bookflight`, `/mybookings`, `/cancelbooking`, `/seatselect`, `/checkinstart`, `/manifest`, `/boardpassenger`, `/boardingstatus`.
- Fleet and crew: `/aircraft`, `/maintenance record`, `/maintenance history`, `/crew`.
- Rewards: `/mymiles`, `/mileshistory`, `/redeemmiles`, `/mileshopanel`.
- Staff development: `/staffapply`, `/staffappreview`, `/stafftraining`, `/mytraining`, `/trainingcomplete`.

This is a virtual community/roleplay management system. SkyMiles, tickets, rewards, and airline operations are not real airline services or travel documents.
