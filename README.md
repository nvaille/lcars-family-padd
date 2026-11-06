# LCARS Family PADD

A retro-futuristic, LCARS-style family dashboard for wall-mounted tablets and kiosks. It shows a clock, local weather, a rotating Stoic quote, and upcoming events from any `.ics` calendar feed.

It's built to run on very old hardware (the reference kiosk is a 1st-gen iPad Mini on iOS 9), so the front end uses no CSS variables, grid, flex `gap`, or ES6.

## Features

- **Chronometer**: large clock and date
- **Environmental Sensors**: current conditions, hourly strip and 5-day forecast from [Open-Meteo](https://open-meteo.com) (no API key needed)
- **Mission Log**: upcoming events from an iCloud, Google, Outlook or any other `.ics` feed, with recurring-event support (RRULE, EXDATE, RECURRENCE-ID)
- **Stoic Directive**: a quote that rotates hourly
- Scales the 1024×748 design to any screen; auto-reloads nightly to keep old Safari stable

## Quick start

Requires Python 3.9+. Standard library only, so there's nothing to install.

```bash
git clone https://github.com/nvaille/lcars-family-padd.git
cd lcars-family-padd
cp config.example.json config.json   # then edit config.json
python3 server.py
```

Open `http://<server-ip>:8000` on the kiosk. On iPad, use **Share → Add to Home Screen** for full-screen mode.

## Configuration

Settings live in `config.json`, which is git-ignored so your private calendar URL is never committed. Every setting can also be set with an environment variable, which takes precedence over the file.

| Key | Env var | Description |
|---|---|---|
| `ics_feed_url` | `LCARS_ICS_URL` | Your calendar's `.ics` / `webcal://` feed. **Keep this private.** |
| `latitude`, `longitude` | `LCARS_LATITUDE`, `LCARS_LONGITUDE` | Weather location |
| `location_name` | `LCARS_LOCATION_NAME` | Label shown above the weather panel |
| `port` | `LCARS_PORT` | HTTP port (default `8000`) |
| `max_events` | `LCARS_MAX_EVENTS` | Events sent to the kiosk (default `5`) |
| `lookahead_days` | `LCARS_LOOKAHEAD_DAYS` | How far ahead recurring events are expanded (default `60`) |
| `timed_default_minutes` | `LCARS_TIMED_DEFAULT_MINUTES` | Assumed length of events with no end time (default `60`) |
| `messaging_provider` | `LCARS_MESSAGING_PROVIDER` | `none` (default) or `telegram`. Turns family text messages on |
| `telegram_bot_token` | `LCARS_TELEGRAM_TOKEN` | Your bot's token from @BotFather. **Secret** |
| `telegram_group_chat_id` | `LCARS_TELEGRAM_GROUP_ID` | Optional family group used for ALL HANDS. **Private** |
| `contacts` | `LCARS_CONTACTS_JSON` | People who get messages: `[{"name":"MOM","telegram_chat_id":"…"}]`. **Private** |
| `crew` | `LCARS_CREW` | Kids who send from the kiosk: a JSON list or `SAM,RILEY` |
| `padd_sender_name` | `LCARS_PADD_SENDER_NAME` | How the kiosk signs messages (default `HOME PADD`) |
| `message_presets` | `LCARS_MESSAGE_PRESETS_JSON` | Preset messages, each with an optional default recipient: `[{"text":"…","to":"ALL"}]` |
| `quick_replies` | `LCARS_QUICK_REPLIES_JSON` | Telegram reply buttons (default `["GOT IT","CALL ME","ON MY WAY"]`) |
| `allow_custom_messages` | `LCARS_ALLOW_CUSTOM_MESSAGES` | Allow typed messages, not just presets (default `false`) |
| `message_retention_hours` | `LCARS_MESSAGE_RETENTION_HOURS` | How long messages are kept (default `24`) |
| `max_message_length` | `LCARS_MAX_MESSAGE_LENGTH` | Longest typed message (default `160`) |
| `send_rate_limit_per_hour` | `LCARS_SEND_RATE_LIMIT` | Most messages the kiosk can send per hour (default `10`, `0` = no limit) |
| `kiosk_allowed_ips` | `LCARS_KIOSK_IPS` | Optional list of device IPs allowed to read and send messages (the server machine is always allowed) |
| `sounds_enabled` | `LCARS_SOUNDS` | Kiosk sound effects on or off (default `true`) |
| `sound_volume` | `LCARS_SOUND_VOLUME` | `0.0`–`1.0` (default `0.6`) |
| `tap_sounds` | `LCARS_TAP_SOUNDS` | Beep on every button press (default `true`) |
| `quiet_hours` | `LCARS_QUIET_HOURS` | Visual alerts only during this range (default `22:00-07:00`; may cross midnight; empty = off) |
| `incoming_repeat_minutes` | `LCARS_INCOMING_REPEAT_MINUTES` | Repeat the incoming alert while unread (default `5`, `0` = off) |
| `incoming_repeat_max` | `LCARS_INCOMING_REPEAT_MAX` | Most repeats per message (default `3`) |
| `sound_files` | `LCARS_SOUND_FILES_JSON` | Optional sound files in `sounds/` per cue, e.g. `{"incoming":"hail.mp3"}` |

`LCARS_CONFIG` can point to a config file somewhere else. Settings ending in `_JSON` take JSON text when set as environment variables.

### Getting a calendar feed URL

- **Apple iCloud**: Calendar app → right-click the calendar → *Share Calendar* → tick *Public Calendar* → copy the `webcal://` link.
- **Google Calendar**: Settings → the calendar → *Integrate calendar* → *Secret address in iCal format*.
- **Outlook.com / Microsoft 365**: Settings → Calendar → *Shared calendars* → *Publish a calendar* → copy the ICS link.

Anyone who has one of these links can read the calendar, so treat it like a password.

## Security notes

The server only serves the dashboard and static assets (`.html`, `.css`, `.js`, fonts, images). Requests for `config.json`, `messages.json`, `server.py`, dotfiles and anything else return 404. `/api/config` exposes only display values (the location label, sound settings, and, when messaging is on, crew and contact *names* and presets). It never exposes the calendar URL, the bot token or chat IDs. The server has no authentication, so run it on your home network only.

Text messages:

- **Who is accepted:** only messages from configured contacts are accepted. Anything else sent to the bot is dropped silently.
- **Kiosk-only sending:** the send endpoints need a custom header and a JSON body. Other websites can't send that without a CORS preflight, and the server never answers preflights.
- **Limits:** sending is rate-limited. You can also limit messaging to the kiosk's IP with `kiosk_allowed_ips`.
- **Message log:** messages are kept in `messages.json`, which is git-ignored, readable only by you, and deleted after `message_retention_hours`.

## Roadmap

- Modular themes (Lower Decks, TNG, Voyager)
- Multiple calendars and CalDAV
- Docker image for Raspberry Pi and Linux servers

## License

[MIT](LICENSE). The embedded Antonio font is under the SIL Open Font License. This is an unofficial fan project, not affiliated with CBS or Paramount.
