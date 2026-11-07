"""
LCARS Family PADD - kiosk server.

Serves index.html and two JSON endpoints used by the dashboard:
  /api/events   upcoming events from an .ics calendar feed
  /api/weather  Open-Meteo forecast for the configured coordinates
  /api/config   non-secret display settings (e.g. location label)
  /api/messages family text messages via Telegram (only when messaging is configured)

Settings come from config.json (next to this file, or the path in LCARS_CONFIG),
with environment variables taking precedence. See config.example.json.
Standard library only; Python 3.9+ recommended (3.7+ works, without TZID support).
"""

import functools
import json
import os
import re
import threading
import time as _time  # 'time' is datetime.time below
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, date, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # older Python: TZID events are treated as server-local time
    ZoneInfo = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "ics_feed_url": "",          # your private .ics / webcal feed (keep secret)
    "latitude": 0.0,
    "longitude": 0.0,
    "location_name": "",         # label shown above the weather panel
    "port": 8000,
    "max_events": 5,             # how many events the kiosk receives
    "lookahead_days": 60,        # how far ahead recurring events are expanded
    "timed_default_minutes": 60, # assumed length of a timed event with no DTEND/DURATION
    "nightly_reload": True,      # kiosk reloads itself at 3 a.m. to keep old Safari stable

    # --- text messages (see docs/text-messages-plan.md) ---
    "messaging_provider": "none",      # none | telegram  (twilio: future adapter)
    "telegram_bot_token": "",          # SECRET
    "telegram_group_chat_id": "",      # optional ALL HANDS group (private)
    "contacts": [],                    # [{"name": "MOM", "telegram_chat_id": "..."}] (private)
    "crew": [],                        # kids who send from the kiosk, e.g. ["SAM", "RILEY"]
    "padd_sender_name": "HOME PADD",
    "message_presets": [
        {"text": "I'M HOME FROM SCHOOL", "to": "ALL"},
        {"text": "HOME SAFE", "to": "ALL"},
        {"text": "PLEASE CALL HOME"},
        {"text": "NEED A RIDE"},
        {"text": "AT A FRIEND'S"},
        {"text": "ALL GOOD"},
    ],
    "quick_replies": ["GOT IT", "CALL ME", "ON MY WAY"],
    "allow_custom_messages": False,
    "message_retention_hours": 24,
    "max_message_length": 160,
    "send_rate_limit_per_hour": 10,    # 0 = unlimited
    "kiosk_allowed_ips": [],           # empty = any device on the network

    # --- sounds (used by the kiosk; see plan section 8) ---
    "sounds_enabled": True,
    "sound_volume": 0.6,
    "tap_sounds": True,
    "quiet_hours": "22:00-07:00",      # "" = no quiet hours
    "incoming_repeat_minutes": 5,      # 0 = no repeats
    "incoming_repeat_max": 3,
    "sound_files": {},                 # e.g. {"incoming": "hail.mp3"}, files in sounds/
}

ENV_VARS = {
    "ics_feed_url": "LCARS_ICS_URL",
    "latitude": "LCARS_LATITUDE",
    "longitude": "LCARS_LONGITUDE",
    "location_name": "LCARS_LOCATION_NAME",
    "port": "LCARS_PORT",
    "max_events": "LCARS_MAX_EVENTS",
    "lookahead_days": "LCARS_LOOKAHEAD_DAYS",
    "timed_default_minutes": "LCARS_TIMED_DEFAULT_MINUTES",
    "nightly_reload": "LCARS_NIGHTLY_RELOAD",
    "messaging_provider": "LCARS_MESSAGING_PROVIDER",
    "telegram_bot_token": "LCARS_TELEGRAM_TOKEN",
    "telegram_group_chat_id": "LCARS_TELEGRAM_GROUP_ID",
    "contacts": "LCARS_CONTACTS_JSON",
    "crew": "LCARS_CREW",
    "padd_sender_name": "LCARS_PADD_SENDER_NAME",
    "message_presets": "LCARS_MESSAGE_PRESETS_JSON",
    "quick_replies": "LCARS_QUICK_REPLIES_JSON",
    "allow_custom_messages": "LCARS_ALLOW_CUSTOM_MESSAGES",
    "message_retention_hours": "LCARS_MESSAGE_RETENTION_HOURS",
    "max_message_length": "LCARS_MAX_MESSAGE_LENGTH",
    "send_rate_limit_per_hour": "LCARS_SEND_RATE_LIMIT",
    "kiosk_allowed_ips": "LCARS_KIOSK_IPS",
    "sounds_enabled": "LCARS_SOUNDS",
    "sound_volume": "LCARS_SOUND_VOLUME",
    "tap_sounds": "LCARS_TAP_SOUNDS",
    "quiet_hours": "LCARS_QUIET_HOURS",
    "incoming_repeat_minutes": "LCARS_INCOMING_REPEAT_MINUTES",
    "incoming_repeat_max": "LCARS_INCOMING_REPEAT_MAX",
    "sound_files": "LCARS_SOUND_FILES_JSON",
}

# Env vars where an empty string is a real value (e.g. LCARS_QUIET_HOURS="" turns quiet hours off).
EMPTY_ENV_ALLOWED = ("quiet_hours", "telegram_group_chat_id", "kiosk_allowed_ips")

SOUND_EXTENSIONS = (".mp3", ".m4a", ".wav")
SOUND_CUES = ("tap", "incoming", "transmit", "sent", "ack", "fail", "open", "lcars")


