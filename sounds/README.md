# Optional sound files

By default the PADD makes its own original tones with Web Audio, so this folder can stay empty.

To use your own sounds instead:

1. Put `.mp3`, `.m4a` or `.wav` files here. Keep each one short and about 100 KB or less. Files over 2 MB are not served.
2. Map them to cues with `sound_files` in `config.json`, for example:

   ```json
   "sound_files": {"incoming": "hail.mp3", "sent": "confirm.wav"}
   ```

   The cues are `tap`, `incoming`, `transmit`, `sent`, `ack`, `fail`, `open`, `lcars` (the LCARS title button, which reloads the page), and `belay`, `abort` and `acknowledged` (the BELAY, ABORT and ACKNOWLEDGED buttons). A cue without a file keeps its built-in tone.

   Easier: run `python3 configure.py sounds`. It lists the files in this folder and lets you pick one for each cue.
3. Restart the server and reload the PADD.

The server only serves files that are listed in `sound_files`, and only from this folder. Everything here except this README is git-ignored, so your files are never committed.

Only use sounds you have the rights to. Don't add clips taken from TV shows or films to a public fork.
