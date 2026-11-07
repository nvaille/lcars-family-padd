# Text Messages Feature — Plan & UI Flow

Status: **research complete. All decisions made; ready for implementation.** Branch: `feature/text-messages`.
Updated: 2026-11-06. Copied from the "LCARS Family PADD" claude.ai project research doc.

## 1. Goal

A two-way family message channel on the kiosk iPad (1st-gen iPad Mini, iOS 9.3.5).

- Main use: a kid with no phone tells a parent "I'm home from school" (or something similar) from the PADD.
- Parents can text back, and their messages show on the PADD with an alert.
- Only family members can send or receive. Anyone else is dropped silently.
- Version 1 uses preset messages. Custom typed messages come right after.
- Messages from the last 24 hours are shown and kept.

### Decisions

| # | Decision |
|---|---|
| D1 | **Transport: a Telegram bot.** Everyone receiving PADD messages will install Telegram. Twilio stays an optional future adapter. |
| D2 | **Contacts are user-defined.** There's no fixed number. At least 2 is expected, so the "who to send to" step stays in the flow. |
| D3 | **Multiple kids are supported.** The kiosk can have several "crew" members who send from it, and each message is signed with the sender's name. |
| D4 | **A setup wizard** (`configure.py`, terminal-based) handles the growing list of settings. The hardest step it solves is pairing Telegram chat IDs. See §7. |
| D5 | **The kiosk iPad's Auto-Lock is set to Never**, so the visual alerts are always seen. |
| D6 | **Star Trek-style sound effects are wanted.** They'll be original synthesized tones by default, with user-supplied files as an option. The iOS 9 limits and how to work around them are in §8. |
| D7 | **Quiet hours are on by default, 22:00–07:00** (`quiet_hours: "22:00-07:00"`). During those hours alerts are visual only. Users can change or clear the setting. |
| D8 | **The crew is kids only.** Parents have phones, so they're contacts and never kiosk senders. |
| D9 | **No per-kid PIN.** The name a kid picks on WHO IS TRANSMITTING? is trusted. Don't build PIN support. |

## 2. How messages get in and out

| | **A. Telegram bot (chosen)** | B. Twilio SMS number | C. ntfy push | D. iMessage via a Mac |
|---|---|---|---|---|
| Parent experience | Telegram app (1:1 chat with the bot, and/or a family group) | Ordinary texts, no app | ntfy app notifications | Ordinary iMessages |
| Two-way | Yes. Parents type replies, or tap quick-reply buttons under the PADD's message | Yes, after 10DLC registration | Weak: the iOS app can't publish yet | Yes |
| Cost | Free | About $19 one-time, plus about $3.20/month, plus about $0.01 per text | Free | Free |
| Setup friction | Create the bot with @BotFather and pair each contact (the wizard does this) | A2P 10DLC Sole Proprietor registration (mobile OTP, campaign vetting, 2–3 weeks, can be rejected) | Topic plus access token | The server must run on an always-on Mac |
| Python stdlib only | Yes (urllib, HTTPS JSON) | Yes (urllib, basic auth) | Yes | Partly: AppleScript via osascript, plus reading `chat.db` with sqlite3 |
| Nothing opened to the internet | Yes (long-poll `getUpdates`) | Yes (poll the Messages API) | Yes | Yes |
| Fits the cross-platform goal | Yes | Yes | Yes | No (macOS only) |

### Ruled out

- **Carrier email-to-SMS gateways:** AT&T shut down June 2025. T-Mobile's gateway is effectively dead since late 2024. Verizon's full shutdown is planned for March 31, 2027.
- **Twilio webhooks** would expose the server, which has no authentication, to the internet.
- **ntfy:** on iPhone, parents can't reply from the app. Publishing from the iOS app is still on ntfy's roadmap.

### Architecture

Build a **transport adapter** layer in `server.py`. Each transport provides `poll()` and `send(recipient, text, buttons)`. Ship Telegram first; Twilio can be added later as a second adapter with no UI changes.