def config_path():
    return os.environ.get("LCARS_CONFIG", os.path.join(BASE_DIR, "config.json"))


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_json(value, key):
    """Env vars carry JSON as text; config.json values are already parsed."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value) if value.strip() else None
    except ValueError:
        raise SystemExit("Setting %r is not valid JSON." % key)


def _as_list(value, key):
    """A JSON list, or a comma-separated string (for env vars like LCARS_CREW=SAM,RILEY)."""
    if isinstance(value, str):
        if value.strip().startswith("["):
            value = _as_json(value, key)
        else:
            value = [p for p in value.split(",")]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_messaging(cfg, warn=print):
    """Clean up the messaging and sound settings in place. Never prints secret values."""
    cfg["messaging_provider"] = str(cfg["messaging_provider"] or "none").strip().lower()
    cfg["telegram_bot_token"] = str(cfg["telegram_bot_token"] or "").strip()
    cfg["telegram_group_chat_id"] = str(cfg["telegram_group_chat_id"] or "").strip()
    cfg["padd_sender_name"] = str(cfg["padd_sender_name"] or "").strip().upper() or "HOME PADD"
    for key in ("allow_custom_messages", "sounds_enabled", "tap_sounds", "nightly_reload"):
        cfg[key] = _as_bool(cfg[key])
    for key in ("message_retention_hours", "max_message_length", "send_rate_limit_per_hour",
                "incoming_repeat_minutes", "incoming_repeat_max"):
        cfg[key] = max(0, int(cfg[key]))
    cfg["message_retention_hours"] = cfg["message_retention_hours"] or DEFAULTS["message_retention_hours"]
    cfg["max_message_length"] = cfg["max_message_length"] or DEFAULTS["max_message_length"]
    cfg["sound_volume"] = min(1.0, max(0.0, float(cfg["sound_volume"])))

    crew = []
    for name in _as_list(cfg["crew"], "crew"):
        name = name.upper()
        if name not in crew:
            crew.append(name)
    cfg["crew"] = crew

    contacts = []
    seen = set()
    for c in _as_json(cfg["contacts"], "contacts") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip().upper()
        chat_id = str(c.get("telegram_chat_id", "")).strip()
        if not name or not chat_id:
            warn("Skipping a contact with no name or chat ID.")
        elif name in ("ALL", "ALL HANDS") or name in seen or name in crew:
            warn("Skipping contact %s: the name is reserved, duplicated, or also a crew name." % name)
        else:
            seen.add(name)
            contacts.append({"name": name, "telegram_chat_id": chat_id})
    cfg["contacts"] = contacts

    presets = []
    for p in _as_json(cfg["message_presets"], "message_presets") or []:
        if isinstance(p, str):
            p = {"text": p}
        if not isinstance(p, dict) or not str(p.get("text", "")).strip():
            continue
        text = str(p["text"]).strip()
        to = str(p.get("to") or "").strip().upper()
        if to == "ALL HANDS":
            to = "ALL"
        if to and to != "ALL" and to not in seen:
            warn("Preset %r: default recipient %s is not a contact; it will ask HAIL WHOM?." % (text, to))
            to = ""
        presets.append({"text": text, "to": to or None})
    cfg["message_presets"] = presets

    replies = _as_json(cfg["quick_replies"], "quick_replies")
    replies = _as_list(replies if replies is not None else [], "quick_replies")
    cfg["quick_replies"] = [r.upper()[:30] for r in replies][:6]

    cfg["kiosk_allowed_ips"] = _as_list(cfg["kiosk_allowed_ips"], "kiosk_allowed_ips")

    qh = str(cfg["quiet_hours"] or "").strip()
    if qh and not re.match(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$", qh):
        warn("quiet_hours %r is not HH:MM-HH:MM; using %s." % (qh, DEFAULTS["quiet_hours"]))
        qh = DEFAULTS["quiet_hours"]
    cfg["quiet_hours"] = qh

    files = {}
    for cue, name in (_as_json(cfg["sound_files"], "sound_files") or {}).items():
        name = os.path.basename(str(name))
        if cue in SOUND_CUES and name.lower().endswith(SOUND_EXTENSIONS) and not name.startswith("."):
            files[cue] = name
        else:
            warn("Ignoring sound_files entry for %r (unknown cue or unsupported file type)." % cue)
    cfg["sound_files"] = files

    provider = cfg["messaging_provider"]
    if provider not in ("none", "telegram"):
        warn("messaging_provider %r is not supported yet; messaging is off." % provider)
        cfg["messaging_provider"] = "none"
    elif provider == "telegram":
        missing = [label for label, ok in (("telegram_bot_token", cfg["telegram_bot_token"]),
                                           ("contacts", contacts), ("crew", crew)) if not ok]
        if missing:
            warn("Messaging is off: telegram needs %s. Run: python3 configure.py messaging"
                 % ", ".join(missing))
            cfg["messaging_provider"] = "none"
    return cfg


def load_config():
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    else:
        print("No config file at %s; using defaults and environment variables." % path)
    for key, var in ENV_VARS.items():
        value = os.environ.get(var)
        if value or (value is not None and key in EMPTY_ENV_ALLOWED):
            cfg[key] = value
    # coerce types (env vars arrive as strings)
    for key in ("latitude", "longitude"):
        cfg[key] = float(cfg[key])
    for key in ("port", "max_events", "lookahead_days", "timed_default_minutes"):
        cfg[key] = int(cfg[key])
    # webcal:// is just https:// for our purposes
    if cfg["ics_feed_url"].startswith("webcal://"):
        cfg["ics_feed_url"] = "https://" + cfg["ics_feed_url"][len("webcal://"):]
    normalize_messaging(cfg)
    return cfg


CONFIG = load_config()

ICS_FEED_URL = CONFIG["ics_feed_url"]

WEATHER_URL = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
    "latitude": CONFIG["latitude"],
    "longitude": CONFIG["longitude"],
    "current_weather": "true",
    "hourly": "temperature_2m,precipitation_probability,rain",
    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    "timezone": "auto",
}, safe=",")

MAX_EVENTS = CONFIG["max_events"]
LOOKAHEAD_DAYS = CONFIG["lookahead_days"]
TIMED_DEFAULT_MINUTES = CONFIG["timed_default_minutes"]

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

# Files the static handler must never serve (config holds the private feed URL).
# Optional user sound files: only the ones mapped in sound_files, only from this folder.
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
SOUND_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav"}
MAX_SOUND_BYTES = 2 * 1024 * 1024

STATIC_EXTENSIONS = (".html", ".css", ".js", ".woff", ".woff2", ".ttf",
                     ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp")

# iOS guesses several home-screen icon names (sized, -precomposed); all get the one icon.
# Desktop browsers' /favicon.ico gets it too (browsers accept a PNG there).
ICON_ALIAS = re.compile(r"^/apple-touch-icon(-\d{2,3}x\d{2,3})?(-precomposed)?\.png$")
ICON_PATH = "/apple-touch-icon.png"


# ---------------------------------------------------------------------------
# ICS parsing (standard library only)
# ---------------------------------------------------------------------------

def unfold(text):
    """Join folded lines (continuations start with a space or tab)."""
    out = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def parse_line(line):
    """'DTSTART;TZID=America/Los_Angeles:20261031T140000' -> (name, params, value)"""
    if ":" not in line:
        return None, {}, ""
    head, value = line.split(":", 1)
    # a ':' can appear inside a quoted param value, e.g. TZID="..."; re-split if needed
    if head.count('"') % 2 == 1:
        rest_head, value = value.split(":", 1)
        head = head + ":" + rest_head
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def unescape(text):
    return (text.replace("\\n", " ").replace("\\N", " ")
                .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip())


def parse_dt(value, params):
    """Return (naive wall-clock datetime, all_day, source tz). tz None = server-local."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        return datetime.strptime(value[:8], "%Y%m%d"), True, None
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    tz = None
    if value.endswith("Z"):
        tz = timezone.utc
    elif params.get("TZID") and ZoneInfo is not None:
        try:
            tz = ZoneInfo(params["TZID"])
        except Exception:
            tz = None  # unknown TZID: treat as local wall-clock time
    return dt, False, tz


