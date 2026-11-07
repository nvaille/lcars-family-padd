"""Tests for the text-messages feature. Standard library only; no real network calls.

Run from the repo root:  python3 -m unittest discover -s tests
"""

import copy
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest

# Import the server with a config path that doesn't exist, so tests never read the
# real config.json (which holds private settings), and without LCARS_* overrides.
_TMP = tempfile.mkdtemp(prefix="lcars-tests-")
for _var in list(os.environ):
    if _var.startswith("LCARS_"):
        del os.environ[_var]
os.environ["LCARS_CONFIG"] = os.path.join(_TMP, "no-such-config.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def make_cfg(**overrides):
    cfg = copy.deepcopy(server.DEFAULTS)
    cfg.update({
        "messaging_provider": "telegram",
        "telegram_bot_token": "TEST-TOKEN",
        "contacts": [{"name": "Mom", "telegram_chat_id": "111"},
                     {"name": "DAD", "telegram_chat_id": "222"}],
        "crew": "sam, riley",
    })
    cfg.update(overrides)
    return server.normalize_messaging(cfg, warn=lambda *_: None)


class Clock(object):
    def __init__(self, now=1800000000.0):
        self.now = now

    def __call__(self):
        return self.now


def msg_event(sender, text, chat=None):
    return {"type": "message", "sender": sender, "chat": chat or sender, "text": text, "ts": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcars-store-")
        self.path = os.path.join(self.dir, "messages.json")
        self.clock = Clock()
        self.fake = server.FakeTransport()

    def service(self, **overrides):
        return server.MessagingService(make_cfg(**overrides), self.fake, self.path, self.clock)


class ConfigTests(unittest.TestCase):
    def test_defaults_keep_messaging_off(self):
        cfg = server.normalize_messaging(copy.deepcopy(server.DEFAULTS), warn=lambda *_: None)
        self.assertEqual(cfg["messaging_provider"], "none")
        self.assertIsNone(server.build_messaging(cfg))
        self.assertEqual(cfg["quiet_hours"], "22:00-07:00")

    def test_every_setting_has_an_env_var(self):
        self.assertEqual(set(server.DEFAULTS), set(server.ENV_VARS))

    def test_normalizes_names_and_lists(self):
        cfg = make_cfg()
        self.assertEqual(cfg["crew"], ["SAM", "RILEY"])
        self.assertEqual([c["name"] for c in cfg["contacts"]], ["MOM", "DAD"])

    def test_rejects_reserved_and_duplicate_contacts(self):
        cfg = make_cfg(contacts=[{"name": "ALL", "telegram_chat_id": "1"},
                                 {"name": "MOM", "telegram_chat_id": "2"},
                                 {"name": "mom", "telegram_chat_id": "3"},
                                 {"name": "SAM", "telegram_chat_id": "4"},
                                 {"name": "NOID"}])
        self.assertEqual(cfg["contacts"], [{"name": "MOM", "telegram_chat_id": "2"}])

    def test_preset_with_unknown_recipient_asks_instead(self):
        cfg = make_cfg(message_presets=[{"text": "HI", "to": "ZED"}, {"text": "YO", "to": "all hands"}, "PLAIN"])
        self.assertEqual(cfg["message_presets"],
                         [{"text": "HI", "to": None}, {"text": "YO", "to": "ALL"}, {"text": "PLAIN", "to": None}])

    def test_telegram_without_contacts_turns_off(self):
        self.assertEqual(make_cfg(contacts=[])["messaging_provider"], "none")
        self.assertEqual(make_cfg(telegram_bot_token="")["messaging_provider"], "none")
        self.assertEqual(make_cfg(messaging_provider="twilio")["messaging_provider"], "none")

    def test_env_strings_are_parsed(self):
        cfg = make_cfg(contacts='[{"name":"MOM","telegram_chat_id":"9"}]', crew='["SAM"]',
                       allow_custom_messages="true", sound_volume="5", kiosk_allowed_ips="10.0.0.5, 10.0.0.6",
                       quick_replies='["ok"]', quiet_hours="bogus", sound_files='{"incoming":"../x.mp3","tap":"a.exe"}')
        self.assertEqual(cfg["contacts"][0]["telegram_chat_id"], "9")
        self.assertEqual(cfg["crew"], ["SAM"])
        self.assertTrue(cfg["allow_custom_messages"])
        self.assertEqual(cfg["sound_volume"], 1.0)
        self.assertEqual(cfg["kiosk_allowed_ips"], ["10.0.0.5", "10.0.0.6"])
        self.assertEqual(cfg["quick_replies"], ["OK"])
        self.assertEqual(cfg["quiet_hours"], "22:00-07:00")
        self.assertEqual(cfg["sound_files"], {"incoming": "x.mp3"})

    def test_empty_quiet_hours_env_turns_it_off(self):
        path = os.path.join(_TMP, "cfg-qh.json")
        with open(path, "w") as fh:
            json.dump({}, fh)
        old = dict(os.environ)
        try:
            os.environ["LCARS_CONFIG"] = path
            os.environ["LCARS_QUIET_HOURS"] = ""
            self.assertEqual(server.load_config()["quiet_hours"], "")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_nightly_reload_defaults_on_and_can_be_turned_off(self):
        self.assertIs(server.public_config(make_cfg(), False)["nightly_reload"], True)
        cfg = server.normalize_messaging(make_cfg(nightly_reload="false"), warn=lambda m: None)
        self.assertIs(cfg["nightly_reload"], False)
        self.assertIs(server.public_config(cfg, True)["nightly_reload"], False)

    def test_nightly_reload_env_var(self):
        path = os.path.join(_TMP, "cfg-nr.json")
        with open(path, "w") as fh:
            json.dump({"nightly_reload": True}, fh)
        old = dict(os.environ)
        try:
            os.environ["LCARS_CONFIG"] = path
            os.environ["LCARS_NIGHTLY_RELOAD"] = "off"
            self.assertIs(server.load_config()["nightly_reload"], False)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_public_config_has_no_secrets(self):
        cfg = make_cfg(telegram_group_chat_id="-100999")
        text = json.dumps(server.public_config(cfg, True))
        for secret in ("TEST-TOKEN", "111", "222", "-100999", "telegram_chat_id", "ics_feed_url"):
            self.assertNotIn(secret, text)
        pub = server.public_config(cfg, True)
        self.assertEqual(pub["contacts"], ["MOM", "DAD"])
        self.assertEqual(pub["message_presets"][0], {"id": "0", "text": "I'M HOME FROM SCHOOL", "to": "ALL"})
        off = server.public_config(cfg, False)
        self.assertFalse(off["messaging_enabled"])
        self.assertNotIn("crew", off)


class IncomingTests(Base):
    def test_strangers_are_dropped_silently(self):
        svc = self.service()
        self.assertIsNone(svc.handle_event(msg_event("999", "hello")))
        self.assertEqual(svc.store.messages, [])
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(svc.dropped, 1)

    def test_contact_message_is_stored_unread(self):
        svc = self.service()
        m = svc.handle_event(msg_event("111", "  Pizza tonight  "))
        self.assertEqual((m["dir"], m["from"], m["text"], m["status"], m["for"]),
                         ("in", "MOM", "Pizza tonight", "new", None))
        snap = svc.snapshot()
        self.assertEqual(snap["unread"], 1)
        self.assertNotIn("111", json.dumps(snap))

    def test_addressed_to_one_kid(self):
        svc = self.service()
        cases = [("@SAM pizza at 6", "SAM", "pizza at 6"), ("riley: home by 6", "RILEY", "home by 6"),
                 ("@sam, hi", "SAM", "hi"), ("Sam : ok", "SAM", "ok"),
                 ("SAMANTHA: no", None, "SAMANTHA: no"), ("@SAM", None, "@SAM"),
                 ("Sam is late", None, "Sam is late")]
        for text, who, body in cases:
            self.assertEqual(svc.parse_addressee(text), (who, body), text)
        svc.handle_event(msg_event("222", "@RILEY dinner"))
        self.assertEqual(svc.snapshot()["unread_for"], {"RILEY": 1})

    def test_commands_ignored(self):
        svc = self.service()
        self.assertIsNone(svc.handle_event(msg_event("111", "/start")))
        self.assertEqual(svc.store.messages, [])

    def test_group_rules(self):
        svc = self.service(telegram_group_chat_id="-100500")
        self.assertIsNotNone(svc.handle_event(msg_event("111", "in group", chat="-100500")))
        self.assertIsNone(svc.handle_event(msg_event("999", "stranger in group", chat="-100500")))
        self.assertIsNone(svc.handle_event(msg_event("111", "other group", chat="-100777")))
        self.assertEqual(len(svc.store.messages), 1)

    def test_long_incoming_text_is_capped(self):
        svc = self.service()
        m = svc.handle_event(msg_event("111", "x" * 5000))
        self.assertEqual(len(m["text"]), server.INCOMING_MAX_CHARS)

    def test_mark_read(self):
        svc = self.service()
        m = svc.handle_event(msg_event("111", "hi"))
        self.assertEqual(svc.mark_read(m["id"])[0], 200)
        self.assertEqual(svc.snapshot()["unread"], 0)
        self.assertEqual(svc.mark_read("0" * 12)[0], 404)


class OutgoingTests(Base):
    def test_preset_to_all_without_group_dms_everyone(self):
        svc = self.service()
        status, payload = svc.send_from_kiosk({"from_crew": "sam", "preset_id": "0"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["message"]["status"], "sent")
        self.assertEqual(payload["message"]["to"], "ALL")
        self.assertEqual(sorted(r for r, _, _ in self.fake.sent), ["111", "222"])
        recipient, text, buttons = self.fake.sent[0]
        self.assertTrue(text.startswith("SAM · HOME PADD"))
        self.assertIn("I'M HOME FROM SCHOOL · ", text)
        mid = payload["message"]["id"]
        self.assertEqual(buttons, [("GOT IT", "q:%s:0" % mid), ("CALL ME", "q:%s:1" % mid),
                                   ("ON MY WAY", "q:%s:2" % mid)])
        for label, data in buttons:
            self.assertLessEqual(len(data.encode()), 64)  # Telegram callback_data limit

    def test_all_hands_uses_group_when_configured(self):
        svc = self.service(telegram_group_chat_id="-100500")
        svc.send_from_kiosk({"from_crew": "SAM", "preset_id": 0})
        self.assertEqual([r for r, _, _ in self.fake.sent], ["-100500"])

    def test_preset_without_default_needs_recipient(self):
        svc = self.service()
        self.assertEqual(svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "3"})[0], 400)
        status, payload = svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "3", "to": "dad"})
        self.assertEqual(status, 200)
        self.assertEqual([r for r, _, _ in self.fake.sent], ["222"])

    def test_validation(self):
        svc = self.service()
        bad = [{"from_crew": "MOM", "preset_id": "0"},           # parents are not crew
               {"preset_id": "0"},                                # two kids: sender required
               {"from_crew": "SAM", "preset_id": "99"},
               {"from_crew": "SAM", "preset_id": "x"},
               {"from_crew": "SAM", "preset_id": "3", "to": "GRANDMA"},
               {"from_crew": "SAM"}]
        for body in bad:
            self.assertEqual(svc.send_from_kiosk(body)[0], 400, body)
        self.assertEqual(self.fake.sent, [])

    def test_single_crew_member_is_default_sender(self):
        svc = self.service(crew=["SAM"])
        self.assertEqual(svc.send_from_kiosk({"preset_id": "0"})[0], 200)

    def test_custom_text_blocked_in_preset_only_mode(self):
        svc = self.service()
        self.assertEqual(svc.send_from_kiosk({"from_crew": "SAM", "to": "MOM", "text": "hi"})[0], 403)
        self.assertEqual(svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "0", "text": "x"})[0], 400)

    def test_custom_text_when_allowed(self):
        svc = self.service(allow_custom_messages=True, max_message_length=10)
        self.assertEqual(svc.send_from_kiosk({"from_crew": "SAM", "to": "MOM", "text": "x" * 11})[0], 400)
        self.assertEqual(svc.send_from_kiosk({"from_crew": "SAM", "to": "MOM", "text": "  \n "})[0], 400)
        status, payload = svc.send_from_kiosk({"from_crew": "SAM", "to": "MOM", "text": "a\nb\x00c"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["message"]["text"], "a b c")

    def test_failure_partial_and_retry(self):
        svc = self.service()
        self.fake.fail_to = {"222"}
        status, payload = svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "0"})
        self.assertEqual((status, payload["message"]["status"], payload["message"]["partial"]), (200, "sent", True))
        self.fake.fail_to = {"*"}
        status, payload = svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "1"})
        self.assertEqual((status, payload["message"]["status"]), (502, "failed"))
        mid = payload["message"]["id"]
        self.fake.fail_to = set()
        status, payload = svc.send_from_kiosk({"retry": mid})
        self.assertEqual((status, payload["message"]["id"], payload["message"]["status"]), (200, mid, "sent"))
        self.assertEqual(svc.send_from_kiosk({"retry": mid})[0], 404)  # only failed ones retry

    def test_rate_limit(self):
        svc = self.service(send_rate_limit_per_hour=2)
        body = {"from_crew": "SAM", "preset_id": "0"}
        self.assertEqual(svc.send_from_kiosk(body)[0], 200)
        self.assertEqual(svc.send_from_kiosk(body)[0], 200)
        self.assertEqual(svc.send_from_kiosk(body)[0], 429)
        self.clock.now += 3601
        self.assertEqual(svc.send_from_kiosk(body)[0], 200)

    def test_acknowledge_received_message(self):
        svc = self.service()
        m = svc.handle_event(msg_event("111", "@RILEY pizza at 6"))
        status, payload = svc.send_from_kiosk({"ack": m["id"]})  # signed by the addressed kid
        self.assertEqual(status, 200)
        self.assertEqual((payload["message"]["from"], payload["message"]["to"], payload["message"]["text"]),
                         ("RILEY", "MOM", "GOT IT"))
        self.assertEqual([r for r, _, _ in self.fake.sent], ["111"])
        self.assertEqual(svc.store.get(m["id"])["status"], "read")
        unaddressed = svc.handle_event(msg_event("222", "hello all"))
        self.assertEqual(svc.send_from_kiosk({"ack": unaddressed["id"]})[0], 400)  # who is transmitting?
        self.assertEqual(svc.send_from_kiosk({"ack": unaddressed["id"], "from_crew": "SAM"})[0], 200)