Telegram specifics:
- **Quick replies:** the bot's outgoing message carries inline-keyboard buttons such as [GOT IT] [CALL ME] [ON MY WAY]. A tap arrives as a `callback_query`. The PADD marks its message `ACK <NAME>` (the first contact to tap) and logs the button text as an incoming line.
- **Free text:** parents can type a reply, and it shows on the PADD as an incoming transmission.
- **1:1 vs group:** each contact pairs a 1:1 chat with the bot, which allows sending to one person. An optional family group (`telegram_group_chat_id`) can be ALL HANDS. If no group is configured, ALL HANDS sends a direct message to every contact. In a group, the bot needs privacy mode off, or parents must use Reply.

## 3. People model (multiple kids, any number of contacts)

There are two lists, both in config:

- **Crew**: the **kids** who use the kiosk. They have no phone and no Telegram account. Parents are never crew. Example: `["SAM", "RILEY"]`.
- **Contacts**: the people who get messages on Telegram (parents, grandparents and so on). Each has a display name and a chat ID. Example: `[{"name":"MOM","telegram_chat_id":"…"}, {"name":"DAD", …}, {"name":"GRANDMA", …}]`.

There's no limit on either list. The UI pages at 6 buttons per screen.

**Outgoing messages** are signed with the crew member's name, so Telegram shows `SAM · HOME PADD`. There's no PIN or other check on who is sending (D9).

**Incoming messages** are for the whole household by default. A contact can address one kid by starting the message with the name: `@SAM pizza at 6` or `SAM: pizza at 6`. The PADD then tags the message `FOR SAM`, and Sam's name lights up on the unread strip.

**Single-kid households:** if there's only one crew member, the "who is transmitting" step is skipped automatically.

## 4. Star Trek vocabulary for the UI

These are original UI strings in a Star Trek style, all caps per the existing CSS:

| Event | Label |
|---|---|
| Unread incoming message | INCOMING TRANSMISSION |
| Banner sub-line | SUBSPACE MESSAGE FROM <NAME> |
| Message addressed to one kid | FOR <CREW NAME> |
| Message list screen | COMMUNICATIONS LOG |
| Start a new message | OPEN CHANNEL / HAIL |
| Pick the sender (crew) | WHO IS TRANSMITTING? |
| Pick the recipient | SELECT CHANNEL / HAIL WHOM? |
| Recipient: everyone | ALL HANDS |
| Send button | TRANSMIT |
| Cancel | ABORT |
| Sending | TRANSMITTING… |
| Sent | TRANSMISSION SENT |
| Contact tapped a quick reply | ACKNOWLEDGED BY <NAME> |
| Failed | TRANSMISSION FAILED · SUBSPACE INTERFERENCE |
| Retry | RETRANSMIT |
| One-tap reply on a received message | ACKNOWLEDGE |
| Back | RETURN |
| Status code (the `.sig` spot) | COMMS NOMINAL / 2 UNREAD / CHANNEL OFFLINE |
| Audio not yet unlocked | AUDIO STANDBY · TOUCH TO ARM |
| Quiet hours active | SILENT RUNNING |

## 5. UI flow

All screens keep the existing frame: the left spine, the elbows, the conduit bars and the top bay with the clock. Only the **lower bay** (`.bot-body`) and the **lower spine segments** change per screen. The spine segments become the nav buttons, which is classic LCARS and gives big touch targets. Every interactive element is at least about 3rem tall, roughly 44pt on the iPad.

The existing `touchmove` blocker means there's no scrolling. Lists page with ▲/▼ buttons.

### Screen 0: Dashboard (existing, plus comms)

```
SPINE            LOWER BAY (unchanged: weather | stoic / mission log)
[03-111968]
[04 HAIL    ] ← shortcut straight to compose (Screen 3)
[05 COMMS   ] ← opens Communications Log (Screen 1)
[06-071984]
```
- **Idle:** spine segment 05 reads `COMMS`.
- **Unread incoming message:**
  - Segment 05 blinks red-orange (`#cc4444` ↔ `#ff6600`, 1.4s step) and reads `INCOMING`.
  - An **INCOMING TRANSMISSION** strip replaces the Stoic Directive. It shows `FROM MOM · 15:42 · FOR SAM` and the first line of the message. Tapping the strip opens Screen 2.
  - The quote comes back once everything is read.
  - The caps pill `04-47219` (it already has a blinking `<small>`) shows `2 UNREAD`.
