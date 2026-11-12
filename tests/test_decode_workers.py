"""Decode worker processes exit when the server process is killed (they must not linger)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, %r)
    import multiprocessing
    import api, map_store
    from tests import helpers
    if __name__ == "__main__":
        store = map_store.MapStore(":memory:", helpers.FakeLoader({}))
        server = api.TopographyAPI(store, "127.0.0.1", 0, decode_workers=2)
        server.decode_chunks([{"cx": "bad"}, {"cx": "bad"}])
        print(" ".join(str(p.pid) for p in multiprocessing.active_children()), flush=True)
        time.sleep(600)
""")


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class DecodeWorkerLifetimeTests(unittest.TestCase):
    def test_workers_exit_when_the_server_is_killed(self):
        child = subprocess.Popen([sys.executable, "-c", CHILD % str(ROOT)], cwd=ROOT,
                                 stdout=subprocess.PIPE, text=True)
        try:
            pids = [int(p) for p in child.stdout.readline().split()]
            self.assertTrue(pids, "the pool started worker processes")
            self.assertTrue(all(_alive(p) for p in pids))
            child.kill()
            child.wait(timeout=10)
            deadline = time.time() + 15
            while time.time() < deadline and any(_alive(p) for p in pids):
                time.sleep(0.5)
            self.assertFalse([p for p in pids if _alive(p)], "workers outlived the killed server")
        finally:
            if child.poll() is None:
                child.kill()


if __name__ == "__main__":
    unittest.main()