def to_local(dt, tz):
    """Convert a wall-clock time in tz to naive server-local time (DST-aware per date)."""
    if tz is None:
        return dt
    return dt.replace(tzinfo=tz).astimezone().replace(tzinfo=None)


def parse_local(value, params):
    dt, all_day, tz = parse_dt(value, params)
    return to_local(dt, tz), all_day


def parse_duration(value):
    """Minimal ISO-8601 duration parser: P1D, PT1H30M, P1W, -PT15M."""
    sign = -1 if value.startswith("-") else 1
    value = value.lstrip("+-")
    if not value.startswith("P"):
        return None
    days = secs = 0
    num = ""
    in_time = False
    for ch in value[1:]:
        if ch == "T":
            in_time = True
        elif ch.isdigit():
            num += ch
        else:
            n = int(num or 0)
            num = ""
            if ch == "W":
                days += 7 * n
            elif ch == "D":
                days += n
            elif ch == "H" and in_time:
                secs += 3600 * n
            elif ch == "M" and in_time:
                secs += 60 * n
            elif ch == "S" and in_time:
                secs += n
    return sign * timedelta(days=days, seconds=secs)


def read_events(ics_text):
    """Return a list of VEVENT dicts: {name: [(params, value), ...]} (top-level props only)."""
    events = []
    current = None
    depth = 0  # nesting inside the VEVENT (e.g. VALARM)
    for line in unfold(ics_text):
        name, params, value = parse_line(line)
        if name is None:
            continue
        if name == "BEGIN":
            if value.upper() == "VEVENT" and current is None:
                current = {}
                depth = 0
            elif current is not None:
                depth += 1
            continue
        if name == "END":
            if current is not None:
                if value.upper() == "VEVENT" and depth == 0:
                    events.append(current)
                    current = None
                else:
                    depth -= 1
            continue
        if current is not None and depth == 0:
            current.setdefault(name, []).append((params, value))
    return events


def first(ev, name):
    v = ev.get(name)
    return v[0] if v else (None, None)


# ---------------------------------------------------------------------------
# RRULE expansion (DAILY / WEEKLY / MONTHLY / YEARLY with the common parts:
# INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH)
# ---------------------------------------------------------------------------

def parse_rrule(value):
    rule = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.upper()] = v
    return rule