- **Sound:** see §8.

### Screen 1: Communications Log (last 24h)

```
SPINE            ┌ COMMUNICATIONS LOG ───────────────── 3 UNREAD ┐
[RETURN     ]    ◀ MOM → SAM      15:42  Pizza tonight, home by 6   ● NEW
[OPEN CHANNEL]   ▶ SAM → ALL      15:31  I'm home from school       ✓ ACK DAD
[▲ PAGE     ]    ▶ RILEY → DAD    12:05  Need a ride                SENT
[▼ PAGE     ]    ◀ DAD            08:10  Have a good day            READ
```
- Newest first, about 6 rows per page.
- The arrow shows direction: ◀ (blue `#66ccff`) is incoming, ▶ (orange `#ff9900`) is outgoing.
- Status reads NEW → READ for incoming. Outgoing goes TRANSMITTING → SENT → ACK <NAME>, or FAILED in red.
- Tapping a row opens Screen 2.
- An empty log shows `NO TRANSMISSIONS IN 24 HOURS`.

### Screen 2: Transmission detail

```
SPINE            ┌ TRANSMISSION · FROM MOM · FOR SAM ─── 15:42 · RECEIVED ┐
[RETURN     ]
[ACKNOWLEDGE]    "Pizza tonight. Home by 6.
[REPLY      ]     Start your homework."          (2.25rem, like .quote)
```
- Opening a message marks it READ.
- **ACKNOWLEDGE** sends a one-tap "Got it" back to the sender. It's signed with the addressed kid, or asks WHO IS TRANSMITTING? when the message wasn't addressed to anyone.
- **REPLY** opens Screen 3 with the recipient already set to the sender.
- Outgoing messages show `RETURN`, plus `RETRANSMIT` if the send failed.
- Long text shrinks to fit using the same loop as `fitQuote()`.

### Screen 3: Open channel (compose)

The order is **who → what → to whom → confirm**. Each preset can carry a default recipient, so the "to whom" step is usually pre-filled and shown on the confirm screen.

