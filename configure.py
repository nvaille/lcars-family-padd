#!/usr/bin/env python3
"""
LCARS Family PADD - setup wizard (terminal). Standard library only.

  python3 configure.py location    weather location: look up a city, pick the right match
  python3 configure.py calendar    calendar feed (.ics / webcal) address, checked before saving
  python3 configure.py messaging   set up Telegram messaging: bot token, pair contacts,
                                   optional family group, crew names, test transmission
  python3 configure.py sounds      sound effects, volume, quiet hours, your own sound files
  python3 configure.py security    which devices may use messaging, and the send rate limit
  python3 configure.py show        print the current settings with secrets masked

In every question, pressing Enter keeps the value shown in [brackets].
Run it on the server machine, in its own terminal window. For messaging, stop the PADD
server first (only one program at a time can read a Telegram bot's updates).
Only the settings you change are written to config.json (a backup goes to config.json.bak).
"""

import contextlib
import copy
import getpass
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import urllib.parse
import urllib.request
import uuid

with contextlib.redirect_stdout(io.StringIO()):  # the server prints config notes on import
    import server

NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .'-]{0,19}$")
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,50}$")
RESERVED_NAMES = ("ALL", "ALL HANDS")
TEST_TEXT = "LCARS PADD ONLINE"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search?"
QUIET_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
SECTIONS = ("location", "calendar", "sounds", "security")
CUE_HELP = {
    "tap": "any button press",
    "incoming": "a new message arrives",
    "transmit": "a message is being sent",
    "sent": "a message was delivered",
    "ack": "someone replied to a message",
    "fail": "a message could not be sent",
    "open": "the comms screens open",
}
MISSING = object()


class Abort(Exception):
    pass


def http_get(url, timeout=15):
    """Download a URL as text (the same User-Agent the server uses for calendar feeds)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------

def load_raw(path):
    """The config file as written (unknown keys kept), or {} if there isn't one."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit("%s is not a JSON object; fix or move it first." % path)
    return data


def save_raw(path, data):
    """Back up the old file to <path>.bak, then write atomically with owner-only permissions."""
    if os.path.exists(path):
        backup = path + ".bak"
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # e.g. some Windows filesystems


def mask(value):
    value = str(value or "")
    if not value:
        return "(not set)"
    return "…" + value[-4:] if len(value) > 8 else "••••"


def describe(key, value):
    """A setting's value for printing, with secrets masked."""
    if value is MISSING:
        return "(removed)"
    if key in ("telegram_bot_token", "ics_feed_url"):
        return mask(value)
    if key == "telegram_group_chat_id":
        return "(set, hidden)" if value else "(not set)"
    if key == "contacts":
        contacts = value if isinstance(value, list) else []
        return ", ".join("%s (paired)" % c.get("name") for c in contacts if isinstance(c, dict)) or "(none)"
    if key == "crew":
        return ", ".join(server._as_list(value or [], "crew")) or "(none)"
    return json.dumps(value, ensure_ascii=False)


def summary_lines(raw):
    first = ["messaging_provider", "telegram_bot_token", "telegram_group_chat_id", "contacts", "crew"]
    lines = ["%-22s %s" % (key, describe(key, raw.get(key, ""))) for key in first]
    lines[0] = "%-22s %s" % ("messaging_provider", raw.get("messaging_provider") or "none")
    lines += ["%-22s %s" % (key, describe(key, raw[key])) for key in sorted(raw) if key not in first]
    return lines


def changed_keys(before, after):
    return sorted(k for k in set(before) | set(after) if before.get(k, MISSING) != after.get(k, MISSING))