def nth_weekday(year, month, weekday, n):
    """n-th (1..5 or -1..-5) weekday (0=Mon) of a month, or None."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        d += timedelta(weeks=n - 1)
    else:
        nm = date(year + (month == 12), month % 12 + 1, 1)
        d = nm - timedelta(days=1)
        d -= timedelta(days=(d.weekday() - weekday) % 7)
        d -= timedelta(weeks=-n - 1)
    return d if d.month == month else None


def month_candidates(year, month, rule, start):
    """Dates within one month that match BYDAY / BYMONTHDAY (or the start's day)."""
    out = []
    if "BYMONTHDAY" in rule:
        for md in rule["BYMONTHDAY"].split(","):
            md = int(md)
            nm = date(year + (month == 12), month % 12 + 1, 1)
            last = (nm - timedelta(days=1)).day
            day = md if md > 0 else last + md + 1
            if 1 <= day <= last:
                out.append(date(year, month, day))
    elif "BYDAY" in rule:
        for token in rule["BYDAY"].split(","):
            token = token.strip()
            wd = WEEKDAYS.index(token[-2:])
            prefix = token[:-2]
            if prefix:
                d = nth_weekday(year, month, wd, int(prefix))
                if d:
                    out.append(d)
            else:  # every such weekday in the month
                d = nth_weekday(year, month, wd, 1)
                while d and d.month == month:
                    out.append(d)
                    d += timedelta(weeks=1)
    else:
        try:
            out.append(date(year, month, start.day))
        except ValueError:
            pass  # e.g. the 31st in a 30-day month: skipped, per RFC 5545
    return sorted(out)


def expand(start, tz, rule, window_end, exdates):
    """Yield local occurrence starts in order. start is wall-clock time in tz."""
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        until, _ = parse_local(rule["UNTIL"], {})
        if len(rule["UNTIL"]) == 8:
            until = until + timedelta(days=1) - timedelta(seconds=1)
    clock = start.time()
    produced = 0
    guard = 0

    def emit(d):
        return to_local(datetime.combine(d, clock), tz)

    def periods():
        if freq == "DAILY":
            d = start.date()
            while True:
                yield [d]
                d += timedelta(days=interval)
        elif freq == "WEEKLY":
            days = rule.get("BYDAY")
            wds = sorted(WEEKDAYS.index(t.strip()[-2:]) for t in days.split(",")) if days else [start.weekday()]
            week = start.date() - timedelta(days=start.weekday())
            while True:
                yield [week + timedelta(days=w) for w in wds]
                week += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            y, m = start.year, start.month
            while True:
                yield month_candidates(y, m, rule, start)
                m += interval
                while m > 12:
                    m -= 12
                    y += 1
        elif freq == "YEARLY":
            months = [int(x) for x in rule["BYMONTH"].split(",")] if "BYMONTH" in rule else [start.month]
            y = start.year
            while True:
                ds = []
                for m in months:
                    if "BYDAY" in rule or "BYMONTHDAY" in rule:
                        ds += month_candidates(y, m, rule, start)
                    else:
                        try:
                            ds.append(date(y, m, start.day))
                        except ValueError:
                            pass
                yield sorted(ds)
                y += interval

    local_start = to_local(start, tz)
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        yield local_start
        return

    for batch in periods():
        guard += 1
        if guard > 5000:
            return
        for d in batch:
            occ = emit(d)
            if occ < local_start:
                continue
            if until and occ > until:
                return
            if occ > window_end:
                return
            produced += 1
            if count is not None and produced > count:
                return
            if occ not in exdates:
                yield occ


def collect_upcoming(ics_text, everything=False):
    """Upcoming events for the kiosk (the first MAX_EVENTS, or all of them for the setup wizard)."""
    now = datetime.now()
    today = datetime.combine(now.date(), time.min)
    window_end = today + timedelta(days=LOOKAHEAD_DAYS)

    raw = read_events(ics_text)

    # Moved/edited single instances of a recurring series (RECURRENCE-ID)
    overridden = set()
    for ev in raw:
        p, v = first(ev, "RECURRENCE-ID")
        if v:
            uid = first(ev, "UID")[1]
            rid, _ = parse_local(v, p)
            overridden.add((uid, rid))

    out = []
    for ev in raw:
        sp, sv = first(ev, "DTSTART")
        if not sv:
            continue
        if (first(ev, "STATUS")[1] or "").upper() == "CANCELLED":
            continue
        try:
            wall_start, all_day, tz = parse_dt(sv, sp)
        except ValueError:
            continue
        start = to_local(wall_start, tz)

        # length of the event
        ep, evv = first(ev, "DTEND")
        dp, dv = first(ev, "DURATION")
        length = None
        try:
            if evv:
                end, _ = parse_local(evv, ep)
                length = end - start
            elif dv:
                length = parse_duration(dv)
        except ValueError:
            pass
        if length is None or length.total_seconds() <= 0:
            length = timedelta(days=1) if all_day else timedelta(minutes=TIMED_DEFAULT_MINUTES)

        summary = unescape(first(ev, "SUMMARY")[1] or "Scheduled Event")
        uid = first(ev, "UID")[1]

        rp, rv = first(ev, "RRULE")
        if rv:
            exdates = set()
            for xp, xv in ev.get("EXDATE", []):
                for item in xv.split(","):
                    try:
                        exdates.add(parse_local(item, xp)[0])
                    except ValueError:
                        pass
            starts = expand(wall_start, tz, parse_rrule(rv), window_end, exdates)
        else:
            starts = [start]

        for occ in starts:
            if rv and (uid, occ) in overridden:
                continue
            if occ + length <= now:     # already finished
                continue
            out.append((occ, all_day, summary))

    out.sort(key=lambda e: (e[0], not e[1]))
    events = []
    for occ, all_day, summary in (out if everything else out[:MAX_EVENTS]):
        # an all-day/multi-day event that began earlier is shown on today
        shown = max(occ, today) if all_day else occ
        events.append({
            "summary": summary,
            "time": "All Day" if all_day else shown.strftime("%H:%M"),
            "date": shown.strftime("%m/%d"),
            "raw_sort": shown.strftime("%Y%m%d"),
        })
    return events


# ---------------------------------------------------------------------------
# Text messages: transports
#
# A transport moves text between the PADD and family phones. poll() returns
# normalized events; send() delivers one message to one chat and returns an id.
#   message event:  {"type": "message", "sender": id, "chat": id, "text": str, "ts": epoch,
#                    "first_name": str}
#   callback event: {"type": "callback", "sender": id, "chat": id, "data": str, "callback_id": str}
# Sender/chat ids are strings. Telegram is the only real transport so far.
# ---------------------------------------------------------------------------

class TransportError(Exception):
    def __init__(self, message, code=None):
        Exception.__init__(self, message)
        self.code = code


class Transport(object):
    name = "none"
    cursor = None  # provider position (Telegram update offset); persisted by the store

    def poll(self, timeout=25):
        raise NotImplementedError

    def send(self, recipient, text, buttons=None):
        """buttons: list of (label, callback_data). Returns a provider message id."""
        raise NotImplementedError

    def answer_callback(self, callback_id, text=""):
        pass


class TelegramTransport(Transport):
    name = "telegram"
    API = "https://api.telegram.org/bot%s/%s"

    def __init__(self, token):
        self.token = token
        self.cursor = None

    def redact(self, text):
        return str(text).replace(self.token, "<token>") if self.token else str(text)

    def _call(self, method, params, timeout=15):
        req = urllib.request.Request(
            self.API % (self.token, method),
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "LCARS-Family-PADD"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            desc = ""
            try:
                desc = json.loads(e.read().decode("utf-8")).get("description", "")
            except Exception:
                pass
            raise TransportError(self.redact("telegram %s: HTTP %d %s" % (method, e.code, desc)), e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TransportError(self.redact("telegram %s: %s" % (method, e)))
        if not payload.get("ok"):
            raise TransportError(self.redact("telegram %s: %s" % (method, payload.get("description"))))
        return payload["result"]

    def get_me(self):
        return self._call("getMe", {})

    @staticmethod
    def normalize(update):
        """Turn one Telegram update into a transport event (or None to ignore it)."""
        msg = update.get("message")
        if msg and isinstance(msg.get("text"), str) and msg.get("from"):
            return {"type": "message",
                    "sender": str(msg["from"]["id"]),
                    "chat": str(msg["chat"]["id"]),
                    "chat_type": msg["chat"].get("type", ""),
                    "chat_title": msg["chat"].get("title", ""),
                    "first_name": msg["from"].get("first_name", ""),
                    "text": msg["text"],
                    "ts": msg.get("date") or int(_time.time())}
        cq = update.get("callback_query")
        if cq and cq.get("from"):
            chat = (cq.get("message") or {}).get("chat") or {}
            return {"type": "callback",
                    "sender": str(cq["from"]["id"]),
                    "chat": str(chat.get("id", "")),
                    "data": cq.get("data") or "",
                    "callback_id": cq.get("id", "")}
        return None

    def poll(self, timeout=25):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if self.cursor is not None:
            params["offset"] = self.cursor
        updates = self._call("getUpdates", params, timeout=timeout + 10)
        events = []
        for update in updates:
            self.cursor = max(self.cursor or 0, int(update["update_id"]) + 1)
            event = self.normalize(update)
            if event:
                events.append(event)
        return events

    def send(self, recipient, text, buttons=None):
        params = {"chat_id": recipient, "text": text, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in buttons]]}
        return self._call("sendMessage", params)["message_id"]

    def answer_callback(self, callback_id, text=""):
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TransportError:
            pass  # cosmetic only (stops the spinner on the contact's button)


class FakeTransport(Transport):
    """In-memory transport for tests: queue events in .inbox, inspect .sent."""
    name = "fake"

    def __init__(self):
        self.inbox = []
        self.sent = []       # (recipient, text, buttons)
        self.answers = []    # (callback_id, text)
        self.fail_to = set() # recipients that raise on send ("*" = all)
        self.fail_poll = False
        self.cursor = None
        self.polls = 0

    def poll(self, timeout=0):
        self.polls += 1
        if self.fail_poll:
            raise TransportError("fake poll failure")
        events, self.inbox = self.inbox, []
        return events

    def send(self, recipient, text, buttons=None):
        if recipient in self.fail_to or "*" in self.fail_to:
            raise TransportError("fake send failure")
        self.sent.append((recipient, text, list(buttons or [])))
        return len(self.sent)

    def answer_callback(self, callback_id, text=""):
        self.answers.append((callback_id, text))


# ---------------------------------------------------------------------------
# Text messages: store and service
# ---------------------------------------------------------------------------

INCOMING_MAX_CHARS = 1000      # longer incoming texts are cut (the kiosk shrinks text to fit)
MAX_POST_BYTES = 4096
MESSAGE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


class MessageStore(object):
    """Thread-safe list of messages, persisted to a git-ignored JSON file and pruned by age."""

    def __init__(self, path, retention_hours, clock=_time.time):
        self.path = path
        self.retention = retention_hours * 3600
        self.clock = clock
        self.lock = threading.RLock()
        self.messages = []
        self.cursor = None
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.messages = [m for m in data.get("messages", [])
                             if isinstance(m, dict) and MESSAGE_ID_RE.match(str(m.get("id", "")))]
            self.cursor = data.get("cursor")
        except (OSError, ValueError, AttributeError):
            print("Message store unreadable; starting with an empty log.")
            self.messages = []
        self.prune()

    def save(self):
        if not self.path:
            return
        with self.lock:
            data = json.dumps({"version": 1, "cursor": self.cursor, "messages": self.messages})
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)

    def prune(self):
        """Drop messages older than the retention window. Returns True if any were dropped."""
        cutoff = self.clock() - self.retention
        with self.lock:
            kept = [m for m in self.messages if m.get("ts", 0) >= cutoff]
            changed = len(kept) != len(self.messages)
            self.messages = kept
        return changed

    def add(self, msg):
        with self.lock:
            self.messages.append(msg)
            self.save()
        return msg

    def get(self, msg_id):
        with self.lock:
            for m in self.messages:
                if m["id"] == msg_id:
                    return m
        return None