class QuickReplyTests(Base):
    def test_first_tap_acks_and_every_tap_is_logged(self):
        svc = self.service()
        _, payload = svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "0"})
        mid = payload["message"]["id"]
        svc.handle_event({"type": "callback", "sender": "222", "chat": "222", "data": "q:%s:2" % mid,
                          "callback_id": "cb1"})
        svc.handle_event({"type": "callback", "sender": "111", "chat": "111", "data": "q:%s:0" % mid,
                          "callback_id": "cb2"})
        snap = svc.snapshot()
        out = [m for m in snap["messages"] if m["id"] == mid][0]
        self.assertEqual(out["ack_by"], "DAD")
        acks = [m for m in snap["messages"] if m["kind"] == "ack"]
        self.assertEqual(sorted((m["from"], m["text"], m["for"]) for m in acks),
                         [("DAD", "ON MY WAY", "SAM"), ("MOM", "GOT IT", "SAM")])
        self.assertEqual(snap["unread"], 0)  # acks chime, but don't raise INCOMING
        self.assertEqual([a[0] for a in self.fake.answers], ["cb1", "cb2"])

    def test_stranger_and_expired_taps(self):
        svc = self.service()
        _, payload = svc.send_from_kiosk({"from_crew": "SAM", "preset_id": "0"})
        mid = payload["message"]["id"]
        svc.handle_event({"type": "callback", "sender": "999", "chat": "999", "data": "q:%s:0" % mid,
                          "callback_id": "x"})
        self.assertIsNone(svc.store.get(mid)["ack_by"])
        self.assertEqual(self.fake.answers, [])  # strangers get nothing back
        svc.handle_event({"type": "callback", "sender": "111", "chat": "111", "data": "q:%s:0" % ("0" * 12),
                          "callback_id": "y"})
        svc.handle_event({"type": "callback", "sender": "111", "chat": "111", "data": "garbage",
                          "callback_id": "z"})
        self.assertEqual([a[1] for a in self.fake.answers], ["TRANSMISSION EXPIRED"] * 2)