def save_changes(path, before, raw, prompts):
    """Show what changed, ask, then write only those keys. True if saved."""
    keys = changed_keys(before, raw)
    if not keys:
        prompts.out("\nNo changes, so %s was left as it was." % os.path.basename(path))
        return False
    prompts.out("\nChanges:")
    for key in keys:
        prompts.out("  %-24s %s" % (key, describe(key, raw.get(key, MISSING))))
    if not prompts.yes("Save these changes to %s?" % os.path.basename(path)):
        prompts.out("Nothing saved.")
        return False
    fresh = load_raw(path)  # re-read, so only the keys changed here are touched
    for key in keys:
        if key in raw:
            fresh[key] = raw[key]
        else:
            fresh.pop(key, None)
    save_raw(path, fresh)
    prompts.out("Saved (previous version in %s.bak; file is readable only by you)." % os.path.basename(path))
    return True


def restart_hint(raw, check_server):
    port = os.environ.get("LCARS_PORT") or raw.get("port") or server.DEFAULTS["port"]
    if check_server(port):
        return ("The PADD server is running. Restart it so it uses the new settings:\n"
                "  press Ctrl+C in its window, then run  python3 server.py")
    return "Start the PADD server when you're ready:  python3 server.py"


def server_running(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def tls_hint(err):
    if "CERTIFICATE_VERIFY_FAILED" in str(err):
        return ("\n  Python can't check HTTPS certificates yet. On a Mac with Python from python.org,"
                "\n  run 'Install Certificates.command' in your /Applications/Python 3.x folder, then retry.")
    return ""


# ---------------------------------------------------------------------------
# Prompts shared by every section
# ---------------------------------------------------------------------------

def _number(value, cast, fallback):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return fallback


class Prompts(object):
    def __init__(self, ask=input, secret=getpass.getpass, out=print):
        self.ask = ask
        self.secret = secret
        self.out = out

    def yes(self, question, default=True):
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.ask("%s %s " % (question, hint)).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def ask_name(self, prompt, taken):
        while True:
            name = self.ask(prompt).strip().upper()
            if not name:
                return ""
            if name in RESERVED_NAMES:
                self.out("  %s is reserved for 'everyone'. Pick another name." % name)
            elif name in taken:
                self.out("  %s is already used. Pick another name." % name)
            elif not NAME_RE.match(name):
                self.out("  Use up to 20 letters, digits, spaces, dots, dashes or apostrophes.")
            else:
                return name

    def ask_text(self, prompt, current=""):
        answer = self.ask("%s%s: " % (prompt, " [%s]" % current if current else "")).strip()
        return answer or current

    def ask_number(self, prompt, current, lo, hi, cast=int):
        while True:
            answer = self.ask("%s [%s]: " % (prompt, current)).strip()
            if not answer:
                return current
            value = _number(answer, cast, None)
            if value is None:
                self.out("  Type a number.")
            elif lo <= value <= hi:
                return value
            else:
                self.out("  Pick a number from %s to %s." % (lo, hi))

    def pick(self, prompt, count):
        """Ask for a list number (1..count). Returns a 0-based index, or None for Enter."""
        while True:
            answer = self.ask(prompt).strip()
            if not answer:
                return None
            if answer.isdigit() and 1 <= int(answer) <= count:
                return int(answer) - 1
            self.out("  Type a number from 1 to %d, or press Enter to cancel." % count)

    def edit_list(self, title, items, show, actions):
        """A menu loop over a list. actions: [(letter, "[A]dd", fn(items))]. Enter or D ends it."""
        actions = list(actions)
        menu = "  ".join(label for _, label, _ in actions) + "  [D]one: "
        while True:
            self.out("\n%s:" % title)
            for i, item in enumerate(items):
                self.out("  %d. %s" % (i + 1, show(item)))
            if not items:
                self.out("  (none)")
            choice = self.ask(menu).strip().lower()
            if choice in ("", "d", "done"):
                return items
            for letter, _, fn in actions:
                if choice == letter:
                    fn(items)
                    break
            else:
                self.out("  Type one of the letters in [brackets].")

    def pick_item(self, items, verb):
        if not items:
            self.out("  There's nothing to %s." % verb)
            return None
        return self.pick("  Number to %s (Enter to cancel): " % verb, len(items))


# ---------------------------------------------------------------------------
# Sections that don't need Telegram: location, calendar, sounds, security
# ---------------------------------------------------------------------------

class Setup(Prompts):
    def __init__(self, raw, ask=input, secret=getpass.getpass, out=print, fetch=http_get,
                 sounds_dir=server.SOUNDS_DIR):
        Prompts.__init__(self, ask, secret, out)
        self.raw = raw
        self.fetch = fetch
        self.sounds_dir = sounds_dir

    def current(self, key):
        return copy.deepcopy(self.raw[key] if key in self.raw else server.DEFAULTS[key])

    def set(self, key, value):
        """Write a setting only if it really changes, so untouched settings stay as they were."""
        if value != self.current(key):
            self.raw[key] = value

    def env_note(self, keys):
        names = [server.ENV_VARS[k] for k in keys if os.environ.get(server.ENV_VARS[k]) is not None]
        if names:
            self.out("Note: %s %s set in this computer's environment and override%s config.json."
                     % (", ".join(names), "is" if len(names) == 1 else "are", "s" if len(names) == 1 else ""))

    # ----- location -----

    def section_location(self):
        self.out("\n== Location (for the weather panel) ==")
        self.env_note(("latitude", "longitude", "location_name"))
        lat = _number(self.current("latitude"), float, 0.0)
        lon = _number(self.current("longitude"), float, 0.0)
        if lat or lon:
            self.out("Now: %s (%.4f, %.4f)" % (self.current("location_name") or "(no label)", lat, lon))
        else:
            self.out("Not set yet.")
        while True:
            query = self.ask("City or town to look up (Enter keeps the current location, "
                             "M to type coordinates): ").strip()
            if not query:
                return
            if query.lower() == "m":
                self._manual_location(lat, lon)
                return
            matches = self._geocode(query)
            if matches is None:
                continue
            if not matches:
                self.out("  No places found for %r. Check the spelling, or type just the city name." % query)
                continue
            for i, r in enumerate(matches):
                self.out("  %d. %s  (%.4f, %.4f)" % (i + 1, place_label(r), r["latitude"], r["longitude"]))
            idx = self.pick("Pick a number (Enter to search again): ", len(matches))
            if idx is None:
                continue
            r = matches[idx]
            label = self.ask_text("Label shown on the PADD", short_label(r))
            self.set("latitude", round(float(r["latitude"]), 4))
            self.set("longitude", round(float(r["longitude"]), 4))
            self.set("location_name", label)
            self.out("  Location: %s" % label)
            return

    def _geocode(self, query):
        """Open-Meteo place search. 'Springfield, IL' searches Springfield and prefers IL matches."""
        name, _, region = query.partition(",")
        name, region = name.strip(), region.strip().lower()
        if len(name) < 2:
            self.out("  Type at least 2 letters.")
            return None
        url = GEOCODE_URL + urllib.parse.urlencode(
            {"name": name, "count": 10, "language": "en", "format": "json"})
        try:
            data = json.loads(self.fetch(url))
        except Exception as e:
            self.out("  Couldn't look that up: %s%s\n  (Type M to enter coordinates by hand instead.)"
                     % (e, tls_hint(e)))
            return None
        results = [r for r in (data.get("results") or []) if isinstance(r, dict)
                   and isinstance(r.get("latitude"), (int, float)) and isinstance(r.get("longitude"), (int, float))]
        if region:
            preferred = [r for r in results if any(str(r.get(f) or "").lower().startswith(region)
                                                   for f in ("admin1", "country", "country_code"))]
            results = preferred or results
        return results[:8]

    def _manual_location(self, lat, lon):
        self.out("Tip: find coordinates by right-clicking a spot in Google Maps.")
        lat = self.ask_number("Latitude (-90 to 90)", lat, -90.0, 90.0, float)
        lon = self.ask_number("Longitude (-180 to 180)", lon, -180.0, 180.0, float)
        label = self.ask_text("Label shown on the PADD", self.current("location_name"))
        self.set("latitude", round(lat, 4))
        self.set("longitude", round(lon, 4))
        self.set("location_name", label)

    # ----- calendar -----

    def section_calendar(self):
        self.out("\n== Calendar ==")
        self.env_note(("ics_feed_url",))
        current = str(self.raw.get("ics_feed_url") or "")
        if current:
            self.out("A calendar feed is saved (%s)." % mask(current))
            if self.yes("Keep it?"):
                if self.yes("Check that it still works?"):
                    self._check_feed(current)
                return
        else:
            self.out("Not set yet.")
        self.out("Paste your calendar's private feed address. Where to find it:\n"
                 "  Google:  calendar.google.com > Settings > your calendar > Integrate calendar >\n"
                 "           'Secret address in iCal format'\n"
                 "  iCloud:  Calendar app > right-click the calendar > Share > Public Calendar > copy link\n"
                 "  Outlook: Settings > Calendar > Shared calendars > Publish a calendar > ICS link\n"
                 "Anyone with this address can read your calendar, so typing is hidden; nothing will appear.")
        for _ in range(5):
            url = self.secret("Calendar feed address (Enter to cancel): ").strip()
            if not url:
                if current and self.yes("Remove the saved calendar feed?", default=False):
                    self.set("ics_feed_url", "")
                return
            if not url.lower().startswith(("https://", "http://", "webcal://")):
                self.out("  That should start with https:// or webcal://")
                continue
            if self._check_feed(url) or self.yes("Save it anyway?", default=False):
                self.set("ics_feed_url", url)
                return
        self.out("Calendar unchanged.")

    def _check_feed(self, url):
        fetch_url = "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url
        self.out("  Checking the feed...")
        try:
            text = self.fetch(fetch_url)
        except Exception as e:
            reason = str(e).replace(fetch_url, "(feed address)").replace(url, "(feed address)")
            self.out("  Couldn't download the feed: %s%s" % (reason, tls_hint(e)))
            return False
        if "BEGIN:VCALENDAR" not in text.upper():
            self.out("  That address answered, but what came back isn't a calendar (.ics) feed.")
            return False
        try:
            events = server.collect_upcoming(text, everything=True)
        except Exception as e:
            self.out("  The feed downloaded but couldn't be read (%s)." % type(e).__name__)
            return False
        days = server.LOOKAHEAD_DAYS
        if not events:
            self.out("  Feed OK, but it has no upcoming events in the next %d days." % days)
        else:
            ev = events[0]
            self.out("  Feed OK: found %d upcoming event%s in the next %d days.\n  Next: %s %s  %s"
                     % (len(events), "" if len(events) == 1 else "s", days, ev["date"], ev["time"], ev["summary"]))
        return True

    # ----- sounds -----

    def section_sounds(self):
        self.out("\n== Sounds ==")
        self.env_note(("sounds_enabled", "sound_volume", "tap_sounds", "quiet_hours",
                       "incoming_repeat_minutes", "incoming_repeat_max", "sound_files"))
        enabled = self.yes("Play sound effects on the PADD?", default=server._as_bool(self.current("sounds_enabled")))
        self.set("sounds_enabled", enabled)
        if not enabled:
            return
        volume = int(round(_number(self.current("sound_volume"), float, 0.6) * 100))
        volume = self.ask_number("Volume, 0-100", min(100, max(0, volume)), 0, 100)
        self.set("sound_volume", round(volume / 100.0, 2))
        self.set("tap_sounds", self.yes("Beep on every button press?",
                                        default=server._as_bool(self.current("tap_sounds"))))
        self.set("quiet_hours", self._ask_quiet_hours(str(self.current("quiet_hours") or "")))
        minutes = self.ask_number("Repeat the new-message alert every how many minutes while it's unread "
                                  "(0 = never)", _number(self.current("incoming_repeat_minutes"), int, 5), 0, 120)
        self.set("incoming_repeat_minutes", minutes)
        if minutes:
            self.set("incoming_repeat_max", self.ask_number(
                "Most repeats per message", _number(self.current("incoming_repeat_max"), int, 3), 0, 50))
        self._sound_files()

    def _ask_quiet_hours(self, current):
        self.out("Quiet hours: new messages still flash on screen, but make no sound.")
        while True:
            answer = self.ask("Quiet hours as HH:MM-HH:MM, or OFF [%s]: " % (current or "OFF")).strip()
            if not answer:
                return current
            if answer.lower() in ("off", "none", "no"):
                return ""
            m = QUIET_RE.match(answer.replace(" ", ""))
            if m:
                h1, m1, h2, m2 = [int(g) for g in m.groups()]
                if h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60:
                    return "%02d:%02d-%02d:%02d" % (h1, m1, h2, m2)
            self.out("  Use 24-hour times like 22:00-07:00.")

    def _sound_files(self):
        try:
            files = sorted(f for f in os.listdir(self.sounds_dir)
                           if f.lower().endswith(server.SOUND_EXTENSIONS) and not f.startswith("."))
        except OSError:
            files = []
        mapping = self.current("sound_files")
        mapping = dict(mapping) if isinstance(mapping, dict) else {}
        if not files:
            self.out("No sound files in the sounds/ folder, so the PADD uses its built-in tones.\n"
                     "(To use your own, see sounds/README.md, then run:  python3 configure.py sounds)")
            return
        self.out("Found %d sound file%s in sounds/." % (len(files), "" if len(files) == 1 else "s"))
        if not self.yes("Choose which files play for each cue?", default=False):
            return
        cues = list(server.SOUND_CUES)
        while True:
            self.out("")
            for i, cue in enumerate(cues):
                self.out("  %d. %-9s %-30s %s" % (i + 1, cue, "(" + CUE_HELP[cue] + ")",
                                                 mapping.get(cue, "built-in tone")))
            idx = self.pick("Cue number to change (Enter when done): ", len(cues))
            if idx is None:
                break
            for j, name in enumerate(files):
                self.out("    %d. %s" % (j + 1, name))
            self.out("    0. built-in tone")
            answer = self.ask("  File number for %s (Enter = no change): " % cues[idx]).strip()
            if answer == "0":
                mapping.pop(cues[idx], None)
            elif answer.isdigit() and 1 <= int(answer) <= len(files):
                mapping[cues[idx]] = files[int(answer) - 1]
            elif answer:
                self.out("  Type a number from the list.")
        self.set("sound_files", mapping)

    # ----- kiosk security -----

    def section_security(self):
        self.out("\n== Kiosk security ==")
        self.env_note(("kiosk_allowed_ips", "send_rate_limit_per_hour"))
        self.out("Normally any device on your Wi-Fi that opens the PADD page can read and send messages.\n"
                 "You can limit that to the iPad's IP address. The server machine is always allowed.\n"
                 "  Find the iPad's address: Settings > Wi-Fi > tap (i) next to your network > IP Address.\n"
                 "  Tip: reserve that address for the iPad in your router, so it never changes.")
        ips = [str(ip) for ip in server._as_list(self.current("kiosk_allowed_ips"), "kiosk_allowed_ips")]

        def add(items):
            answer = self.ask("  IP address to allow, e.g. 192.168.1.42 (Enter to cancel): ").strip()
            if not answer:
                return
            try:
                ip = str(ipaddress.ip_address(answer))
            except ValueError:
                self.out("  That isn't an IP address. It looks like four numbers with dots between them.")
                return
            if ip in items:
                self.out("  %s is already on the list." % ip)
            else:
                items.append(ip)

        def remove(items):
            idx = self.pick_item(items, "remove")
            if idx is not None:
                items.pop(idx)

        self.edit_list("Devices allowed to use messaging (none listed = any device on your network)",
                       ips, str, [("a", "[A]dd", add), ("r", "[R]emove", remove)])
        self.set("kiosk_allowed_ips", ips)
        rate = _number(self.current("send_rate_limit_per_hour"), int, 10)
        self.set("send_rate_limit_per_hour", self.ask_number(
            "Most messages the PADD may send per hour (0 = no limit)", max(0, rate), 0, 1000))


def place_label(r):
    parts = []
    for field in ("name", "admin1", "country"):
        value = str(r.get(field) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def short_label(r):
    name = str(r.get("name") or "").strip()
    region = str(r.get("admin1") or r.get("country") or "").strip()
    return "%s, %s" % (name, region) if region and region != name else name


# ---------------------------------------------------------------------------
# The messaging wizard
# ---------------------------------------------------------------------------

class Wizard(Prompts):
    def __init__(self, raw, ask=input, secret=getpass.getpass, out=print,
                 make_transport=server.TelegramTransport):
        Prompts.__init__(self, ask, secret, out)
        self.raw = raw
        self.make_transport = make_transport
        self.transport = None
        self.bot_username = ""

    # ----- steps -----

    def step_token(self):
        self.out("\n== 1. Telegram bot ==")
        existing = str(self.raw.get("telegram_bot_token") or "")
        if existing and self.yes("Keep the saved bot token (%s)?" % mask(existing)):
            if self._check_token(existing):
                return
            self.out("  The saved token didn't work; paste a new one.")
        self.out("If you don't have a bot yet:\n"
                 "  1. In Telegram, open a chat with @BotFather and send /newbot\n"
                 "  2. Pick a display name (e.g. Home PADD) and a username ending in 'bot'\n"
                 "  3. BotFather replies with a token like 123456789:AA...\n"
                 "Paste it below. Typing is hidden; nothing will appear. Press Enter when done.")
        for _ in range(3):
            token = self.secret("Bot token: ").strip()
            if not token:
                raise Abort("No token entered.")
            if not TOKEN_RE.match(token):
                self.out("  That doesn't look like a bot token (digits, a colon, then ~35 characters).")
                continue
            if self._check_token(token):
                self.raw["telegram_bot_token"] = token
                return
        raise Abort("Could not verify a bot token.")

    def _check_token(self, token):
        transport = self.make_transport(token)
        try:
            me = transport.get_me()
        except server.TransportError as e:
            if e.code in (401, 404):
                self.out("  Telegram rejected that token.")
            else:
                self.out("  Couldn't reach Telegram: %s%s" % (e, tls_hint(e)))
            return False
        self.transport = transport
        self.bot_username = me.get("username", "")
        self.out("  Connected to @%s" % self.bot_username)
        return True

    def _poll(self, timeout=10):
        try:
            return self.transport.poll(timeout=timeout)
        except server.TransportError as e:
            if e.code == 409:
                raise Abort("Another program is reading this bot's updates. Stop the PADD server "
                            "(Ctrl+C in its window) and run the wizard again.\n  Telegram said: %s" % e)
            self.out("  Telegram error: %s%s" % (e, tls_hint(e)))
            return []

    def step_contacts(self):
        self.out("\n== 2. Contacts (the people who get PADD messages) ==")
        contacts = [c for c in (self.raw.get("contacts") or []) if isinstance(c, dict)]
        if contacts:
            names = ", ".join(str(c.get("name")) for c in contacts)
            if not self.yes("Keep the paired contacts (%s)?" % names):
                contacts = []
        if contacts and not self.yes("Pair more contacts now?", default=False):
            self.raw["contacts"] = contacts
            return
        known = dict((str(c.get("telegram_chat_id")), str(c.get("name"))) for c in contacts)
        taken = set(known.values()) | set(server._as_list(self.raw.get("crew") or [], "crew"))
        taken = set(n.upper() for n in taken)
        skipped = set()
        self.out("Ask each person to open https://t.me/%s in Telegram and tap START.\n"
                 "Waiting for them... (press Ctrl+C when everyone is paired)" % self.bot_username)
        try:
            while True:
                for ev in self._poll():
                    if ev["type"] != "message" or ev.get("chat_type") != "private":
                        continue
                    if ev["sender"] in known or ev["sender"] in skipped:
                        continue
                    who = ev.get("first_name") or "someone"
                    self.out("\n  New contact: %s" % who)
                    name = self.ask_name("  Display name for %s (e.g. MOM), or Enter to skip: " % who, taken)
                    if not name:
                        skipped.add(ev["sender"])
                        continue
                    known[ev["sender"]] = name
                    taken.add(name)
                    contacts.append({"name": name, "telegram_chat_id": ev["sender"]})
                    self._notify(ev["sender"], "PADD link established. You are %s." % name)
                    self.out("  Paired %s." % name)
                    if not self.yes("  Wait for another contact?"):
                        raise KeyboardInterrupt
        except KeyboardInterrupt:
            self.out("")
        if not contacts:
            raise Abort("No contacts paired.")
        self.raw["contacts"] = contacts
        self.out("Contacts: %s" % ", ".join(c["name"] for c in contacts))

    def step_group(self):
        self.out("\n== 3. Family group (optional) ==")
        current = str(self.raw.get("telegram_group_chat_id") or "")
        if current:
            if self.yes("A family group is set for ALL HANDS. Keep it?"):
                return
            self.raw["telegram_group_chat_id"] = ""
        self.out("Without a group, ALL HANDS sends a private message to every contact.")
        if not self.yes("Use a Telegram family group for ALL HANDS instead?", default=False):
            return
        self.out("1. Add @%s to your family group.\n"
                 "2. In the group, send:  /padd\n"
                 "   (Tip: to let the bot see ordinary group messages, turn privacy mode off in\n"
                 "    @BotFather: /setprivacy > Disable. Otherwise parents should use Reply.)\n"
                 "Waiting for /padd... (Ctrl+C to skip)" % self.bot_username)
        try:
            while True:
                for ev in self._poll():
                    text = (ev.get("text") or "").strip().lower()
                    if ev["type"] == "message" and ev.get("chat_type") in ("group", "supergroup") \
                            and (text == "/padd" or text.startswith("/padd@") or text.startswith("/padd ")):
                        self.raw["telegram_group_chat_id"] = ev["chat"]
                        self._notify(ev["chat"], "PADD link established. This group is ALL HANDS.")
                        self.out("  Captured group %r." % (ev.get("chat_title") or "group"))
                        return
        except KeyboardInterrupt:
            self.out("\n  Skipped the family group.")

    def step_crew(self):
        self.out("\n== 4. Crew (the kids who send from the PADD) ==")
        crew = [n.upper() for n in server._as_list(self.raw.get("crew") or [], "crew")]
        if crew and self.yes("Keep the crew (%s)?" % ", ".join(crew)):
            self.raw["crew"] = crew
            return
        contact_names = set(str(c.get("name")).upper() for c in self.raw.get("contacts") or [])
        crew = []
        self.out("Enter each kid's name, one at a time. Press Enter on an empty line when done.")
        while True:
            name = self.ask_name("  Crew name: ", contact_names | set(crew))
            if not name:
                if crew:
                    break
                self.out("  At least one crew member is needed.")
                continue
            crew.append(name)
        self.raw["crew"] = crew

    def step_test(self):
        self.out("\n== 6. Test transmission ==")
        if not self.yes("Send '%s' to every contact and wait for a reply tap?" % TEST_TEXT):
            return
        nonce = uuid.uuid4().hex[:8]
        buttons = [(label, "t:%s:%d" % (nonce, i))
                   for i, label in enumerate(self.raw.get("quick_replies") or server.DEFAULTS["quick_replies"])]
        sent = 0
        for c in self.raw["contacts"]:
            try:
                self.transport.send(c["telegram_chat_id"], TEST_TEXT, buttons)
                sent += 1
                self.out("  Sent to %s." % c["name"])
            except server.TransportError as e:
                self.out("  Could not send to %s: %s" % (c["name"], e))
        if not sent:
            self.out("  Nothing was sent.")
            return
        names = dict((str(c["telegram_chat_id"]), c["name"]) for c in self.raw["contacts"])
        self.out("Ask a contact to tap one of the buttons under the message.\n"
                 "Waiting up to 2 minutes... (Ctrl+C to skip)")
        try:
            for _ in range(12):
                for ev in self._poll(timeout=10):
                    if ev["type"] == "callback" and ev["data"].startswith("t:%s:" % nonce):
                        self.transport.answer_callback(ev["callback_id"], "TWO-WAY LINK CONFIRMED")
                        who = names.get(ev["sender"])
                        if who:
                            self.out("  ACKNOWLEDGED BY %s. Two-way link confirmed." % who)
                            return
            self.out("  No tap yet. Sending works; check the buttons later.")
        except KeyboardInterrupt:
            self.out("\n  Skipped waiting.")

    def _notify(self, chat_id, text):
        try:
            self.transport.send(chat_id, text)
        except server.TransportError:
            pass

    def finish(self):
        """Confirm the updates the wizard read, so the server won't see them again."""
        if self.transport is not None and self.transport.cursor is not None:
            try:
                self.transport.poll(timeout=0)
            except server.TransportError:
                pass


def run_messaging(path, ask=input, secret=getpass.getpass, out=print,
                  make_transport=server.TelegramTransport, check_server=server_running):
    raw = load_raw(path)
    out("LCARS PADD setup: messaging\nConfig file: %s" % path)
    port = os.environ.get("LCARS_PORT") or raw.get("port") or server.DEFAULTS["port"]
    if check_server(port):
        out("\nWARNING: something is running on port %s, probably the PADD server.\n"
            "Only one program at a time can read the bot's updates, so stop the server first\n"
            "(press Ctrl+C in its window)." % port)
        if not Wizard(raw, ask, secret, out).yes("Continue anyway?", default=False):
            out("Nothing changed.")
            return 1
    if os.environ.get("LCARS_TELEGRAM_TOKEN") or os.environ.get("LCARS_CONTACTS_JSON"):
        out("\nNote: LCARS_* environment variables are set; they override config.json.")

    wiz = Wizard(raw, ask, secret, out, make_transport)
    saved = False
    try:
        wiz.step_token()
        wiz.step_contacts()
        wiz.step_group()
        wiz.step_crew()
        raw["messaging_provider"] = "telegram"

        out("\n== 5. Save ==")
        for line in summary_lines(raw):
            out("  " + line)
        if not wiz.yes("Save these settings to %s?" % os.path.basename(path)):
            out("Nothing saved.")
            return 1
        save_raw(path, raw)
        saved = True
        out("Saved (previous version in %s.bak; file is readable only by you)." % os.path.basename(path))

        wiz.step_test()
    except Abort as e:
        out("\n%s%s" % (e, "" if saved else " Nothing saved."))
        return 1
    except (KeyboardInterrupt, EOFError):
        out("\nCancelled.%s" % ("" if saved else " Nothing saved."))
        return 1
    finally:
        wiz.finish()
    out("\nDone. Start the PADD server again:  python3 server.py")
    return 0


def run_section(path, name, ask=input, secret=getpass.getpass, out=print, fetch=http_get,
                sounds_dir=server.SOUNDS_DIR, check_server=server_running):
    raw = load_raw(path)
    before = copy.deepcopy(raw)
    setup = Setup(raw, ask, secret, out, fetch, sounds_dir)
    out("LCARS PADD setup: %s\nConfig file: %s\n(Press Enter to keep the value in [brackets]. "
        "Ctrl+C quits without saving.)" % (name, path))
    try:
        getattr(setup, "section_" + name)()
        saved = save_changes(path, before, raw, setup)
    except Abort as e:
        out("\n%s Nothing saved." % e)
        return 1
    except (KeyboardInterrupt, EOFError):
        out("\nCancelled. Nothing saved.")
        return 1
    if saved:
        out("\n" + restart_hint(raw, check_server))
    return 0 if saved or not changed_keys(before, raw) else 1


def run_show(path, out=print):
    raw = load_raw(path)
    out("Config file: %s%s" % (path, "" if raw else " (not found)"))
    for line in summary_lines(raw):
        out("  " + line)
    return 0


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    path = server.config_path()
    if command == "messaging":
        return run_messaging(path)
    if command == "show":
        return run_show(path)
    if command in SECTIONS:
        return run_section(path, command)
    print(__doc__.strip())
    return 0 if command in ("", "-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
