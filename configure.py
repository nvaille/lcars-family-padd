#!/usr/bin/env python3
"""
LCARS Family PADD - setup wizard (terminal). Standard library only.

  python3 configure.py messaging   set up Telegram messaging: bot token, pair contacts,
                                   optional family group, crew names, test transmission
  python3 configure.py show        print the current settings with secrets masked

Run it on the server machine, in its own terminal window, while the PADD server is
stopped (only one program at a time can read a Telegram bot's updates).
Settings are merged into config.json (a backup is written to config.json.bak first).
"""

import contextlib
import getpass
import io
import json
import os
import re
import shutil
import socket
import sys
import uuid

with contextlib.redirect_stdout(io.StringIO()):  # the server prints config notes on import
    import server

NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .'-]{0,19}$")
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,50}$")
RESERVED_NAMES = ("ALL", "ALL HANDS")
TEST_TEXT = "LCARS PADD ONLINE"


class Abort(Exception):
    pass


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


def summary_lines(raw):
    contacts = raw.get("contacts") or []
    lines = [
        "messaging_provider     %s" % (raw.get("messaging_provider") or "none"),
        "telegram_bot_token     %s" % mask(raw.get("telegram_bot_token")),
        "telegram_group_chat_id %s" % ("(set, hidden)" if raw.get("telegram_group_chat_id") else "(not set)"),
        "contacts               %s" % (", ".join(
            "%s (paired)" % c.get("name") for c in contacts if isinstance(c, dict)) or "(none)"),
        "crew                   %s" % (", ".join(server._as_list(raw.get("crew") or [], "crew")) or "(none)"),
    ]
    for key in sorted(raw):
        if key not in ("messaging_provider", "telegram_bot_token", "telegram_group_chat_id",
                       "contacts", "crew", "ics_feed_url"):
            lines.append("%-22s %s" % (key, json.dumps(raw[key])))
    if "ics_feed_url" in raw:
        lines.append("%-22s %s" % ("ics_feed_url", mask(raw["ics_feed_url"])))
    return lines


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
# The messaging wizard
# ---------------------------------------------------------------------------

class Wizard(object):
    def __init__(self, raw, ask=input, secret=getpass.getpass, out=print,
                 make_transport=server.TelegramTransport):
        self.raw = raw
        self.ask = ask
        self.secret = secret
        self.out = out
        self.make_transport = make_transport
        self.transport = None
        self.bot_username = ""

    # ----- prompts -----

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
    print(__doc__.strip())
    print("\n(The full guided setup for location, calendar, presets and sounds is coming in a later version.)")
    return 0 if command in ("", "-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
