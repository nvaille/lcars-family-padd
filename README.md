# LCARS Family PADD

A retro-futuristic, LCARS-style family dashboard for wall-mounted tablets and kiosks. It shows a clock, local weather, a rotating Stoic quote, and upcoming events from any `.ics` calendar feed.

It's built to run on very old hardware (the reference kiosk is a 1st-gen iPad Mini on iOS 9), so the front end uses no CSS variables, grid, flex `gap`, or ES6.

## Features

- **Chronometer**: large clock and date
- **Environmental Sensors**: current conditions, hourly strip and 5-day forecast from [Open-Meteo](https://open-meteo.com) (no API key needed)
- **Mission Log**: upcoming events from an iCloud, Google, Outlook or any other `.ics` feed, with recurring-event support (RRULE, EXDATE, RECURRENCE-ID)
- **Stoic Directive**: a quote that rotates hourly
- **Family comms**: kids send preset messages ("I'M HOME FROM SCHOOL") to parents' phones over Telegram, and parents reply. Includes LCARS-style sound cues and quiet hours
- Scales the 1024×748 design to any screen; auto-reloads nightly to keep old Safari stable (`nightly_reload`)

## Quick start

Requires Python 3.9+. Standard library only, so there's nothing to install.

```bash
git clone https://github.com/nvaille/lcars-family-padd.git
cd lcars-family-padd
python3 configure.py   # guided setup: location, calendar, optional messaging, sounds
python3 server.py
```

The setup wizard asks one question at a time and saves to `config.json`. You can also copy `config.example.json` to `config.json` and edit it by hand.

Open `http://<server-ip>:8000` on the kiosk. On iPad, use **Share → Add to Home Screen** for full-screen mode (it gets an LCARS icon, `apple-touch-icon.png`).

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
| `nightly_reload` | `LCARS_NIGHTLY_RELOAD` | Reload the kiosk page at 3 a.m. to keep old Safari stable (default `true`). Each reload needs a tap to re-arm audio |
| `messaging_provider` | `LCARS_MESSAGING_PROVIDER` | `none` (default) or `telegram`. Turns family text messages on |
| `telegram_bot_token` | `LCARS_TELEGRAM_TOKEN` | Your bot's token from @BotFather. **Secret** |
| `telegram_group_chat_id` | `LCARS_TELEGRAM_GROUP_ID` | Optional family group used for ALL HANDS. **Private** |
| `contacts` | `LCARS_CONTACTS_JSON` | People who get messages: `[{"name":"MOM","telegram_chat_id":"…"}]`. **Private** |
| `crew` | `LCARS_CREW` | Kids who send from the kiosk: a JSON list or `SAM,RILEY` |
| `padd_sender_name` | `LCARS_PADD_SENDER_NAME` | How the kiosk signs messages (default `HOME PADD`) |
| `message_presets` | `LCARS_MESSAGE_PRESETS_JSON` | Preset messages, each with an optional default recipient: `[{"text":"…","to":"ALL"}]` |
| `quick_replies` | `LCARS_QUICK_REPLIES_JSON` | Telegram reply buttons (default `["GOT IT","CALL ME","ON MY WAY"]`) |
| `allow_custom_messages` | `LCARS_ALLOW_CUSTOM_MESSAGES` | Allow typed messages, not just presets (default `false`). Adds a **CUSTOM MESSAGE** button after the presets |
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
| `sound_files` | `LCARS_SOUND_FILES_JSON` | Optional sound files in `sounds/` per cue, e.g. `{"incoming":"hail.mp3"}`. Cues: `tap`, `incoming`, `transmit`, `sent`, `ack`, `fail`, `open`, `lcars` (the LCARS title button) |

`LCARS_CONFIG` can point to a config file somewhere else. Settings ending in `_JSON` take JSON text when set as environment variables.

### Setup wizard

`configure.py` is a terminal wizard. Run it on the server machine. Each command sets up one part, so you can change one thing without going through everything:

| Command | What it sets up |
|---|---|
| `python3 configure.py` | Everything below, one section after another, with a single save at the end |
| `python3 configure.py location` | Weather location: type a city (e.g. `Springfield, IL`) and pick the right match |
| `python3 configure.py calendar` | Calendar feed address. Typing is hidden, and the feed is checked before saving ("Found 12 upcoming events…") |
| `python3 configure.py messaging` | Telegram bot, contacts, family group and crew (see below) |
| `python3 configure.py contacts` | Add (pair), remove or rename the people who get messages |
| `python3 configure.py crew` | Add, remove, rename or reorder the kids who send messages |
| `python3 configure.py presets` | Preset messages (text, order, default recipient), typed messages on/off, reply buttons and the PADD's signature |
| `python3 configure.py sounds` | Sound on/off, volume, quiet hours, alert repeats, and your own sound files |
| `python3 configure.py security` | Which devices may use messaging (by IP address) and the hourly send limit |
| `python3 configure.py test` | Sends a test message to every contact and waits for a reply tap |
| `python3 configure.py show` | Prints your settings with secrets masked |

How it behaves:

- **Keeping values:** pressing Enter keeps the value shown in `[brackets]`.
- **Lists:** contacts, crew, presets and reply buttons use a menu like `[A]dd [R]emove re[N]ame [D]one`. Changing one item leaves the others alone.
- **Contacts and presets stay in step:** renaming a contact updates any preset aimed at them. Removing a contact makes their presets ask HAIL WHOM? instead.
- **Saving:** before saving, the wizard lists what's changing and asks first. Only those settings are written. Everything else in `config.json` stays as it was, and the previous version is kept as `config.json.bak`. Both files are readable only by you.
- **Quitting:** Ctrl+C quits without saving.
- **Restart:** the server reads `config.json` when it starts, so restart it after saving. The wizard reminds you.

### Text messages (Telegram)

Kids can send preset messages ("I'M HOME FROM SCHOOL") from the PADD to parents' phones, and parents can reply. Messages go through a free Telegram bot. Everyone who receives PADD messages needs the Telegram app.

1. **Stop the PADD server**, if it's running (Ctrl+C in its window). Only one program at a time can read the bot's messages.
2. **Run the wizard:** `python3 configure.py messaging`
   - It shows how to create a bot with @BotFather, then asks for the token. The token is typed hidden and checked with Telegram.
   - It pairs each contact. They open the bot's link and tap START, then you give them a display name (MOM, DAD…).
   - Optionally, it links a family group for ALL HANDS: add the bot to the group and send `/padd` there.
   - It asks for the crew (the kids' names), shows what's changing, and saves to `config.json`. The previous version is kept as `config.json.bak`.
   - Finally, it sends a test transmission and waits for someone to tap a reply button.
3. **Start the server again:** `python3 server.py`

Running `messaging` again shows menus for your existing contacts and crew instead of starting over. To pair one more person later, use `python3 configure.py contacts` and choose `[A]dd`. Only adding a contact or the test needs the server stopped. Renaming and removing work any time. To change which presets the kids see, use `python3 configure.py presets`.

### Sounds and quiet hours

The PADD plays short original tones when you tap buttons, open a channel, send a message, and when a message arrives. No audio files are needed.

- **Arming the audio:** iPads only allow sound after the screen has been touched, and that has to happen again after every page reload (including the nightly 3 a.m. reload). Until then, the pill in the top-right reads **AUDIO STANDBY · TOUCH TO ARM**, and new messages are visual only. One tap anywhere arms it, and the pill switches to **AUDIO ONLINE**.
- **Quiet hours:** the default is 22:00 to 07:00 (`quiet_hours`). During quiet hours the pill reads **SILENT RUNNING**, the new-message chime is off, and button sounds play at 30% volume. At the end of quiet hours, the chime plays once if anything is still unread.
- **Repeats:** while a message is unread, the chime repeats every `incoming_repeat_minutes`, up to `incoming_repeat_max` times.
- **Sound test:** press and hold the audio pill for about a second. It plays every cue in turn.
- **Your own sounds:** see [`sounds/README.md`](sounds/README.md).

### Getting a calendar feed URL

- **Apple iCloud**: Calendar app → right-click the calendar → *Share Calendar* → tick *Public Calendar* → copy the `webcal://` link.
- **Google Calendar**: Settings → the calendar → *Integrate calendar* → *Secret address in iCal format*.
- **Outlook.com / Microsoft 365**: Settings → Calendar → *Shared calendars* → *Publish a calendar* → copy the ICS link.

Anyone who has one of these links can read the calendar, so treat it like a password.

## iPad kiosk setup

These steps are for a wall-mounted iPad (the reference kiosk is a 1st-gen iPad Mini on iOS 9):

1. **Open the dashboard.** In Safari, go to `http://<server-ip>:8000`, then use **Share → Add to Home Screen** and open it from the new icon (full-screen).
2. **Keep the screen on:** Settings → General → **Auto-Lock → Never**. This makes sure message alerts are always seen.
3. **Make sounds audible.** The iPad's mute silences all web sounds.
   - Settings → General → set **Use Side Switch to: Lock Rotation**.
   - Make sure mute is off in Control Center (swipe up from the bottom).
4. **Turn the volume up** with the side buttons.
5. **Arm the audio:** tap the screen once after each reload. The audio pill should say AUDIO ONLINE. Press and hold it to hear the sound test.
6. **Optional:** use Settings → General → Accessibility → **Guided Access** to lock the iPad to the dashboard.

To load a new version of the dashboard, tap the **LCARS 2247** title. It reloads the page.

## Security notes

The server only serves the dashboard and static assets (`.html`, `.css`, `.js`, fonts, images), plus any sound files you list in `sound_files` (only from `sounds/`). Requests for `config.json`, `messages.json`, `server.py`, dotfiles and anything else return 404. `/api/config` exposes only display values (the location label, sound settings, and, when messaging is on, crew and contact *names* and presets). It never exposes the calendar URL, the bot token or chat IDs. The server has no authentication, so run it on your home network only.

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