def public_message(m):
    """What the kiosk sees. Never includes chat IDs or provider details."""
    return {k: m.get(k) for k in ("id", "dir", "kind", "ts", "from", "to", "for", "text",
                                  "status", "ack_by", "reply_to", "partial")}


class MessagingService(object):
    def __init__(self, cfg, transport, store_path, clock=_time.time):
        self.cfg = cfg
        self.transport = transport
        self.clock = clock
        self.crew = list(cfg["crew"])
        self.contacts = dict((c["name"], c["telegram_chat_id"]) for c in cfg["contacts"])
        self.contact_by_id = dict((v, k) for k, v in self.contacts.items())
        self.group_id = cfg.get("telegram_group_chat_id") or ""
        self.presets = cfg["message_presets"]
        self.quick_replies = cfg["quick_replies"]
        self.allowed_ips = set(cfg["kiosk_allowed_ips"])
        self.store = MessageStore(store_path, cfg["message_retention_hours"], clock)
        if self.store.cursor is not None:
            self.transport.cursor = self.store.cursor
        self.channel_ok = True
        self.dropped = 0
        self._sends = deque()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ----- background poller -------------------------------------------------

    def start(self, poll_timeout=25):
        self._thread = threading.Thread(target=self._run, args=(poll_timeout,), name="lcars-poller")
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, poll_timeout):
        delay = 5
        while not self._stop.is_set():
            if self.poll_once(poll_timeout):
                delay = 5
            else:
                self._stop.wait(delay)
                delay = min(delay * 2, 60)

    def poll_once(self, timeout=0):
        """One poll round. Returns False if the transport failed."""
        try:
            events = self.transport.poll(timeout=timeout)
        except TransportError as e:
            if self.channel_ok or e.code == 409:
                hint = " (another program, e.g. configure.py, is reading this bot's updates)" \
                    if e.code == 409 else ""
                print("Messaging: channel offline: %s%s" % (e, hint))
            self.channel_ok = False
            return False
        except Exception as e:  # never let the poller thread die
            print("Messaging: poll error: %s" % type(e).__name__)
            self.channel_ok = False
            return False
        if not self.channel_ok:
            print("Messaging: channel back online.")
        self.channel_ok = True
        for event in events:
            try:
                self.handle_event(event)
            except Exception as e:
                print("Messaging: could not handle an update: %s" % type(e).__name__)
        with self.store.lock:
            changed = self.store.prune()
            if self.transport.cursor != self.store.cursor:
                self.store.cursor = self.transport.cursor
                changed = True
            if changed:
                self.store.save()
        return True

    # ----- incoming ---------------------------------------------------------

    def contact_for(self, event):
        """Contact name for an event, or None if it isn't from a family contact in an allowed chat."""
        name = self.contact_by_id.get(event.get("sender"))
        if not name:
            return None
        chat = event.get("chat")
        if chat == event.get("sender") or (self.group_id and chat == self.group_id):
            return name
        return None

    def parse_addressee(self, text):
        """'@SAM pizza at 6' or 'SAM: pizza at 6' -> ('SAM', 'pizza at 6'). Else (None, text)."""
        for name in sorted(self.crew, key=len, reverse=True):
            if text[:len(name) + 1].upper() == "@" + name:
                rest = text[len(name) + 1:]
                if rest[:1] in (" ", ":", ",", "\n", "\t"):
                    body = rest.lstrip(" :,\n\t").strip()
                    if body:
                        return name, body
            elif text[:len(name)].upper() == name:
                rest = text[len(name):].lstrip(" \t")
                if rest.startswith(":") and rest[1:].strip():
                    return name, rest[1:].strip()
        return None, text

    def _new_id(self):
        return uuid.uuid4().hex[:12]

    def handle_event(self, event):
        name = self.contact_for(event)
        if not name:
            self.dropped += 1  # strangers are dropped silently, no reply
            return None
        if event["type"] == "message":
            text = (event.get("text") or "").strip()
            if not text or text.startswith("/"):
                return None  # bot commands (/start, /padd) are for the setup wizard
            for_crew, body = self.parse_addressee(text)
            return self.store.add({
                "id": self._new_id(), "dir": "in", "kind": "msg", "ts": int(self.clock()),
                "from": name, "to": None, "for": for_crew, "text": body[:INCOMING_MAX_CHARS],
                "status": "new", "ack_by": None, "reply_to": None, "partial": False})
        if event["type"] == "callback":
            return self._handle_quick_reply(name, event)
        return None

    def _handle_quick_reply(self, name, event):
        match = re.match(r"^q:([0-9a-f]{12}):(\d)$", event.get("data", ""))
        orig = self.store.get(match.group(1)) if match else None
        if not orig or orig["dir"] != "out":
            self.transport.answer_callback(event.get("callback_id"), "TRANSMISSION EXPIRED")
            return None
        idx = int(match.group(2))
        label = self.quick_replies[idx] if idx < len(self.quick_replies) else "ACKNOWLEDGED"
        with self.store.lock:
            if not orig.get("ack_by"):
                orig["ack_by"] = name
            line = self.store.add({
                "id": self._new_id(), "dir": "in", "kind": "ack", "ts": int(self.clock()),
                "from": name, "to": None, "for": orig.get("from"), "text": label,
                "status": "read", "ack_by": None, "reply_to": orig["id"], "partial": False})
        self.transport.answer_callback(event.get("callback_id"),
                                       "%s RECEIVED BY %s" % (label, self.cfg["padd_sender_name"]))
        return line

    # ----- outgoing ---------------------------------------------------------

    def _rate_ok(self):
        limit = self.cfg["send_rate_limit_per_hour"]
        now = self.clock()
        with self._send_lock:
            while self._sends and self._sends[0] <= now - 3600:
                self._sends.popleft()
            if limit and len(self._sends) >= limit:
                return False
            self._sends.append(now)
            return True

    def _resolve_crew(self, value, fallback=None):
        name = str(value or "").strip().upper() or (fallback or "")
        if not name and len(self.crew) == 1:
            name = self.crew[0]
        return name if name in self.crew else None

    def send_from_kiosk(self, body):
        """Handle POST /api/messages. Returns (http_status, payload)."""
        if "retry" in body:
            orig = self.store.get(str(body["retry"]))
            if not orig or orig["dir"] != "out" or orig["status"] != "failed":
                return 404, {"error": "no failed transmission with that id"}
            if not self._rate_ok():
                return 429, {"error": "rate limit"}
            return self._transmit(orig)

        if "ack" in body:
            orig = self.store.get(str(body["ack"]))
            if not orig or orig["dir"] != "in" or orig["kind"] != "msg":
                return 404, {"error": "no incoming transmission with that id"}
            sender = self._resolve_crew(body.get("from_crew"), orig.get("for"))
            if not sender:
                return 400, {"error": "from_crew must be a crew member"}
            if orig["from"] not in self.contacts:
                return 400, {"error": "that contact is no longer configured"}
            to, text, reply_to = orig["from"], "GOT IT", orig["id"]
            ack_of = orig
        else:
            ack_of = None
            sender = self._resolve_crew(body.get("from_crew"))
            if not sender:
                return 400, {"error": "from_crew must be a crew member"}
            preset_id = body.get("preset_id")
            reply_to = None
            if preset_id is not None:
                try:
                    preset = self.presets[int(preset_id)]
                except (ValueError, TypeError, IndexError):
                    return 400, {"error": "unknown preset"}
                if "text" in body:
                    return 400, {"error": "send preset_id or text, not both"}
                text = preset["text"]
                default_to = preset.get("to")
            elif "text" in body:
                if not self.cfg["allow_custom_messages"]:
                    return 403, {"error": "custom messages are turned off; use a preset"}
                text = CONTROL_CHARS_RE.sub(" ", str(body["text"])).strip()
                if not text:
                    return 400, {"error": "empty message"}
                if len(text) > self.cfg["max_message_length"]:
                    return 400, {"error": "message too long"}
                default_to = None
            else:
                return 400, {"error": "preset_id or text is required"}
            to = str(body.get("to") or default_to or "").strip().upper()
            if to == "ALL HANDS":
                to = "ALL"
            if to != "ALL" and to not in self.contacts:
                return 400, {"error": "to must be ALL or a contact name"}

        if not self._rate_ok():
            return 429, {"error": "rate limit"}
        if ack_of is not None:
            with self.store.lock:
                ack_of["status"] = "read"  # saved by store.add below
        msg = self.store.add({
            "id": self._new_id(), "dir": "out", "kind": "msg", "ts": int(self.clock()),
            "from": sender, "to": to, "for": None, "text": text, "status": "transmitting",
            "ack_by": None, "reply_to": reply_to, "partial": False})
        return self._transmit(msg)

    def _recipients(self, to):
        if to == "ALL":
            return [self.group_id] if self.group_id else list(self.contacts.values())
        return [self.contacts[to]] if to in self.contacts else []

    def _transmit(self, msg):
        with self.store.lock:
            msg["status"] = "transmitting"
            msg["partial"] = False
            self.store.save()
        stamp = datetime.fromtimestamp(msg["ts"]).strftime("%H:%M")
        text = "%s · %s \U0001F596\n%s · %s" % (
            msg["from"], self.cfg["padd_sender_name"], msg["text"], stamp)
        buttons = [(label, "q:%s:%d" % (msg["id"], i)) for i, label in enumerate(self.quick_replies)]
        recipients = self._recipients(msg["to"])
        ok = 0
        for chat_id in recipients:
            try:
                self.transport.send(chat_id, text, buttons)
                ok += 1
            except TransportError as e:
                print("Messaging: send failed: %s" % e)
        with self.store.lock:
            msg["status"] = "sent" if ok else "failed"
            msg["partial"] = 0 < ok < len(recipients)
            self.store.save()
        if ok:
            return 200, {"message": public_message(msg)}
        return 502, {"error": "transmission failed", "message": public_message(msg)}

    # ----- kiosk views ------------------------------------------------------

    def mark_read(self, msg_id):
        msg = self.store.get(msg_id)
        if not msg:
            return 404, {"error": "not found"}
        with self.store.lock:
            if msg["dir"] == "in" and msg["status"] != "read":
                msg["status"] = "read"
                self.store.save()
        return 200, {"message": public_message(msg)}

    def snapshot(self):
        with self.store.lock:
            if self.store.prune():
                self.store.save()
            msgs = sorted(self.store.messages, key=lambda m: m["ts"], reverse=True)
            unread = [m for m in msgs if m["dir"] == "in" and m["status"] == "new"]
            unread_for = {}
            for m in unread:
                if m.get("for"):
                    unread_for[m["for"]] = unread_for.get(m["for"], 0) + 1
            return {"enabled": True,
                    "channel": "online" if self.channel_ok else "offline",
                    "unread": len(unread),
                    "unread_for": unread_for,
                    "messages": [public_message(m) for m in msgs]}


