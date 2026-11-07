"""Tests for the tides feature (CETACEAN OPS). Standard library only; no real network calls.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import copy
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse

_TMP = tempfile.mkdtemp(prefix="lcars-tides-")
for _var in list(os.environ):
    if _var.startswith("LCARS_"):
        del os.environ[_var]
os.environ["LCARS_CONFIG"] = os.path.join(_TMP, "no-such-config.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

NOW = 1794009600.0  # 2026-11-07 00:00 UTC
HILO = json.dumps({"predictions": [
    {"t": "2026-11-07 12:12", "v": "2.215", "type": "L"},
    {"t": "2026-11-07 18:28", "v": "8.913", "type": "H"},
    {"t": "2026-11-08 01:07", "v": "-0.357", "type": "L"},
    {"t": "2026-11-08 07:36", "v": "7.166", "type": "H"},
]})
STATION = json.dumps({"stations": [{"id": "9414290", "name": "San Francisco", "state": "CA"}]})


class Clock(object):
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeNOAA(object):
    """fetch(url) stand-in: answers from canned text, or raises when told to."""

    def __init__(self, tides=HILO, station=STATION):
        self.tides = tides
        self.station = station
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        reply = self.station if "/mdapi/" in url else self.tides
        if isinstance(reply, Exception):
            raise reply
        return reply


def service(fake=None, clock=None, units="feet"):
    return server.TideService("9414290", units, fetch=fake or FakeNOAA(), clock=clock or Clock(),
                              log=lambda *_: None)


class ParseTests(unittest.TestCase):
    def test_parse_hilo(self):
        tides = server.parse_tides(HILO)
        self.assertEqual(len(tides), 4)
        self.assertEqual(tides[0], {"t": 1794053520, "v": 2.21, "type": "L"})  # 12:12 UTC
        self.assertEqual(tides[2]["v"], -0.36)
        self.assertEqual([t["type"] for t in tides], ["L", "H", "L", "H"])

    def test_noaa_error_and_junk(self):
        with self.assertRaises(server.TideError) as cm:
            server.parse_tides('{"error": {"message": " The station is not a valid station."}}')
        self.assertIn("not a valid station", str(cm.exception))
        for bad in ("<html>", "[]", '{"predictions": []}',
                    '{"predictions": [{"t": "bad", "v": "1", "type": "H"}, {"t": "2026-11-07 01:00"}]}'):
            with self.assertRaises(server.TideError):
                server.parse_tides(bad)

    def test_station_name(self):
        self.assertEqual(server.parse_station_name(STATION), "SAN FRANCISCO, CA")
        self.assertEqual(server.parse_station_name('{"errorMsg": "No station"}'), "")
        self.assertEqual(server.parse_station_name("nope"), "")

    def test_url(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(server.tide_url("9414290", "meters", NOW)).query)
        self.assertEqual(q["station"], ["9414290"])
        self.assertEqual(q["interval"], ["hilo"])
        self.assertEqual(q["time_zone"], ["gmt"])
        self.assertEqual(q["units"], ["metric"])
        self.assertEqual(q["begin_date"], ["20261106"])  # yesterday (UTC), so the past hours are covered
        self.assertEqual(q["datum"], ["MLLW"])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(
            server.tide_url("9414290", "feet", NOW)).query)["units"], ["english"])


class ConfigTests(unittest.TestCase):
    def norm(self, **values):
        cfg = copy.deepcopy(server.DEFAULTS)
        cfg.update(values)
        warnings = []
        return server.normalize_tides(cfg, warn=warnings.append), warnings

    def test_defaults_off(self):
        cfg, warnings = self.norm()
        self.assertEqual((cfg["tide_station_id"], cfg["tide_units"], warnings), ("", "feet", []))
        self.assertIsNone(server.build_tides(cfg))
        self.assertIs(server.public_config(cfg, False)["tides_enabled"], False)

    def test_station_and_units(self):
        cfg, warnings = self.norm(tide_station_id=" 9414290 ", tide_units="Metric")
        self.assertEqual((cfg["tide_station_id"], cfg["tide_units"], warnings), ("9414290", "meters", []))
        self.assertIsInstance(server.build_tides(cfg), server.TideService)
        pub = server.public_config(cfg, False)
        self.assertIs(pub["tides_enabled"], True)
        self.assertNotIn("9414290", json.dumps(pub))  # the kiosk gets the station from /api/tides

    def test_bad_values_warn(self):
        cfg, warnings = self.norm(tide_station_id="../config.json", tide_units="fathoms")
        self.assertEqual((cfg["tide_station_id"], cfg["tide_units"]), ("", "feet"))
        self.assertEqual(len(warnings), 2)

    def test_every_setting_has_an_env_var_and_example(self):
        for key in ("tide_station_id", "tide_units"):
            self.assertIn(key, server.ENV_VARS)
        with open(os.path.join(os.path.dirname(server.__file__), "config.example.json")) as fh:
            example = json.load(fh)
        self.assertIn("tide_station_id", example)
        self.assertIn("tide_units", example)


class ServiceTests(unittest.TestCase):
    def test_snapshot_and_cache(self):
        fake, clock = FakeNOAA(), Clock()
        svc = service(fake, clock)
        snap = svc.snapshot()
        self.assertEqual((snap["name"], snap["units"], snap["stale"], len(snap["tides"])),
                         ("SAN FRANCISCO, CA", "FT", False, 4))
        self.assertEqual(len(fake.urls), 2)  # predictions + station name
        clock.now += server.TIDE_REFRESH_SECONDS - 1
        svc.snapshot()
        self.assertEqual(len(fake.urls), 2)  # still cached
        clock.now += 2
        svc.snapshot()
        self.assertEqual(len(fake.urls), 3)  # refreshed; the name is already known

    def test_outage_keeps_last_copy_and_retries_later(self):
        fake, clock = FakeNOAA(), Clock()
        svc = service(fake, clock)
        svc.snapshot()
        fake.tides = OSError("network is unreachable")
        clock.now += server.TIDE_REFRESH_SECONDS
        snap = svc.snapshot()
        self.assertEqual((snap["stale"], len(snap["tides"])), (True, 4))
        calls = len(fake.urls)
        clock.now += 60
        svc.snapshot()
        self.assertEqual(len(fake.urls), calls)  # no hammering NOAA every request
        fake.tides = HILO
        clock.now += server.TIDE_RETRY_SECONDS
        self.assertIs(svc.snapshot()["stale"], False)

    def test_no_data_yet(self):
        svc = service(FakeNOAA(tides='{"error": {"message": "bad station"}}'))
        with self.assertRaises(server.TideError):
            svc.snapshot()

    def test_name_failure_is_not_fatal(self):
        fake = FakeNOAA(station=OSError("timeout"))
        snap = service(fake).snapshot()
        self.assertEqual((snap["name"], len(snap["tides"])), ("", 4))

    def test_meters(self):
        self.assertEqual(service(units="meters").snapshot()["units"], "M")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, server.LCARSHandler, "log_message", server.LCARSHandler.log_message)
        server.LCARSHandler.log_message = lambda *args: None
        self.httpd = server.make_server(0)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever)
        thread.daemon = True
        thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.TIDES = None

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
        return resp.status, body

    def test_off(self):
        server.TIDES = None
        self.assertEqual(self.get("/api/tides"), (200, {"enabled": False}))

    def test_on(self):
        server.TIDES = service()
        status, body = self.get("/api/tides")
        self.assertEqual((status, body["enabled"], body["station"], len(body["tides"])), (200, True, "9414290", 4))

    def test_unavailable(self):
        server.TIDES = service(FakeNOAA(tides=OSError("down")))
        status, body = self.get("/api/tides")
        self.assertEqual((status, body["enabled"]), (502, True))


if __name__ == "__main__":
    unittest.main()
