"""
LCARS Family PADD - kiosk server.

Serves index.html and two JSON endpoints used by the dashboard:
  /api/events   upcoming events from an .ics calendar feed
  /api/weather  Open-Meteo forecast for the configured coordinates
  /api/config   non-secret display settings (e.g. location label)

Settings come from config.json (next to this file, or the path in LCARS_CONFIG),
with environment variables taking precedence. See config.example.json.
Standard library only; Python 3.9+ recommended (3.7+ works, without TZID support).
"""

import functools
import json
import os
import urllib.parse
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
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
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.environ.get("LCARS_CONFIG", os.path.join(BASE_DIR, "config.json"))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    else:
        print("No config file at %s; using defaults and environment variables." % path)
    for key, var in ENV_VARS.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    # coerce types (env vars arrive as strings)
    for key in ("latitude", "longitude"):
        cfg[key] = float(cfg[key])
    for key in ("port", "max_events", "lookahead_days", "timed_default_minutes"):
        cfg[key] = int(cfg[key])
    # webcal:// is just https:// for our purposes
    if cfg["ics_feed_url"].startswith("webcal://"):
        cfg["ics_feed_url"] = "https://" + cfg["ics_feed_url"][len("webcal://"):]
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
STATIC_EXTENSIONS = (".html", ".css", ".js", ".woff", ".woff2", ".ttf",
                     ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp")


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


def collect_upcoming(ics_text):
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
    for occ, all_day, summary in out[:MAX_EVENTS]:
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

    def do_GET(self):
        path = self.path.split("?", 1)[0]

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
            # Only non-secret display settings. Never add the feed URL here.
            self.send_json(200, {"location_name": CONFIG["location_name"]})

        elif path == "/" or path.lower().endswith(STATIC_EXTENSIONS):
            name = os.path.basename(urllib.parse.unquote(path))
            if name.startswith("."):
                self.send_error(404)
                return
            super().do_GET()

        else:
            # Blocks config.json, server.py, .git/, etc.
            self.send_error(404)


if __name__ == "__main__":
    handler = functools.partial(LCARSHandler, directory=BASE_DIR)
    httpd = HTTPServer(("", CONFIG["port"]), handler)
    print("LCARS Server running on port %d..." % CONFIG["port"])
    httpd.serve_forever()