**3a. WHO IS TRANSMITTING?** (skipped when there's one crew member)
```
[ SAM ]  [ RILEY ]
```

**3b. Choose a message**
```
SAM · CHANNEL OPEN
[ I'M HOME FROM SCHOOL ] [ HOME SAFE      ] [ PLEASE CALL HOME ]
[ NEED A RIDE          ] [ AT A FRIEND'S  ] [ ALL GOOD         ]
[ CUSTOM MESSAGE ▸     ]
```
- Presets come from config, for example `{"text":"I'M HOME FROM SCHOOL","to":"ALL"}`. The `to` field is optional.
- **CUSTOM MESSAGE** (phase 2, hidden while `allow_custom_messages` is false) puts a text field in the **top half** of the bay. The iOS keyboard covers about the bottom 45% in landscape.
  - The field has a `118 / 160` counter.
  - TRANSMIT and ABORT sit next to the field, not below it.

**3c. HAIL WHOM?** (only when the preset has no default recipient, or when CHANGE is tapped)
```
[ MOM ]  [ DAD ]  [ GRANDMA ]  [ ALL HANDS ]      ▲/▼ if more than 6
```

**3d. Confirm**
```
SAM → ALL HANDS   "I'M HOME FROM SCHOOL"    [ TRANSMIT ]  [ CHANGE ]  [ ABORT ]
```
CHANGE goes back to 3c.

**3e. Result**
- TRANSMIT shows `TRANSMITTING…`, animated like the data cascade.
- Success shows `TRANSMISSION SENT · 15:44` for 3 seconds, then returns to Screen 1.
- Failure shows `TRANSMISSION FAILED` with RETRANSMIT and ABORT.

**Fastest path for the main use:** HAIL → SAM → I'M HOME FROM SCHOOL → TRANSMIT. That's **4 taps**, or 3 in a one-kid home.

### Global rules

- **Idle timeout:** any comms screen goes back to the dashboard after 90 seconds without a touch. A draft is discarded.
- **Wake on new message:** a new incoming message doesn't interrupt a compose in progress. It only updates the badge.
- **Polling:** the kiosk polls `GET /api/messages` every 15 seconds. `.sig` shows `CHANNEL OFFLINE` when the poll fails.
- **Retention:** messages are kept for `message_retention_hours` (24). The server prunes them.
- **Read receipts to contacts:** off by default. If turned on, they appear as a Telegram reaction or an edited message.

### What a contact sees (Telegram)

```
SAM · HOME PADD 🖖
I'M HOME FROM SCHOOL · 15:44
[ GOT IT ]  [ CALL ME ]  [ ON MY WAY ]
```

## 6. Server design notes

- **Background poller:** a `threading.Thread` runs `poll()` (long-poll `getUpdates` with an `offset`) and writes to a thread-safe message store. Only one process can consume `getUpdates` at a time, so the wizard's pairing step must run while the server is stopped, or the server must handle pairing itself.
- **Store:** `messages.json`, git-ignored and pruned to 24 hours. The `.json` extension is already refused by the static handler.
- **Endpoints:**
  - `GET /api/messages`: the 24-hour list plus unread counts.
  - `POST /api/messages`: send. The body is `{from_crew, to, preset_id | text}`.
  - `POST /api/messages/<id>/read`.
- **Protecting the endpoints that accept posted data:**
  - Require the custom header `X-LCARS-Kiosk: 1` plus a JSON body. This forces a CORS preflight, and the server never answers preflights.
  - Optional `kiosk_allowed_ips` allowlist.
  - Rate limit.
  - Recipient and sender names must be on the roster.
  - Text is capped in length.
  - In preset-only mode the server accepts only `preset_id`.
- **Server threads:** switch to `ThreadingHTTPServer`.
- **Logging:** don't log message bodies.
- **`/api/config`** may expose crew names, contact *names*, presets, `padd_sender_name` and the sound settings. It must never expose tokens or chat IDs.

### New settings (config.json / env, plus placeholders in config.example.json and README table rows)

| Key | Env var | Notes |
|---|---|---|
| `messaging_provider` | `LCARS_MESSAGING_PROVIDER` | `none` (default), `telegram`, `twilio` |
| `telegram_bot_token` | `LCARS_TELEGRAM_TOKEN` | **Secret** |
| `telegram_group_chat_id` | `LCARS_TELEGRAM_GROUP_ID` | Optional ALL HANDS group. **Private** |
| `contacts` | `LCARS_CONTACTS_JSON` | `[{"name":"MOM","telegram_chat_id":"…"}]`. **Private** |
| `crew` | `LCARS_CREW` | Kids who use the kiosk, comma-separated or a JSON list |
| `padd_sender_name` | `LCARS_PADD_SENDER_NAME` | For example `HOME PADD` |
| `message_presets` | `LCARS_MESSAGE_PRESETS_JSON` | `[{"text":"…","to":"ALL"}]` |
| `quick_replies` | `LCARS_QUICK_REPLIES_JSON` | Telegram buttons, for example `["GOT IT","CALL ME","ON MY WAY"]` |
| `allow_custom_messages` | `LCARS_ALLOW_CUSTOM_MESSAGES` | `false` in v1 |
| `message_retention_hours` | `LCARS_MESSAGE_RETENTION_HOURS` | Default 24 |
| `max_message_length` | `LCARS_MAX_MESSAGE_LENGTH` | Default 160 |
| `send_rate_limit_per_hour` | `LCARS_SEND_RATE_LIMIT` | Default 10 |
| `kiosk_allowed_ips` | `LCARS_KIOSK_IPS` | Optional |
| `sounds_enabled` | `LCARS_SOUNDS` | Default `true` |
| `sound_volume` | `LCARS_SOUND_VOLUME` | 0.0–1.0, default 0.6 |
| `tap_sounds` | `LCARS_TAP_SOUNDS` | LCARS beep on every button press. Default `true` |
| `quiet_hours` | `LCARS_QUIET_HOURS` | **Default `22:00-07:00`.** Visual alerts only during these hours. The range may cross midnight. An empty string turns quiet hours off |
| `incoming_repeat_minutes` | `LCARS_INCOMING_REPEAT_MINUTES` | Repeat the alert while unread. Default 5. `0` turns repeats off |
| `incoming_repeat_max` | `LCARS_INCOMING_REPEAT_MAX` | Default 3 |
| `sound_files` | `LCARS_SOUND_FILES_JSON` | Optional per-cue override, for example `{"incoming":"hail.mp3"}`. The files go in `sounds/` |

## 7. Setup wizard (`configure.py`)

### Why a terminal wizard, not a web page

- **Security:** a web setup page would add endpoints that *write secrets* to a server with no authentication.
- **Typing:** pasting a bot token on a 2012 iPad keyboard is painful.
- **What it is:** a standard-library terminal script that runs on the server machine. It works the same on macOS, Windows, Linux and the Pi.
- **Docker:** `docker run -it … python3 configure.py` writes to the mounted config volume.

### Usage

- `python3 configure.py` runs the full guided setup.
- `python3 configure.py messaging` (or `location`, `calendar`, `contacts`, `crew`, `presets`, `sounds`, `security`, `test`) re-runs one section. Contacts, crew and presets use add/remove/rename menus, so one item can change without redoing the rest.
- `python3 configure.py show` prints the current config with secrets masked (`tk_…abcd`).

### Sections

1. **Location:** type a city, the wizard looks it up with Open-Meteo's geocoding API (free, no key), you pick from the matches, and it saves the latitude, longitude and label.
2. **Calendar:** paste the ICS URL. Input is hidden with `getpass`, since the URL is a secret. The wizard fetches the feed, runs it through the server's own parser, and reports something like "Found 12 upcoming events. Next: …".
3. **Messaging:**
   - Pick a provider. For Telegram, the wizard explains the @BotFather steps.
   - Paste the token (hidden). The wizard checks it with `getMe` and shows the bot's @username.
   - **Pairing:** "Ask each contact to open t.me/<bot> and tap START." The wizard listens and lists each new person, you give them a display name (MOM, DAD…), and it saves their chat ID. Repeat for as many contacts as you like.
   - Optional: add the bot to a family group and send `/padd` there to capture the group ID.
   - The wizard warns if the server is running, because only one process can listen for Telegram updates.
4. **Crew:** the kids' names.
5. **Presets and quick replies:** show the defaults and let you edit, reorder and set default recipients.
6. **Sounds:** on/off, volume, and repeat settings. Quiet hours default to 22:00–07:00, and you can press Enter to keep that. Lists any files found in `sounds/` and lets you map them to cues.
7. **Kiosk security:** optional iPad IP allowlist and the rate limit.
8. **Test transmission:** sends `LCARS PADD ONLINE` to every contact and waits for a quick-reply tap to confirm two-way works.
9. **Save:**
   - Merges into the existing `config.json`, keeping any keys the wizard doesn't know.
   - Writes `config.json.bak` first, which also needs a `.gitignore` entry.
   - Sets the file to `chmod 600` where the OS supports it.
   - Prints a masked summary.

### Scope and branches

- The `feature/text-messages` PR includes a minimal `configure.py messaging`, since pairing chat IDs is required to use the feature at all.
- The full wizard (location, calendar, crew, presets, sounds) is a follow-up branch, `feature/setup-wizard`.

## 8. Sounds

### iOS 9 limits

1. **One touch per page load.** iOS 9 only unlocks Web Audio from a `touchend` event; Safari 9 dropped `touchstart` as an unlock trigger. After one tap anywhere, the page can play sounds on its own (including when a message arrives) until it's reloaded.
   - The unlock trick is to play a 1-sample silent buffer, created with `createBuffer(1, 1, 22050)`, from that first `touchend`. Keep retrying on later touches until it's confirmed.
2. **The nightly 3 a.m. reload re-locks audio.** Tapping the title to reload does too. Until someone touches the PADD after a reload, an incoming message is **visual only**. The 3 a.m. reload falls inside the default quiet hours.
   - **Indicator:** while audio isn't unlocked, a spine or caps label reads `AUDIO STANDBY · TOUCH TO ARM`. After the first touch it changes to `AUDIO ONLINE`.
   - **Option to test later:** make the nightly reload configurable (`nightly_reload: true`), and try a "soft refresh" that keeps the audio unlock.
3. **The side switch.** Web Audio is silenced by iOS mute (the side switch set to Mute, or Mute in Control Center).
   - **Setup step:** Settings → General → set the side switch to **Lock Rotation**, make sure mute is off, and turn the volume up. Put this in the README's kiosk setup section.
   - **Fallback (tried, failed):** start a looping, near-silent `<audio>` element on the first tap, which is reported to switch the page into a "playback" audio session that ignores the mute switch.
   - **Tested 2026-11-07: doesn't work on iOS 9.** A looping 1-second silent WAV (`data:` URI), started from the first tap, left the cues silent while the iPad was muted. The code was dropped; the README setup step (side switch → Lock Rotation, mute off) is the answer.

### Where the sounds come from (copyright)

The real Star Trek effects belong to CBS/Paramount. This public MIT-licensed repo must not ship clips taken from the shows.

- **Default: original synthesized tones** made with Web Audio oscillators in `index.html`. There are no audio files. Use ES5, `createOscillator` plus a gain envelope, and `noteOn` as the fallback for `start`.

  | Cue | Suggested original sound |
  |---|---|
  | `tap` | Short high blip (about 1.2 kHz, 40 ms) |
  | `incoming` | Rising three-tone chirp, played twice, then repeated every `incoming_repeat_minutes` while unread |
  | `transmit` | Quick upward frequency sweep |
  | `sent` | Two-tone confirm (low → high) |
  | `ack` | Soft single chime when a contact taps a quick reply |
  | `fail` | Low double buzz |
  | `open` | Gentle two-note "channel open" when entering COMMS |

- **Optional: user-supplied files.** Drop `.mp3`, `.m4a` or `.wav` files into `sounds/` and map them in `sound_files`. The folder is git-ignored apart from a README placeholder.
- **Optional, later: bundled CC0 packs**, with the license checked file by file and recorded in `sounds/CREDITS.md`.

### Quiet hours (default 22:00–07:00)

- **What's silent:** automatic sounds, including the `incoming` chime and its repeats. Visual alerts work as normal.
- **Sounds a person triggers:** tap beeps and the sent/fail cues still play, at about 30% volume.
- **Indicator:** the audio status label reads `SILENT RUNNING`.
- **Unread at 07:00:** the `incoming` chime plays once (if audio is armed).
- **Clock:** the kiosk's local clock. The range is parsed client-side and may cross midnight.

### Implementation notes

- **Decoding:** use the callback form of `decodeAudioData` (Safari 9 has no Promises), once per file after unlock.
- **Serving the files:** allow `.mp3`, `.m4a` and `.wav` through the static handler only from `sounds/`. Keep each file about 100 KB or less.
- **Sound test:** a long press on the `AUDIO ONLINE` label plays each cue in turn.

## Sources

- Telegram Bot API: https://core.telegram.org/bots/api
- Telegram bot features (keyboards, privacy mode): https://core.telegram.org/bots/features
- Twilio SMS pricing (US): https://www.twilio.com/en-us/sms/pricing/us
- Twilio Sole Proprietor fees: https://support.twilio.com/hc/en-us/articles/9550596959643-
- iOS 9 Web Audio unlock on touchend (WebKit bug 149367): https://bugs.webkit.org/show_bug.cgi?id=149367
- Web Audio and the iOS silent switch: https://developer.apple.com/forums/thread/23499
- Carrier email-to-SMS shutdowns: https://textbolt.com/migration/verizon