def public_config(cfg, messaging_enabled):
    """Non-secret values for /api/config. Never add tokens, chat IDs or the feed URL here."""
    out = {
        "location_name": cfg["location_name"],
        "nightly_reload": cfg["nightly_reload"],
        "messaging_enabled": bool(messaging_enabled),
        "sounds_enabled": cfg["sounds_enabled"],
        "sound_volume": cfg["sound_volume"],
        "tap_sounds": cfg["tap_sounds"],
        "quiet_hours": cfg["quiet_hours"],
        "incoming_repeat_minutes": cfg["incoming_repeat_minutes"],
        "incoming_repeat_max": cfg["incoming_repeat_max"],
        "sound_files": dict(cfg["sound_files"]),
    }
    if messaging_enabled:
        out.update({
            "crew": list(cfg["crew"]),
            "contacts": [c["name"] for c in cfg["contacts"]],
            "message_presets": [{"id": str(i), "text": p["text"], "to": p.get("to")}
                                for i, p in enumerate(cfg["message_presets"])],
            "padd_sender_name": cfg["padd_sender_name"],
            "allow_custom_messages": cfg["allow_custom_messages"],
            "max_message_length": cfg["max_message_length"],
        })
    return out


def build_messaging(cfg):
    """Create the messaging service if a provider is configured, else None."""
    if cfg["messaging_provider"] != "telegram":
        return None
    store_path = os.path.join(os.path.dirname(os.path.abspath(config_path())), "messages.json")
    return MessagingService(cfg, TelegramTransport(cfg["telegram_bot_token"]), store_path)


