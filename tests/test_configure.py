"""Tests for configure.py (the setup wizard). No real network calls: a fake Telegram is used."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error

_TMP = tempfile.mkdtemp(prefix="lcars-wiz-")
for _var in list(os.environ):
    if _var.startswith("LCARS_"):
        del os.environ[_var]
os.environ["LCARS_CONFIG"] = os.path.join(_TMP, "no-such-config.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import configure  # noqa: E402
import server  # noqa: E402

# Shaped like a bot token but with a short bot id, so it is obviously fake.
GOOD_TOKEN = "12345:" + "A" * 35


class FakeBot(server.FakeTransport):
    """Scripted Telegram: each poll() returns the next batch (a list, or a function of the bot)."""

    def __init__(self, token, batches, valid=True):
        server.FakeTransport.__init__(self)
        self.token = token
        self.batches = batches
        self.valid = valid

    def get_me(self):
        if not self.valid or self.token != GOOD_TOKEN:
            raise server.TransportError("HTTP 401 Unauthorized", 401)
        return {"username": "home_padd_bot"}

    def poll(self, timeout=0):
        self.polls += 1
        self.cursor = (self.cursor or 0) + 1
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch(self) if callable(batch) else batch


def private(sender, name, text="/start"):
    return {"type": "message", "sender": sender, "chat": sender, "chat_type": "private",
            "first_name": name, "text": text, "ts": 0}


def tap_test_button(bot):
    data = [b for b in bot.sent if b[2]][-1][2][0][1]  # first button of the last test message
    return [{"type": "callback", "sender": "222", "chat": "222", "data": data, "callback_id": "cb"}]


NO_STATIONS = json.dumps({"stations": []})
STATIONS = json.dumps({"stations": [
    {"id": "9414290", "name": "San Francisco", "state": "CA", "lat": 37.8063, "lng": -122.4659},
    {"id": "9414750", "name": "Alameda", "state": "CA", "lat": 37.7717, "lng": -122.3},
    {"id": "9999999", "name": "Far Away", "state": "WA", "lat": 47.6, "lng": -122.3},
    {"id": "../x", "name": "Bad ID", "state": "CA", "lat": 37.80, "lng": -122.45},
    {"id": "9414000", "name": "No Coordinates", "state": "CA"},
]})


class Script(object):
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("unexpected prompt: %r" % prompt)
        return self.answers.pop(0)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcars-wiz-run-")
        self.path = os.path.join(self.dir, "config.json")
        self.output = []
        self.bots = []

    def run_wizard(self, answers, secrets, batches, existing=None, running=False):
        if existing is not None:
            with open(self.path, "w") as fh:
                json.dump(existing, fh)

        def make(token):
            bot = FakeBot(token, batches)
            self.bots.append(bot)
            return bot
        self.ask = Script(answers)
        code = configure.run_section(self.path, "messaging", ask=self.ask, secret=Script(secrets),
                                     out=self.output.append, make_transport=make,
                                     check_server=lambda port: running)
        self.assertEqual(self.ask.answers, [], "unused answers; output:\n" + "\n".join(self.output))
        return code

    def saved(self):
        with open(self.path) as fh:
            return json.load(fh)

    def test_full_setup(self):
        batches = [
            [private("111", "Alex"), private("999", "Stranger")],
            [private("222", "Jo"), private("111", "Alex", "hello again")],
            [{"type": "message", "sender": "111", "chat": "-100500", "chat_type": "supergroup",
              "chat_title": "Family", "text": "/padd@home_padd_bot", "ts": 0}],
            tap_test_button,
        ]
        answers = ["mom", "",          # name Alex, keep waiting
                   "",                 # skip the stranger
                   "Dad", "n",         # name Jo, stop waiting
                   "y",                # use a family group
                   "sam", "Riley", "", # crew
                   "",                 # save
                   ""]                 # send test
        code = self.run_wizard(answers, [GOOD_TOKEN], batches, existing={"location_name": "Home", "port": 8000})
        self.assertEqual(code, 0, "\n".join(self.output))
        cfg = self.saved()
        self.assertEqual(cfg["location_name"], "Home")  # other settings are kept
        self.assertEqual(cfg["messaging_provider"], "telegram")
        self.assertEqual(cfg["telegram_bot_token"], GOOD_TOKEN)
        self.assertEqual(cfg["contacts"], [{"name": "MOM", "telegram_chat_id": "111"},
                                           {"name": "DAD", "telegram_chat_id": "222"}])
        self.assertEqual(cfg["telegram_group_chat_id"], "-100500")
        self.assertEqual(cfg["crew"], ["SAM", "RILEY"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path + ".bak").st_mode), 0o600)
        with open(self.path + ".bak") as fh:
            self.assertEqual(json.load(fh), {"location_name": "Home", "port": 8000})

        bot = self.bots[-1]
        recipients = [r for r, _, _ in bot.sent]
        self.assertNotIn("999", recipients)  # strangers get nothing
        self.assertIn("ACKNOWLEDGED BY DAD. Two-way link confirmed.", " ".join(self.output))
        tests = [s for s in bot.sent if s[1] == configure.TEST_TEXT]
        self.assertEqual(sorted(r for r, _, _ in tests), ["111", "222"])
        self.assertEqual([b[0] for b in tests[0][2]], ["GOT IT", "CALL ME", "ON MY WAY"])
        self.assertEqual(bot.answers, [("cb", "TWO-WAY LINK CONFIRMED")])

        # The saved file is something the server accepts.
        server_cfg = dict(server.DEFAULTS)
        server_cfg.update(cfg)
        self.assertEqual(server.normalize_messaging(server_cfg, warn=lambda *_: None)["messaging_provider"],
                         "telegram")

        text = "\n".join(self.output)
        for secret in (GOOD_TOKEN, "111", "222", "-100500"):
            self.assertNotIn(secret, text)

    def test_rerun_keeps_everything(self):
        existing = {"messaging_provider": "telegram", "telegram_bot_token": GOOD_TOKEN,
                    "contacts": [{"name": "MOM", "telegram_chat_id": "111"}], "crew": ["SAM"],
                    "telegram_group_chat_id": "-100500"}
        answers = ["", "", "", "", "n"]  # keep token, contacts menu done, keep group, crew menu done, no test
        self.assertEqual(self.run_wizard(answers, [], [], existing=existing), 0, "\n".join(self.output))
        self.assertEqual(self.saved(), existing)
        self.assertIn("No changes", "\n".join(self.output))

    def test_token_retries(self):
        secrets = ["not a token", "54321:" + "B" * 35, GOOD_TOKEN]
        batches = [[private("111", "Alex")]]
        answers = ["mom", "n", "", "sam", "", "", "n"]
        self.assertEqual(self.run_wizard(answers, secrets, batches), 0, "\n".join(self.output))
        joined = "\n".join(self.output)
        self.assertIn("doesn't look like a bot token", joined)
        self.assertIn("Telegram rejected that token", joined)

    def test_reserved_and_duplicate_names(self):
        batches = [[private("111", "Alex"), private("222", "Jo")]]
        answers = ["all hands", "mom", "", "MOM", "dad", "n", "", "mom", "sam", "", "", "n"]
        self.assertEqual(self.run_wizard(answers, [GOOD_TOKEN], batches), 0, "\n".join(self.output))
        self.assertEqual([c["name"] for c in self.saved()["contacts"]], ["MOM", "DAD"])
        self.assertEqual(self.saved()["crew"], ["SAM"])  # MOM rejected as a crew name

    def test_server_running_warning(self):
        self.assertEqual(self.run_wizard([""], [], [], existing={"port": 8000}, running=True), 1)
        self.assertIn("stop the server first", "\n".join(self.output))
        self.assertEqual(self.saved(), {"port": 8000})
        self.assertFalse(os.path.exists(self.path + ".bak"))

    def test_conflict_aborts_without_saving(self):
        batches = [server.TransportError("HTTP 409 Conflict", 409)]
        self.assertEqual(self.run_wizard([], [GOOD_TOKEN], batches), 1)
        self.assertIn("Another program is reading", "\n".join(self.output))
        self.assertFalse(os.path.exists(self.path))

    def test_ctrl_c_before_save_writes_nothing(self):
        batches = [[private("111", "Alex")]]
        answers = ["mom", "n", ""]  # ...then Ctrl+C at the crew prompt

        def make(token):
            return FakeBot(token, batches)

        script = Script(answers)

        def ask(prompt=""):
            if "Crew name" in prompt:
                raise KeyboardInterrupt
            return script(prompt)
        code = configure.run_section(self.path, "messaging", ask=ask, secret=Script([GOOD_TOKEN]),
                                     out=self.output.append, make_transport=make,
                                     check_server=lambda port: False)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.path))


FEED = "https://calendar.example.com/private/abc123secret/basic.ics"
GEO = json.dumps({"results": [
    {"name": "Springfield", "admin1": "Missouri", "country": "United States", "country_code": "US",
     "latitude": 37.21533, "longitude": -93.29824},
    {"name": "Springfield", "admin1": "Illinois", "country": "United States", "country_code": "US",
     "latitude": 39.80172, "longitude": -89.64371},
]})


def ics(days_ahead=(1, 2, 3)):
    import datetime
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for i, d in enumerate(days_ahead):
        day = (datetime.date.today() + datetime.timedelta(days=d)).strftime("%Y%m%d")
        lines += ["BEGIN:VEVENT", "UID:e%d" % i, "DTSTART:%sT180000" % day, "SUMMARY:Event %d" % i, "END:VEVENT"]
    return "\r\n".join(lines + ["END:VCALENDAR"])


class SectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcars-sec-")
        self.path = os.path.join(self.dir, "config.json")
        self.sounds = os.path.join(self.dir, "sounds")
        os.mkdir(self.sounds)
        self.output = []
        self.fetched = []

    def run_section(self, name, answers, secrets=(), existing=None, responses=None, running=False):
        if existing is not None:
            with open(self.path, "w") as fh:
                json.dump(existing, fh)
        responses = dict(responses or {})
        responses.setdefault(configure.TIDE_STATIONS_URL, NO_STATIONS)  # an inland town by default

        def fetch(url):
            self.fetched.append(url)
            for prefix, reply in responses.items():
                if url.startswith(prefix):
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
            raise AssertionError("unexpected fetch: %s" % url)
        self.ask = Script(answers)
        code = configure.run_section(self.path, name, ask=self.ask, secret=Script(secrets),
                                     out=self.output.append, fetch=fetch, sounds_dir=self.sounds,
                                     check_server=lambda port: running)
        self.assertEqual(self.ask.answers, [], "unused answers; output:\n" + "\n".join(self.output))
        return code

    def saved(self):
        with open(self.path) as fh:
            return json.load(fh)

    def text(self):
        return "\n".join(self.output)

    # ----- location -----

    def test_location_lookup_with_region(self):
        existing = {"port": 8000, "crew": ["SAM"], "custom_key": "kept"}
        answers = ["Springfield, IL", "1", "", ""]  # search, pick, keep suggested label, save
        code = self.run_section("location", answers, existing=existing,
                                responses={configure.GEOCODE_URL: GEO}, running=True)
        self.assertEqual(code, 0, self.text())
        self.assertIn("name=Springfield&", self.fetched[0])
        self.assertNotIn("IL", self.fetched[0])  # the region filters results; it isn't sent
        cfg = self.saved()
        self.assertEqual((cfg["latitude"], cfg["longitude"]), (39.8017, -89.6437))
        self.assertEqual(cfg["location_name"], "Springfield, Illinois")
        self.assertEqual(cfg["custom_key"], "kept")
        self.assertEqual(cfg["crew"], ["SAM"])
        self.assertIn("Restart it", self.text())

    def test_location_search_again_then_custom_label(self):
        responses = {configure.GEOCODE_URL + "name=Nowhere": json.dumps({}),
                     configure.GEOCODE_URL: GEO}
        answers = ["Nowhere", "Springfield", "", "Springfield", "2", "Home", ""]
        self.assertEqual(self.run_section("location", answers, responses=responses), 0, self.text())
        self.assertIn("No places found", self.text())
        self.assertEqual(self.saved()["location_name"], "Home")
        self.assertEqual(self.saved()["latitude"], 39.8017)

    def test_location_offline_manual_entry(self):
        responses = {configure.GEOCODE_URL: OSError("network is unreachable")}
        answers = ["Paris", "m", "48.8566", "999", "2.3522", "Paris", ""]
        self.assertEqual(self.run_section("location", answers, responses=responses), 0, self.text())
        self.assertIn("Couldn't look that up", self.text())
        self.assertIn("Pick a number from", self.text())
        cfg = self.saved()
        self.assertEqual((cfg["latitude"], cfg["longitude"], cfg["location_name"]), (48.8566, 2.3522, "Paris"))

    def test_enter_keeps_everything_and_writes_nothing(self):
        existing = {"latitude": 1.5, "longitude": 2.5, "location_name": "Base"}
        self.assertEqual(self.run_section("location", [""], existing=existing), 0)
        self.assertIn("No changes", self.text())
        self.assertFalse(os.path.exists(self.path + ".bak"))

    # ----- tides -----

    COAST = {"latitude": 37.79, "longitude": -122.44, "location_name": "San Francisco, CA"}

    def test_tides_pick_nearest_station(self):
        answers = ["2", "", ""]  # second-nearest station, keep feet, save
        code = self.run_section("tides", answers, existing=self.COAST,
                                responses={configure.TIDE_STATIONS_URL: STATIONS})
        self.assertEqual(code, 0, self.text())
        self.assertIn("1. SAN FRANCISCO, CA (9414290)  1.8 miles", self.text())
        self.assertIn("2. ALAMEDA, CA (9414750)  7.7 miles", self.text())
        self.assertNotIn("FAR AWAY", self.text())  # over 60 miles
        self.assertNotIn("BAD ID", self.text())
        cfg = self.saved()
        self.assertEqual(cfg["tide_station_id"], "9414750")
        self.assertNotIn("tide_units", cfg)  # feet is the default, so nothing is written

    def test_location_step_offers_tides(self):
        answers = ["", "2", "m", ""]  # keep location, second station, meters, save
        code = self.run_section("location", answers, existing=self.COAST,
                                responses={configure.TIDE_STATIONS_URL: STATIONS})
        self.assertEqual(code, 0, self.text())
        cfg = self.saved()
        self.assertEqual((cfg["tide_station_id"], cfg["tide_units"]), ("9414750", "meters"))

    def test_location_step_inland_asks_nothing(self):
        existing = {"latitude": 39.8, "longitude": -89.6, "location_name": "Springfield"}
        self.assertEqual(self.run_section("location", [""], existing=existing,
                                          responses={configure.TIDE_STATIONS_URL: STATIONS}), 0)
        self.assertIn("No NOAA tide stations within 60 miles", self.text())
        self.assertIn("stays off", self.text())

    def test_tides_manual_id(self):
        missing = urllib.error.HTTPError("url", 404, "Not Found", None, io.BytesIO(b""))
        self.addCleanup(missing.close)
        responses = {configure.TIDE_STATIONS_URL: OSError("network is unreachable"),
                     server.TIDE_STATION_API % "1234567": missing,
                     server.TIDE_STATION_API % "9414290": json.dumps(
                         {"stations": [{"id": "9414290", "name": "San Francisco", "state": "CA"}]})}
        answers = ["m", "../etc", "m", "1234567", "m", "9414290", "", ""]
        code = self.run_section("tides", answers, existing=self.COAST, responses=responses)
        self.assertEqual(code, 0, self.text())
        self.assertIn("Couldn't get NOAA's tide station list", self.text())
        self.assertIn("Station IDs are letters and digits", self.text())
        self.assertIn("NOAA doesn't list a station 1234567", self.text())
        self.assertIn("Station 9414290: SAN FRANCISCO, CA", self.text())
        self.assertEqual(self.saved()["tide_station_id"], "9414290")

    def test_tides_turn_off_and_keep(self):
        existing = dict(self.COAST, tide_station_id="9414750")
        self.assertEqual(self.run_section("tides", ["", ""], existing=existing,
                                          responses={configure.TIDE_STATIONS_URL: STATIONS}), 0)
        self.assertIn("No changes", self.text())
        self.output = []
        self.assertEqual(self.run_section("tides", ["o", ""], existing=existing,
                                          responses={configure.TIDE_STATIONS_URL: STATIONS}), 0)
        self.assertEqual(self.saved()["tide_station_id"], "")

    # ----- calendar -----

    def test_calendar_new_feed_checked_and_hidden(self):
        existing = {"location_name": "Home"}
        code = self.run_section("calendar", [""], secrets=[FEED], existing=existing,
                                responses={FEED: ics()})
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.saved(), {"location_name": "Home", "ics_feed_url": FEED})
        self.assertIn("found 3 upcoming events", self.text())
        self.assertIn("Next: ", self.text())
        self.assertNotIn("abc123secret", self.text())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_calendar_counts_more_than_max_events(self):
        self.run_section("calendar", [""], secrets=[FEED], responses={FEED: ics(range(1, 9))})
        self.assertIn("found 8 upcoming events", self.text())

    def test_calendar_webcal_and_bad_feeds(self):
        webcal = "webcal://calendar.example.com/private/abc123secret/basic.ics"
        responses = {"https://bad.example.com/": "<html>login</html>",
                     "https://down.example.com/": OSError("timed out for https://down.example.com/x.ics"),
                     FEED: ics()}
        secrets = ["ftp://nope", "https://bad.example.com/x.ics", "https://down.example.com/x.ics", webcal]
        # bad html: don't save anyway; download fails: don't save anyway; webcal works; save
        answers = ["n", "n", ""]
        self.assertEqual(self.run_section("calendar", answers, secrets=secrets, responses=responses), 0,
                         self.text())
        self.assertEqual(self.saved()["ics_feed_url"], webcal)
        text = self.text()
        self.assertIn("should start with https://", text)
        self.assertIn("isn't a calendar", text)
        self.assertIn("Couldn't download the feed: timed out for (feed address)", text)
        self.assertNotIn("down.example.com", text)

    def test_calendar_keep_and_check(self):
        existing = {"ics_feed_url": FEED}
        self.assertEqual(self.run_section("calendar", ["", ""], existing=existing,
                                          responses={FEED: ics([1])}), 0)
        self.assertIn("found 1 upcoming event in", self.text())
        self.assertIn("No changes", self.text())

    def test_calendar_remove(self):
        existing = {"ics_feed_url": FEED}
        self.assertEqual(self.run_section("calendar", ["n", "y", ""], secrets=[""], existing=existing), 0)
        self.assertEqual(self.saved()["ics_feed_url"], "")

    # ----- sounds -----

    def test_sounds_defaults_write_nothing(self):
        # sounds on, volume, taps, quiet hours, repeat minutes, repeat max, don't look again
        self.assertEqual(self.run_section("sounds", ["", "", "", "", "", "", ""], existing={}), 0, self.text())
        self.assertIn("No changes", self.text())
        self.assertIn("built-in tone", self.text())
        self.assertIn(os.path.abspath(self.sounds), self.text())   # the folder path is spelled out

    def test_sounds_folder_rescan_finds_new_files(self):
        sounds = self.sounds

        class Copier(object):     # copies a file in when asked to look again, like the owner in Finder
            def __init__(self, answers):
                self.answers = list(answers)

            def __call__(self, prompt):
                if "Look in the sounds folder again" in prompt:
                    open(os.path.join(sounds, "chirp.wav"), "w").close()
                return self.answers.pop(0)

        answers = ["", "", "", "", "", "",
                   "y",          # look again (chirp.wav appears)
                   "",           # choose files: default is now Yes
                   "8", "1",     # lcars -> chirp.wav
                   "",           # done with cues
                   ""]           # save
        code = configure.run_section(self.path, "sounds", ask=Copier(answers), secret=Script([]),
                                     out=self.output.append, sounds_dir=self.sounds)
        self.assertEqual(code, 0, self.text())
        self.assertEqual(self.saved()["sound_files"], {"lcars": "chirp.wav"})
        self.assertIn("the LCARS button, which reloads", self.text())
        server_cfg = dict(server.DEFAULTS, sound_files={"lcars": "chirp.wav"})
        warnings = []
        server.normalize_messaging(server_cfg, warn=warnings.append)
        self.assertEqual((warnings, server_cfg["sound_files"]), ([], {"lcars": "chirp.wav"}))

    def test_sounds_changes_and_file_mapping(self):
        for name in ("hail.mp3", "beep.wav", "notes.txt", ".hidden.mp3"):
            open(os.path.join(self.sounds, name), "w").close()
        answers = ["", "150", "80", "n", "9pm", "21:30 - 6:45", "0",
                   "y",           # choose files
                   "2", "2",      # incoming -> hail.mp3 (files sorted: beep.wav, hail.mp3)
                   "1", "1",      # tap -> beep.wav
                   "1", "0",      # tap -> back to built-in
                   "",            # done with cues
                   ""]            # save
        self.assertEqual(self.run_section("sounds", answers), 0, self.text())
        cfg = self.saved()
        self.assertEqual(cfg, {"sound_volume": 0.8, "tap_sounds": False, "quiet_hours": "21:30-06:45",
                               "incoming_repeat_minutes": 0, "sound_files": {"incoming": "hail.mp3"}})
        self.assertIn("Use 24-hour times", self.text())
        self.assertNotIn("notes.txt", self.text())
        server_cfg = dict(server.DEFAULTS)
        server_cfg.update(cfg)
        warnings = []
        server.normalize_messaging(server_cfg, warn=warnings.append)
        self.assertEqual(warnings, [])

    def test_sounds_maps_belay_abort_acknowledged(self):
        for name in ("back.wav", "close.wav", "ok.wav"):
            open(os.path.join(self.sounds, name), "w").close()
        answers = ["", "", "", "", "", "",
                   "y",           # choose files
                   "9", "1",      # belay -> back.wav
                   "10", "2",     # abort -> close.wav
                   "11", "3",     # acknowledged -> ok.wav
                   "",            # done with cues
                   ""]            # save
        self.assertEqual(self.run_section("sounds", answers, existing={}), 0, self.text())
        mapping = {"belay": "back.wav", "abort": "close.wav", "acknowledged": "ok.wav"}
        self.assertEqual(self.saved()["sound_files"], mapping)
        for text in ("BELAY: back one screen", "ABORT: back to the main screen",
                     "ACKNOWLEDGED, after a message is sent"):
            self.assertIn(text, self.text())
        server_cfg = dict(server.DEFAULTS, sound_files=dict(mapping))
        warnings = []
        server.normalize_messaging(server_cfg, warn=warnings.append)
        self.assertEqual((warnings, server_cfg["sound_files"]), ([], mapping))

    def test_sounds_off_and_quiet_hours_off(self):
        self.run_section("sounds", ["n", ""])
        self.assertEqual(self.saved(), {"sounds_enabled": False})
        self.output = []
        self.run_section("sounds", ["y", "", "", "off", "", "", "", ""])
        self.assertEqual(self.saved(), {"sounds_enabled": True, "quiet_hours": ""})

    # ----- security -----

    def test_security_allowlist_and_rate(self):
        existing = {"kiosk_allowed_ips": ["192.168.1.9"]}
        answers = ["a", "192.168.1.42", "a", "not-an-ip", "a", "192.168.1.42", "x",
                   "r", "1", "r", "", "", "20", ""]
        self.assertEqual(self.run_section("security", answers, existing=existing), 0, self.text())
        self.assertEqual(self.saved(), {"kiosk_allowed_ips": ["192.168.1.42"], "send_rate_limit_per_hour": 20})
        text = self.text()
        self.assertIn("isn't an IP address", text)
        self.assertIn("already on the list", text)
        self.assertIn("letters in [brackets]", text)

    def test_ctrl_c_saves_nothing(self):
        def ask(prompt=""):
            raise KeyboardInterrupt
        code = configure.run_section(self.path, "security", ask=ask, out=self.output.append,
                                     check_server=lambda port: False)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("Nothing saved", self.text())

    def test_decline_save(self):
        self.assertEqual(self.run_section("security", ["", "5", "n"], existing={"port": 8000}), 1)
        self.assertEqual(self.saved(), {"port": 8000})


FAMILY = {
    "messaging_provider": "telegram", "telegram_bot_token": GOOD_TOKEN,
    "contacts": [{"name": "MOM", "telegram_chat_id": "111"}, {"name": "DAD", "telegram_chat_id": "222"}],
    "crew": ["SAM", "RILEY"],
    "message_presets": [{"text": "HOME SAFE", "to": "ALL"}, {"text": "CALL ME", "to": "MOM"},
                        {"text": "PICK ME UP", "to": "DAD"}],
    "location_name": "Home",
}


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcars-edit-")
        self.path = os.path.join(self.dir, "config.json")
        self.output = []
        self.bots = []

    def run_section(self, name, answers, existing=FAMILY, batches=None, running=False):
        with open(self.path, "w") as fh:
            json.dump(existing, fh)

        def make(token):
            if batches is None:
                raise AssertionError("this step should not contact Telegram")
            bot = FakeBot(token, batches)
            self.bots.append(bot)
            return bot
        ask = Script(answers)
        code = configure.run_section(self.path, name, ask=ask, out=self.output.append,
                                     make_transport=make, check_server=lambda port: running)
        self.assertEqual(ask.answers, [], "unused answers; output:\n" + self.text())
        return code

    def saved(self):
        with open(self.path) as fh:
            return json.load(fh)

    def text(self):
        return "\n".join(self.output)

    def test_rename_contact_updates_presets_offline(self):
        answers = ["n", "1", "mum", "", ""]  # rename #1 to MUM, done, save
        self.assertEqual(self.run_section("contacts", answers, running=True), 0, self.text())
        cfg = self.saved()
        self.assertEqual(cfg["contacts"], [{"name": "MUM", "telegram_chat_id": "111"},
                                           {"name": "DAD", "telegram_chat_id": "222"}])
        self.assertEqual([p["to"] for p in cfg["message_presets"]], ["ALL", "MUM", "DAD"])
        self.assertEqual(cfg["crew"], ["SAM", "RILEY"])
        self.assertEqual(cfg["location_name"], "Home")
        self.assertNotIn("111", self.text())

    def test_rename_rejects_crew_and_other_contact_names(self):
        answers = ["n", "2", "sam", "mom", "all", "", ""]  # all rejected, then cancel; done
        self.assertEqual(self.run_section("contacts", answers), 0, self.text())
        self.assertIn("No changes", self.text())
        self.assertEqual(self.text().count("already used"), 2)
        self.assertIn("reserved", self.text())

    def test_remove_contact_clears_their_presets(self):
        answers = ["r", "2", "n",   # change of mind
                   "r", "2", "y", "", ""]
        self.assertEqual(self.run_section("contacts", answers), 0, self.text())
        cfg = self.saved()
        self.assertEqual([c["name"] for c in cfg["contacts"]], ["MOM"])
        self.assertEqual(cfg["message_presets"][2], {"text": "PICK ME UP", "to": None})
        self.assertIn("asks HAIL WHOM? each time", self.text())
        server_cfg = dict(server.DEFAULTS)
        server_cfg.update(cfg)
        warnings = []
        server.normalize_messaging(server_cfg, warn=warnings.append)
        self.assertEqual(warnings, [])

    def test_removing_last_contact_warns(self):
        existing = dict(FAMILY, contacts=[{"name": "MOM", "telegram_chat_id": "111"}])
        self.run_section("contacts", ["r", "1", "y", "", ""], existing=existing)
        self.assertIn("messaging will turn off", self.text())
        self.assertEqual(self.saved()["contacts"], [])

    def test_add_contact_pairs_only_the_new_person(self):
        batches = [[private("111", "Alex", "/start again"), private("333", "Gran")]]
        answers = ["a", "nana", "", ""]
        self.assertEqual(self.run_section("contacts", answers, batches=batches), 0, self.text())
        cfg = self.saved()
        self.assertEqual(cfg["contacts"], FAMILY["contacts"] + [{"name": "NANA", "telegram_chat_id": "333"}])
        self.assertEqual(cfg["message_presets"], FAMILY["message_presets"])
        bot = self.bots[0]
        self.assertEqual([r for r, _, _ in bot.sent], ["333"])  # only the new person is greeted
        self.assertGreaterEqual(bot.polls, 2)  # finish() confirmed the updates it read

    def test_add_contact_warns_if_server_running(self):
        answers = ["a", "", ""]  # don't continue; back at the menu, done (no changes)
        self.assertEqual(self.run_section("contacts", answers, batches=[], running=True), 0, self.text())
        self.assertIn("stop the server first", self.text())
        self.assertIn("Stop the server, then run this again", self.text())

    def test_add_contact_without_token(self):
        existing = {"contacts": [{"name": "MOM", "telegram_chat_id": "111"}]}
        self.run_section("contacts", ["a", ""], existing=existing)
        self.assertIn("Run:  python3 configure.py messaging", self.text())

    def test_add_contact_gives_up_after_quiet_period(self):
        self.run_section("contacts", ["a", ""], batches=[])
        self.assertIn("stopped waiting", self.text())

    def test_crew_editing(self):
        answers = ["a", "mom", "jo",       # MOM is a contact; JO added
                   "n", "2", "rye",        # RILEY -> RYE
                   "m", "3", "1",          # JO to the top
                   "r", "2", "", ""]       # remove SAM, done, save
        self.assertEqual(self.run_section("crew", answers), 0, self.text())
        self.assertEqual(self.saved()["crew"], ["JO", "RYE"])
        self.assertIn("already used", self.text())

    def test_crew_keeps_one_while_messaging_on(self):
        existing = dict(FAMILY, crew=["SAM"])
        self.run_section("crew", ["r", "1", ""], existing=existing)
        self.assertIn("at least one name", self.text())
        self.assertEqual(self.saved()["crew"], ["SAM"])

    def test_crew_can_be_emptied_when_messaging_off(self):
        self.run_section("crew", ["r", "1", "", ""], existing={"crew": ["SAM"]})
        self.assertEqual(self.saved()["crew"], [])

    def test_presets_editing(self):
        long_text = "X" * 41
        answers = ["a", long_text, "need help", "3",          # add, to MOM (1 ask, 2 ALL, 3 MOM)
                   "e", "1", "home safe and sound",           # edit text
                   "t", "3", "1",                             # PICK ME UP -> ask each time
                   "m", "4", "1",                             # NEED HELP to the top
                   "r", "3",                                  # remove CALL ME
                   "",                                        # done with presets
                   "y",                                       # allow typed messages
                   "a", "be there soon",                      # quick reply added
                   "r", "2",                                  # remove CALL ME reply
                   "m", "3", "1",                             # BE THERE SOON first
                   "",                                        # done with replies
                   "kitchen padd",                            # signature
                   ""]                                        # save
        self.assertEqual(self.run_section("presets", answers), 0, self.text())
        cfg = self.saved()
        self.assertEqual(cfg["message_presets"], [{"text": "NEED HELP", "to": "MOM"},
                                                  {"text": "HOME SAFE AND SOUND", "to": "ALL"},
                                                  {"text": "PICK ME UP", "to": None}])
        self.assertEqual(cfg["quick_replies"], ["BE THERE SOON", "GOT IT", "ON MY WAY"])
        self.assertEqual(cfg["padd_sender_name"], "KITCHEN PADD")
        self.assertTrue(cfg["allow_custom_messages"])
        self.assertIn("keep it to 40", self.text())
        self.assertEqual(cfg["contacts"], FAMILY["contacts"])

    def test_presets_untouched_defaults_are_not_written(self):
        existing = {"crew": ["SAM"]}
        self.assertEqual(self.run_section("presets", ["", "", "", ""], existing=existing), 0, self.text())
        self.assertIn("No changes", self.text())
        self.assertNotIn("allow_custom_messages", self.saved())
        self.assertIn("I'M HOME FROM SCHOOL", self.text())

    def test_presets_keep_one_and_reply_limit(self):
        existing = dict(FAMILY, message_presets=[{"text": "HOME SAFE"}],
                        quick_replies=["A", "B", "C", "D", "E", "F"])
        self.run_section("presets", ["r", "1", "", "", "a", "", ""], existing=existing)
        self.assertIn("Keep at least one preset", self.text())
        self.assertIn("room for 6 buttons", self.text())

    def test_unknown_preset_recipient_is_flagged(self):
        existing = dict(FAMILY, message_presets=[{"text": "HI", "to": "GRAN"}])
        self.run_section("presets", ["", "", "", ""], existing=existing)
        self.assertIn("GRAN (not a contact", self.text())


class TourTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcars-tour-")
        self.path = os.path.join(self.dir, "config.json")
        self.sounds = os.path.join(self.dir, "sounds")
        os.mkdir(self.sounds)
        self.output = []
        self.bots = []

    def tour(self, answers, secrets=(), existing=None, batches=None, running=False):
        if existing is not None:
            with open(self.path, "w") as fh:
                json.dump(existing, fh)

        def make(token):
            if batches is None:
                raise AssertionError("the tour should not contact Telegram here")
            bot = FakeBot(token, batches)
            self.bots.append(bot)
            return bot

        def fetch(url):
            if url.startswith(configure.GEOCODE_URL):
                return GEO
            if url == configure.TIDE_STATIONS_URL:
                return NO_STATIONS
            raise AssertionError("unexpected fetch")
        ask = Script(answers)
        code = configure.run_section(self.path, "tour", ask=ask, secret=Script(secrets), out=self.output.append,
                                     fetch=fetch, sounds_dir=self.sounds, make_transport=make,
                                     check_server=lambda port: running)
        self.assertEqual(ask.answers, [], "unused answers; output:\n" + self.text())
        return code

    def saved(self):
        with open(self.path) as fh:
            return json.load(fh)

    def text(self):
        return "\n".join(self.output)

    def test_first_run_without_messaging(self):
        answers = ["Springfield", "2", "",   # location: pick Illinois, keep label
                   "",                        # no text messaging
                   "", "50", "", "", "", "", "",  # sounds: volume 50
                   ""]                        # save
        self.assertEqual(self.tour(answers, secrets=[""]), 0, self.text())
        cfg = self.saved()
        self.assertEqual(cfg, {"latitude": 39.8017, "longitude": -89.6437,
                               "location_name": "Springfield, Illinois", "sound_volume": 0.5})
        self.assertNotIn("Test transmission", self.text())
        self.assertNotIn("Preset messages", self.text())

    def test_rerun_with_messaging_on_changes_nothing(self):
        answers = ["",                    # keep location
                   "",                    # keep bot token
                   "",                    # contacts menu: done
                   "",                    # no family group
                   "",                    # crew menu: done
                   "", "", "", "",        # presets done, typed messages, replies done, signature
                   "", "", "", "", "", "", "",  # sounds
                   "", "",                # allowlist done, rate limit
                   "n"]                   # no test transmission
        self.assertEqual(self.tour(answers, secrets=[""], existing=FAMILY, batches=[]), 0, self.text())
        self.assertIn("No changes", self.text())
        self.assertEqual(self.saved(), FAMILY)
        for section in ("Location", "Calendar", "Text messaging", "Contacts", "Crew", "Preset messages",
                        "Quick replies", "Sounds", "Kiosk security", "Test transmission"):
            self.assertIn("== %s" % section, self.text())

    def test_messaging_problem_keeps_the_rest_of_the_tour(self):
        answers = ["Springfield", "1", "Home",  # location
                   "y",                          # set up messaging...
                   "",                           # ...but the server is running: don't continue
                   "", "", "", "", "", "", "",   # sounds
                   ""]                           # save
        self.assertEqual(self.tour(answers, secrets=[""], running=True), 0, self.text())
        cfg = self.saved()
        self.assertEqual(cfg["location_name"], "Home")
        self.assertNotIn("messaging_provider", cfg)
        self.assertIn("the rest of the setup continues", self.text())

    def test_failed_pairing_undoes_messaging_changes(self):
        answers = ["", "y"]  # keep location, try messaging (nobody pairs)
        answers += ["", "", "", "", "", "", ""]  # sounds; nothing changed, so no save question
        code = self.tour(answers, secrets=["", GOOD_TOKEN], batches=[])
        self.assertEqual(code, 0, self.text())
        self.assertIn("No contacts paired", self.text())
        self.assertNotIn("telegram_bot_token", self.saved() if os.path.exists(self.path) else {})


class TestCommandTests(unittest.TestCase):
    def test_sends_and_confirms(self):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(path, "w") as fh:
            json.dump(FAMILY, fh)
        out, bots = [], []

        def make(token):
            bots.append(FakeBot(token, [tap_test_button]))
            return bots[-1]
        code = configure.run_test(path, ask=Script([""]), out=out.append, make_transport=make,
                                  check_server=lambda port: False)
        self.assertEqual(code, 0, "\n".join(out))
        self.assertIn("ACKNOWLEDGED BY DAD", "\n".join(out))
        self.assertEqual(sorted(r for r, _, _ in bots[0].sent), ["111", "222"])
        with open(path) as fh:
            self.assertEqual(json.load(fh), FAMILY)  # the test never writes the config

    def test_needs_contacts(self):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        out = []
        self.assertEqual(configure.run_test(path, ask=Script([]), out=out.append,
                                            check_server=lambda port: False), 1)
        self.assertIn("No contacts are paired yet", "\n".join(out))


class HelperTests(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(configure.mask(GOOD_TOKEN), "…AAAA")
        self.assertEqual(configure.mask(""), "(not set)")
        self.assertEqual(configure.mask("short"), "••••")

    def test_show_hides_secrets(self):
        raw = {"telegram_bot_token": GOOD_TOKEN, "telegram_group_chat_id": "-100500",
               "contacts": [{"name": "MOM", "telegram_chat_id": "111"}], "crew": ["SAM"],
               "ics_feed_url": "https://example.com/secret-calendar-path.ics", "port": 8000}
        text = "\n".join(configure.summary_lines(raw))
        for secret in (GOOD_TOKEN, "-100500", "111", "secret-calendar-path"):
            self.assertNotIn(secret, text)
        self.assertIn("MOM (paired)", text)
        self.assertIn("8000", text)

    def test_save_creates_private_file(self):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        configure.save_raw(path, {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(path + ".bak"))  # nothing to back up the first time
        self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
