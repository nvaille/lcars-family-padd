"""Tests for configure.py (the setup wizard). No real network calls: a fake Telegram is used."""

import json
import os
import stat
import sys
import tempfile
import unittest

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
        code = configure.run_messaging(self.path, ask=self.ask, secret=Script(secrets),
                                       out=self.output.append, make_transport=make,
                                       check_server=lambda port: running)
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
        answers = ["", "", "", "", "", "", "n"]  # keep token, contacts, no more, keep group, crew, save, no test
        self.assertEqual(self.run_wizard(answers, [], [], existing=existing), 0, "\n".join(self.output))
        self.assertEqual(self.saved(), existing)

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
        code = configure.run_messaging(self.path, ask=ask, secret=Script([GOOD_TOKEN]),
                                       out=self.output.append, make_transport=make,
                                       check_server=lambda port: False)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.path))


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