MESSAGING = None  # set in __main__ (or by tests)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class LCARSHandler(SimpleHTTPRequestHandler):
    def send_json(self, status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if getattr(self, "_revalidate", False):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        self._revalidate = False
        path = self.path.split("?", 1)[0]
        if ICON_ALIAS.match(path) or path == "/favicon.ico":
            self.path = path = ICON_PATH

        if path == "/api/events":
            if not ICS_FEED_URL:
                self.send_json(200, [{"summary": "Calendar feed not configured", "time": "ERR", "date": ""}])
                return
            try:
                req = urllib.request.Request(ICS_FEED_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as response:
                    ics_data = response.read().decode("utf-8", errors="ignore")
                events = collect_upcoming(ics_data)
                if not events:
                    events = [{"summary": "No upcoming events scheduled", "time": "--:--", "date": ""}]
            except Exception as e:
                print("Calendar error:", e)
                events = [{"summary": "Calendar Feed Sync Error", "time": "ERR", "date": ""}]
            self.send_json(200, events)

        elif path == "/api/weather":
            try:
                req = urllib.request.Request(WEATHER_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as response:
                    weather_data = response.read()
                self.send_json(200, weather_data)
            except Exception as e:
                print("Weather error:", e)
                self.send_json(500, {"status": "error", "message": str(e)})

        elif path == "/api/config":
            # Only non-secret display settings. Never add the feed URL, tokens or chat IDs here.
            self.send_json(200, public_config(MESSAGING.cfg if MESSAGING else CONFIG, MESSAGING is not None))

        elif path == "/api/messages":
            if MESSAGING is None:
                self.send_json(200, {"enabled": False, "messages": [], "unread": 0, "unread_for": {}})
            elif not self.client_allowed():
                self.send_json(403, {"error": "forbidden"})
            else:
                self.send_json(200, MESSAGING.snapshot())

        elif path.startswith("/sounds/"):
            self.send_sound(urllib.parse.unquote(path[len("/sounds/"):]))

        elif path == "/" or path.lower().endswith(STATIC_EXTENSIONS):
            name = os.path.basename(urllib.parse.unquote(path))
            if name.startswith("."):
                self.send_error(404)
                return
            # Make the iPad re-check the page on every load, so it never flashes a stale copy.
            self._revalidate = True
            super().do_GET()

        else:
            # Blocks config.json, messages.json, server.py, .git/, etc.
            self.send_error(404)

    # ----- text message endpoints -------------------------------------------

    def client_allowed(self):
        if MESSAGING is None or not MESSAGING.allowed_ips:
            return True
        ip = self.client_address[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        return ip in MESSAGING.allowed_ips or ip in ("127.0.0.1", "::1")

    def read_body(self):
        """Read the request body (bounded). Always called first, so every reply arrives cleanly."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_POST_BYTES:
            self.close_connection = True
            if 0 < length <= 16 * MAX_POST_BYTES:
                self.rfile.read(length)  # discard, so the client still gets a clean 400
            return None
        return self.rfile.read(length)

    def parse_kiosk_json(self, raw):
        """Body of a kiosk POST as a dict, or (None, (status, error)).

        The custom header + JSON content type can't be sent cross-site without a CORS
        preflight, and this server never answers preflights, so other web pages the
        family visits can't post to these endpoints."""
        if raw is None:
            return None, (400, "bad body length")
        if self.headers.get("X-LCARS-Kiosk") != "1":
            return None, (403, "missing kiosk header")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None, (415, "JSON body required")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, (400, "invalid JSON")
        if not isinstance(body, dict):
            return None, (400, "JSON object required")
        return body, None

    def do_POST(self):
        raw = self.read_body()
        path = self.path.split("?", 1)[0]
        read_match = re.match(r"^/api/messages/([0-9a-f]{12})/read$", path)
        if path != "/api/messages" and not read_match:
            self.send_error(404)
            return
        if MESSAGING is None:
            self.send_json(503, {"error": "messaging is not configured"})
            return
        if not self.client_allowed():
            self.send_json(403, {"error": "forbidden"})
            return
        body, err = self.parse_kiosk_json(raw)
        if err:
            self.send_json(err[0], {"error": err[1]})
            return
        if read_match:
            status, payload = MESSAGING.mark_read(read_match.group(1))
        else:
            status, payload = MESSAGING.send_from_kiosk(body)
        self.send_json(status, payload)

    def send_sound(self, name):
        """Serve one sound file named in sound_files. Anything else is a 404."""
        cfg = MESSAGING.cfg if MESSAGING else CONFIG
        ext = os.path.splitext(name)[1].lower()
        if (name not in cfg["sound_files"].values() or ext not in SOUND_TYPES
                or name != os.path.basename(name) or "\\" in name or name.startswith(".")):
            self.send_error(404)
            return
        full = os.path.join(SOUNDS_DIR, name)
        if os.path.dirname(os.path.realpath(full)) != os.path.realpath(SOUNDS_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        if os.path.getsize(full) > MAX_SOUND_BYTES:
            print("Sound file %s is over %d KB; not served." % (name, MAX_SOUND_BYTES // 1024))
            self.send_error(404)
            return
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", SOUND_TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        # Never answer CORS preflights: no Access-Control-* headers, ever.
        self.send_error(405)


def make_server(port, directory=BASE_DIR):
    handler = functools.partial(LCARSHandler, directory=directory)
    httpd = ThreadingHTTPServer(("", port), handler)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    MESSAGING = build_messaging(CONFIG)
    for cue, name in sorted(CONFIG["sound_files"].items()):
        if not os.path.isfile(os.path.join(SOUNDS_DIR, name)):
            print("Sound file for %r not found: sounds/%s (the built-in tone is used)." % (cue, name))
    if MESSAGING is not None:
        print("Messaging: telegram, %d contact(s), %d crew. Listening for transmissions..."
              % (len(MESSAGING.contacts), len(MESSAGING.crew)))
        MESSAGING.start()
    else:
        print("Messaging: off.")
    httpd = make_server(CONFIG["port"])
    print("LCARS Server running on port %d..." % CONFIG["port"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
