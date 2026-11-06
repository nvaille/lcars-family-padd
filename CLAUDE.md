# LCARS Family PADD — notes for Claude Code

A retro-futuristic LCARS family dashboard for wall-mounted kiosks. Public, MIT-licensed repo:
`nvaille/lcars-family-padd` (owner's personal GitHub account).

## Files

- `server.py`: Python HTTP server (standard library only). Serves `index.html` and the `/api/*` JSON endpoints.
- `index.html`: the whole kiosk front end (inline CSS and JS, plus the embedded Antonio font).
- `config.example.json`: committed placeholders. `config.json` holds real settings, is **git-ignored and must never be committed**.
- `README.md`: user docs, including the configuration table.
- `docs/`: design and planning docs. `docs/text-messages-plan.md` is the spec for the text-messages feature.

## Hard rules

1. **The front end must keep working on a 1st-gen iPad Mini (iOS 9.3.5, Safari 9.1).**
   - Not allowed: ES6 (`let`, `const`, arrow functions, template literals, classes, Promises, `fetch`), CSS variables, CSS grid, and flex `gap`.
   - Use `var`, `function`, `XMLHttpRequest`, and callbacks.
   - Keep the rem-based scaling: JS sets the root font size, so the 1024×748 design scales.
   - Keep the LCARS geometry: continuous spine, elbows, conduit bars and segments. No isolated boxed cards or generic rounded pill buttons.
2. **The server uses the Python standard library only**, if at all possible.
3. **Settings:**
   - Every new setting goes in `DEFAULTS` + `ENV_VARS` (an `LCARS_*` env var) in `server.py`.
   - Each one gets a placeholder in `config.example.json` and a row in the README configuration table.
   - Secrets (tokens, chat IDs, phone numbers) only ever live in `config.json` or env vars.
4. **No leaks:**
   - Never serve anything new through the static-file handler that could leak config or secrets.
   - `/api/config` exposes only non-secret display values.
   - Don't log message bodies or secrets.

## Git rules

- **Never push to `main`.** It's protected. Work on feature branches, then open a PR into `main` with `gh pr create`.
- **Commit identity, set repo-locally:** `nvaille <275858791+nvaille@users.noreply.github.com>`.
- **Before every push:**
  - Show me `git diff --stat` for what's being pushed.
  - Scan the diff for secrets: Telegram bot tokens (`\d{8,10}:[A-Za-z0-9_-]{35}`), chat IDs, phone numbers, real calendar URLs, `config.json`.
  - Ask me before pushing.
- **The owner is new to Claude Code in the terminal.** Explain what you're doing in plain language, and stop at checkpoints for review.

## Running and testing

- **Run the server:** `python3 server.py` (port 8000 by default; stop it with Ctrl+C).
- **Kiosk URL:** `http://<mac-ip>:8000` (`ipconfig getifaddr en0` prints the Mac's IP).
- **Server tests:** use `unittest` from the standard library (`python3 -m unittest discover -s tests`). Don't make real network calls in tests; use a fake transport.
- **ES5 check:** extract the inline `<script>` and parse it as ES5, for example with `npx --yes acorn --ecma5 /tmp/padd.js`. Don't commit any tooling.
- **Layout check:** check it in a desktop browser at 1024×748. The owner tests on the real iPad.