class StoreAndPollerTests(Base):
    def test_persist_reload_prune_and_permissions(self):
        svc = self.service()
        svc.handle_event(msg_event("111", "old"))
        self.clock.now += 3600 * 23
        svc.handle_event(msg_event("111", "new"))
        self.fake.cursor = 42
        svc.poll_once()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        again = server.MessagingService(make_cfg(), server.FakeTransport(), self.path, self.clock)
        self.assertEqual(len(again.store.messages), 2)
        self.assertEqual(again.transport.cursor, 42)
        self.clock.now += 3600 * 2  # 'old' is now 25h old
        self.assertEqual([m["text"] for m in again.snapshot()["messages"]], ["new"])
        with open(self.path) as fh:
            self.assertEqual([m["text"] for m in json.load(fh)["messages"]], ["new"])

    def test_corrupt_store_starts_empty(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.service().store.messages, [])

    def test_poll_failure_marks_channel_offline(self):
        svc = self.service()
        self.fake.fail_poll = True
        self.assertFalse(svc.poll_once())
        self.assertEqual(svc.snapshot()["channel"], "offline")
        self.fake.fail_poll = False
        self.fake.inbox.append(msg_event("111", "back"))
        self.assertTrue(svc.poll_once())
        snap = svc.snapshot()
        self.assertEqual((snap["channel"], snap["unread"]), ("online", 1))

    def test_background_thread_delivers(self):
        svc = self.service()
        self.fake.inbox.append(msg_event("111", "threaded"))
        svc.start(poll_timeout=0)
        try:
            for _ in range(200):
                if svc.store.messages:
                    break
                threading.Event().wait(0.01)
        finally:
            svc.stop()
        self.assertEqual(svc.store.messages[0]["text"], "threaded")


