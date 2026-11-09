"""Tests that the server stops cleanly on SIGTERM (what `docker stop` and systemd send).

Standard library only; no real network calls (no config file, so messaging is off).

Run from the repo root:  python3 -m unittest discover -s tests
"""

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipIf(os.name != "posix", "SIGTERM handling is POSIX-only")
class StopSignalTest(unittest.TestCase):
    def test_sigterm_shuts_down_cleanly(self):
        tmp = tempfile.mkdtemp(prefix="lcars-stop-")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LCARS_")}
        env["LCARS_CONFIG"] = os.path.join(tmp, "no-such-config.json")
        port = _free_port()
        env["LCARS_PORT"] = str(port)
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(ROOT, "server.py")],
            env=env, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.time() + 10
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    if time.time() > deadline or proc.poll() is not None:
                        self.fail("server did not start")
                    time.sleep(0.1)
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn(b"Shutting down.", out)


if __name__ == "__main__":
    unittest.main()
