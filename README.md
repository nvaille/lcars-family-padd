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

`LCARS_CONFIG` can point to a config file somewhere else.

### Getting a calendar feed URL

- **Apple iCloud**: Calendar app → right-click the calendar → *Share Calendar* → tick *Public Calendar* → copy the `webcal://` link.
- **Google Calendar**: Settings → the calendar → *Integrate calendar* → *Secret address in iCal format*.
- **Outlook.com / Microsoft 365**: Settings → Calendar → *Shared calendars* → *Publish a calendar* → copy the ICS link.

Anyone who has one of these links can read the calendar, so treat it like a password.

## Security notes

The server only serves the dashboard and static assets (`.html`, `.css`, `.js`, fonts, images). Requests for `config.json`, `server.py`, dotfiles and anything else return 404. `/api/config` exposes only the location label. The server has no authentication, so run it on your home network only.

## Roadmap

- Modular themes (Lower Decks, TNG, Voyager)
- Multiple calendars and CalDAV
- Docker image for Raspberry Pi and Linux servers

## License

[MIT](LICENSE). The embedded Antonio font is under the SIL Open Font License. This is an unofficial fan project, not affiliated with CBS or Paramount.