class TelegramTransportTests(unittest.TestCase):
    class Canned(server.TelegramTransport):
        def __init__(self, responses):
            server.TelegramTransport.__init__(self, "123:SECRET")
            self.responses = responses
            self.calls = []

        def _call(self, method, params, timeout=15):
            self.calls.append((method, params))
            return self.responses.pop(0)

    def test_poll_normalizes_and_advances_offset(self):
        t = self.Canned([[
            {"update_id": 10, "message": {"date": 5, "text": "hi", "from": {"id": 111, "first_name": "M"},
                                          "chat": {"id": 111, "type": "private"}}},
            {"update_id": 11, "message": {"date": 6, "from": {"id": 111}, "chat": {"id": 111},
                                          "photo": []}},
            {"update_id": 12, "callback_query": {"id": "cb", "data": "q:x:0", "from": {"id": 222},
                                                 "message": {"chat": {"id": 222}}}},
        ], []])
        events = t.poll(timeout=1)
        self.assertEqual([e["type"] for e in events], ["message", "callback"])
        self.assertEqual((events[0]["sender"], events[0]["chat"], events[0]["text"]), ("111", "111", "hi"))
        self.assertEqual(t.cursor, 13)
        t.poll(timeout=1)
        self.assertEqual(t.calls[1][1]["offset"], 13)

    def test_send_builds_inline_keyboard(self):
        t = self.Canned([{"message_id": 7}])
        self.assertEqual(t.send("111", "hello", [("GOT IT", "q:a:0")]), 7)
        method, params = t.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(params["reply_markup"],
                         {"inline_keyboard": [[{"text": "GOT IT", "callback_data": "q:a:0"}]]})

    def test_errors_never_contain_the_token(self):
        t = server.TelegramTransport("123:SECRET")
        self.assertEqual(t.redact("https://api.telegram.org/bot123:SECRET/getMe failed"),
                         "https://api.telegram.org/bot<token>/getMe failed")


