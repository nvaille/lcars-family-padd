"""Tests for the audit fixes: unreadable calendar events, the calendar URL in logs, and the
Host-header (DNS rebinding) check. Standard library only; no real network calls.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta

# Import the server with a config path that doesn't exist, so tests never read the
# real config.json (which holds private settings), and without LCARS_* overrides.
_TMP = tempfile.mkdtemp(prefix="lcars-tests-")
for _var in list(os.environ):
    if _var.startswith("LCARS_"):
        del os.environ[_var]
os.environ.setdefault("LCARS_CONFIG", os.path.join(_TMP, "no-such-config.json"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def ics(*events):
    return "BEGIN:VCALENDAR\n" + "".join(events) + "END:VCALENDAR\n"


def vevent(uid, start, extra=""):
    return "BEGIN:VEVENT\nUID:%s\nDTSTART:%s\nSUMMARY:%s\n%sEND:VEVENT\n" % (uid, start, uid, extra)


def summaries(events):
    return [e["summary"] for e in events]


class UnreadableEventTests(unittest.TestCase):
    GOOD = vevent("good", "20991231T100000Z")

    def collect(self, text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            events = server.collect_upcoming(text, everything=True)
        return events, out.getvalue()

    def test_bad_rrules_skip_only_that_event(self):
        for rrule in ("FREQ=WEEKLY;BYDAY=XX", "FREQ=DAILY;COUNT=abc",
                      "FREQ=MONTHLY;BYMONTHDAY=", "FREQ=DAILY;INTERVAL=x"):
            events, log = self.collect(ics(self.GOOD, vevent("bad", "20991231T090000Z", "RRULE:%s\n" % rrule)))
            self.assertEqual(summaries(events), ["good"], rrule)
            self.assertIn("skipped 1 event", log, rrule)

    def test_bad_event_in_the_middle_keeps_the_events_around_it(self):
        text = ics(vevent("a", "20990101T100000Z"),
                   vevent("bad", "20990102T100000Z", "RRULE:FREQ=WEEKLY;BYDAY=XX\n"),
                   vevent("c", "20990103T100000Z"))
        events, _ = self.collect(text)
        self.assertEqual(summaries(events), ["a", "c"])

    def test_bad_recurrence_id_does_not_break_the_feed(self):
        text = ics(self.GOOD, vevent("moved", "20991231T090000Z", "RECURRENCE-ID:not-a-date\n"))
        events, _ = self.collect(text)
        self.assertIn("good", summaries(events))

    def test_log_line_never_includes_event_text(self):
        _, log = self.collect(ics(vevent("PRIVATE DOCTOR VISIT", "20991231T090000Z", "RRULE:FREQ=DAILY;COUNT=abc\n")))
        self.assertIn("skipped 1 event", log)
        self.assertNotIn("DOCTOR", log)

    def test_good_calendar_logs_nothing(self):
        events, log = self.collect(ics(self.GOOD))
        self.assertEqual((summaries(events), log), (["good"], ""))

    def test_normal_recurring_events_still_expand(self):
        tomorrow = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
        text = ics(vevent("weekly", tomorrow.strftime("%Y%m%dT%H%M%S"), "RRULE:FREQ=WEEKLY;COUNT=3\n"))
        events, log = self.collect(text)
        self.assertEqual((summaries(events), log), (["weekly"] * 3, ""))


class FeedUrlTests(unittest.TestCase):
    SECRET_URL = "calendar.example.com/private/SECRETTOKEN/basic.ics"   # no scheme: urllib raises ValueError

    def test_normalize_calendar(self):
        warnings = []
        cfg = server.normalize_calendar({"ics_feed_url": "webcal://cal.example.com/x.ics"}, warn=warnings.append)
        self.assertEqual((cfg["ics_feed_url"], warnings), ("https://cal.example.com/x.ics", []))
        cfg = server.normalize_calendar({"ics_feed_url": ""}, warn=warnings.append)
        self.assertEqual((cfg["ics_feed_url"], warnings), ("", []))

    def test_missing_scheme_warns_without_printing_the_url(self):
        warnings = []
        server.normalize_calendar({"ics_feed_url": self.SECRET_URL}, warn=warnings.append)
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("SECRETTOKEN", warnings[0])

    def test_calendar_error_text_hides_the_url(self):
        self.addCleanup(setattr, server, "ICS_FEED_URL", server.ICS_FEED_URL)
        server.ICS_FEED_URL = "https://cal.example.com/private/SECRETTOKEN/basic.ics"
        e = urllib.error.URLError("failed for " + server.ICS_FEED_URL)
        text = server.calendar_error_text(e)
        self.assertNotIn("SECRETTOKEN", text)
        self.assertIn("<feed>", text)
        self.assertEqual(server.calendar_error_text(ValueError("unknown url type: '%s'" % self.SECRET_URL)),
                         "ValueError")
        http_error = urllib.error.HTTPError(server.ICS_FEED_URL, 403, "Forbidden", None, io.BytesIO())
        self.addCleanup(http_error.close)
        self.assertEqual(server.calendar_error_text(http_error), "HTTP Error 403: Forbidden")


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server.LCARSHandler, "log_message", server.LCARSHandler.log_message)
        server.LCARSHandler.log_message = lambda *args: None  # keep test output quiet
        server.MESSAGING = None
        self.httpd = server.make_server(0)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever)
        thread.daemon = True
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, host="127.0.0.1", body=None, headers=None):
        """host=None sends no Host header at all."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body or b"")))
        conn.endheaders(body)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw


class EventsLogTests(ServerCase):
    def test_bad_feed_url_is_not_logged(self):
        self.addCleanup(setattr, server, "ICS_FEED_URL", server.ICS_FEED_URL)
        server.ICS_FEED_URL = FeedUrlTests.SECRET_URL
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, raw = self.request("GET", "/api/events")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw.decode())[0]["time"], "ERR")   # the kiosk still gets a clean error
        self.assertIn("Calendar error", out.getvalue())
        self.assertNotIn("SECRETTOKEN", out.getvalue())


class HostCheckUnitTests(unittest.TestCase):
    def test_host_only(self):
        for value, expected in (("Padd.LAN:8000", "padd.lan"), ("192.168.1.36:8000", "192.168.1.36"),
                                ("[::1]:8000", "::1"), ("[fe80::1]", "fe80::1"), ("evil.example.com.", "evil.example.com"),
                                ("padd", "padd"), ("", ""), (None, "")):
            self.assertEqual(server.host_only(value), expected, value)

    def test_normal_kiosk_addresses_are_allowed(self):
        for header in ("192.168.1.36:8000", "10.0.0.5", "127.0.0.1:8000", "[::1]:8000", "localhost:8000",
                       "padd", "padd:8000", "padd.local:8000", "PADD.LOCAL"):
            self.assertTrue(server.host_allowed(header), header)

    def test_missing_host_header_is_allowed(self):
        self.assertTrue(server.host_allowed(None))
        self.assertTrue(server.host_allowed(""))

    def test_public_style_names_are_refused(self):
        for header in ("evil.example.com", "evil.example.com:8000", "EVIL.example.com.", "padd.home",
                       "192.168.1.36.evil.example.com", "localhost.evil.com", ":8000"):
            self.assertFalse(server.host_allowed(header), header)

    def test_allowed_hosts_setting_opens_a_name(self):
        self.assertTrue(server.host_allowed("padd.home:8000", ["padd.home"]))
        self.assertFalse(server.host_allowed("other.home", ["padd.home"]))

    def test_normalize_hosts(self):
        cfg = server.normalize_hosts({"allowed_hosts": "Padd.Home:8000, other.lan., ,"})
        self.assertEqual(cfg["allowed_hosts"], ["padd.home", "other.lan"])
        self.assertEqual(server.normalize_hosts({"allowed_hosts": ["A.B"]})["allowed_hosts"], ["a.b"])
        self.assertEqual(server.normalize_hosts({"allowed_hosts": []})["allowed_hosts"], [])

    def test_setting_is_wired_up(self):
        self.assertEqual(server.DEFAULTS["allowed_hosts"], [])
        self.assertEqual(server.ENV_VARS["allowed_hosts"], "LCARS_ALLOWED_HOSTS")


class HostCheckHTTPTests(ServerCase):
    def test_rebinding_name_is_refused_everywhere(self):
        for path in ("/", "/index.html", "/api/config", "/api/messages", "/api/events", "/api/weather", "/api/tides"):
            self.assertEqual(self.request("GET", path, host="evil.example.com:8000")[0], 421, path)
        status, _ = self.request("POST", "/api/messages", host="evil.example.com", body=b"{}",
                                 headers={"X-LCARS-Kiosk": "1", "Content-Type": "application/json"})
        self.assertEqual(status, 421)

    def test_refusal_reveals_nothing(self):
        _, raw = self.request("GET", "/api/config", host="evil.example.com")
        self.assertNotIn(b"location_name", raw)

    def test_normal_hosts_work(self):
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port, "padd.local", "padd", None):
            status, raw = self.request("GET", "/api/config", host=host)
            self.assertEqual(status, 200, host)
            self.assertIn(b"location_name", raw)
        self.assertEqual(self.request("GET", "/", host="192.168.1.36:8000")[0], 200)

    def test_post_with_a_normal_host_still_reaches_the_endpoint(self):
        status, _ = self.request("POST", "/api/messages", host="192.168.1.36:8000", body=b"{}",
                                 headers={"X-LCARS-Kiosk": "1", "Content-Type": "application/json"})
        self.assertEqual(status, 503)   # messaging is off here; not 421

    def test_allowed_hosts_setting_lets_a_dotted_name_in(self):
        self.addCleanup(server.CONFIG.__setitem__, "allowed_hosts", server.CONFIG["allowed_hosts"])
        self.assertEqual(self.request("GET", "/api/config", host="padd.home:8000")[0], 421)
        server.CONFIG["allowed_hosts"] = ["padd.home"]
        self.assertEqual(self.request("GET", "/api/config", host="padd.home:8000")[0], 200)
        self.assertEqual(self.request("GET", "/api/config", host="evil.example.com")[0], 421)


if __name__ == "__main__":
    unittest.main()