class HTTPTests(Base):
    def setUp(self):
        Base.setUp(self)
        self.addCleanup(setattr, server.LCARSHandler, "log_message", server.LCARSHandler.log_message)
        server.LCARSHandler.log_message = lambda *args: None  # keep test output quiet
        self.svc = self.service()
        server.MESSAGING = self.svc
        self.httpd = server.make_server(0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.MESSAGING = None

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            payload = json.loads(raw.decode())
        except ValueError:
            payload = None
        return resp.status, payload, resp

    KIOSK = {"X-LCARS-Kiosk": "1", "Content-Type": "application/json"}

    def test_send_and_list(self):
        status, payload, _ = self.request("POST", "/api/messages",
                                          {"from_crew": "SAM", "preset_id": "0"}, self.KIOSK)
        self.assertEqual((status, payload["message"]["status"]), (200, "sent"))
        status, payload, _ = self.request("GET", "/api/messages")
        self.assertEqual((status, len(payload["messages"]), payload["channel"]), (200, 1, "online"))

    def test_post_protections(self):
        body = {"from_crew": "SAM", "preset_id": "0"}
        self.assertEqual(self.request("POST", "/api/messages", body, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.request("POST", "/api/messages", body,
                                      {"X-LCARS-Kiosk": "1", "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/messages", b"[1]", self.KIOSK)[0], 400)
        self.assertEqual(self.request("POST", "/api/messages", b"{bad", self.KIOSK)[0], 400)
        self.assertEqual(self.request("POST", "/api/messages", b"{" + b" " * 5000 + b"}", self.KIOSK)[0], 400)
        self.assertEqual(self.fake.sent, [])

    def test_preflight_is_never_answered(self):
        status, _, resp = self.request("OPTIONS", "/api/messages", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-lcars-kiosk,content-type"})
        self.assertEqual(status, 405)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertIsNone(resp.getheader("Access-Control-Allow-Headers"))

    def test_read_endpoint(self):
        m = self.svc.handle_event(msg_event("111", "hi"))
        self.assertEqual(self.request("POST", "/api/messages/%s/read" % m["id"], {}, self.KIOSK)[0], 200)
        self.assertEqual(self.svc.snapshot()["unread"], 0)
        self.assertEqual(self.request("POST", "/api/messages/zzz/read", {}, self.KIOSK)[0], 404)

    def test_config_and_static_files_stay_private(self):
        status, payload, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["messaging_enabled"])
        self.assertEqual((payload["crew"], payload["contacts"]), (["SAM", "RILEY"], ["MOM", "DAD"]))
        self.assertNotIn("111", json.dumps(payload))
        for path in ("/messages.json", "/config.json", "/config.json.bak", "/server.py", "/.gitignore"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_home_screen_icon_and_its_aliases(self):
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                     "/apple-touch-icon-152x152.png", "/apple-touch-icon-76x76-precomposed.png"):
            status, _, resp = self.request("GET", path)
            self.assertEqual((status, resp.getheader("Content-Type")), (200, "image/png"), path)
        for path in ("/apple-touch-icon.png.bak", "/apple-touch-icon-../config.json",
                     "/apple-touch-icon-x.png", "/apple-touch-icon-1x1/../../config.json"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_page_is_revalidated_but_api_is_not_stored(self):
        status, _, resp = self.request("GET", "/")
        self.assertEqual((status, resp.getheader("Cache-Control")), (200, "no-cache"))
        _, _, resp = self.request("GET", "/api/config")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_ip_allowlist(self):
        self.svc.allowed_ips = {"10.9.9.9"}

        class FakeHandler(object):
            client_address = ("192.168.1.50", 1234)
        self.assertFalse(server.LCARSHandler.client_allowed(FakeHandler()))
        FakeHandler.client_address = ("::ffff:10.9.9.9", 1)
        self.assertTrue(server.LCARSHandler.client_allowed(FakeHandler()))
        FakeHandler.client_address = ("127.0.0.1", 1)  # the server machine itself
        self.assertTrue(server.LCARSHandler.client_allowed(FakeHandler()))

    def test_messaging_off_behaves_like_before(self):
        server.MESSAGING = None
        status, payload, _ = self.request("GET", "/api/messages")
        self.assertEqual((status, payload["enabled"]), (200, False))
        self.assertEqual(self.request("POST", "/api/messages", {"preset_id": "0"}, self.KIOSK)[0], 503)
        status, payload, _ = self.request("GET", "/api/config")
        self.assertFalse(payload["messaging_enabled"])
        self.assertNotIn("crew", payload)
        self.assertEqual(self.request("GET", "/")[0], 200)


class SoundFileTests(unittest.TestCase):
    """Only files mapped in sound_files, only from sounds/, only audio types."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcars-sounds-")
        self.sounds = os.path.join(self.dir, "sounds")
        os.mkdir(self.sounds)
        for name, data in (("hail.mp3", b"ID3fake"), ("beep.wav", b"RIFFfake"), ("other.mp3", b"x"),
                           ("big.m4a", b"0" * (server.MAX_SOUND_BYTES + 1))):
            with open(os.path.join(self.sounds, name), "wb") as fh:
                fh.write(data)
        with open(os.path.join(self.dir, "secret.mp3"), "wb") as fh:
            fh.write(b"outside")
        self.patches = [(server, "SOUNDS_DIR", server.SOUNDS_DIR),
                        (server.CONFIG, "sound_files", server.CONFIG["sound_files"]),
                        (server.LCARSHandler, "log_message", server.LCARSHandler.log_message)]
        server.SOUNDS_DIR = self.sounds
        server.CONFIG["sound_files"] = {"incoming": "hail.mp3", "tap": "beep.wav", "ack": "big.m4a",
                                        "fail": "../secret.mp3", "sent": "missing.mp3"}
        server.LCARSHandler.log_message = lambda *args: None
        server.MESSAGING = None
        self.httpd = server.make_server(0)
        self.port = self.httpd.server_address[1]
        t = threading.Thread(target=self.httpd.serve_forever)
        t.daemon = True
        t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for obj, key, value in self.patches:
            if isinstance(obj, dict):
                obj[key] = value
            else:
                setattr(obj, key, value)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), body

    def test_mapped_files_are_served_with_audio_types(self):
        self.assertEqual(self.get("/sounds/hail.mp3"), (200, "audio/mpeg", b"ID3fake"))
        self.assertEqual(self.get("/sounds/beep.wav")[:2], (200, "audio/wav"))

    def test_everything_else_is_404(self):
        for path in ("/sounds/other.mp3",             # in the folder but not mapped
                     "/sounds/big.m4a",               # mapped but too big
                     "/sounds/missing.mp3",           # mapped but absent
                     "/sounds/..%2Fsecret.mp3", "/sounds/../secret.mp3",
                     "/secret.mp3", "/sounds/", "/sounds/README.md",
                     "/sounds/..%2Fconfig.json", "/sounds/%2e%2e/server.py"):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
