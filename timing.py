"""Slow-path logging: a re-entrant lock that reports long waits and holds, and a request timer.

Logging only; behaviour is unchanged. Threshold: ``CARTOGRAPHY_SLOW_MS`` (default 200 ms).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

log = logging.getLogger("cartography.timing")
SLOW_S = float(os.environ.get("CARTOGRAPHY_SLOW_MS", "200")) / 1000.0


def _caller(depth: int) -> str:
    try:
        frame = sys._getframe(depth)
        return f"{frame.f_code.co_name}:{frame.f_lineno}"
    except ValueError:  # pragma: no cover - stack shorter than expected
        return "?"


class TimedRLock:
    """``threading.RLock`` that logs outer acquisitions waiting or held longer than ``SLOW_S``."""

    def __init__(self, name: str = "store"):
        self._lock = threading.RLock()
        self._name = name
        self._local = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        depth = getattr(self._local, "depth", 0)
        if depth:
            ok = self._lock.acquire(blocking, timeout)
            if ok:
                self._local.depth = depth + 1
            return ok
        who = _caller(2)
        start = time.perf_counter()
        ok = self._lock.acquire(blocking, timeout)
        if not ok:
            return False
        waited = time.perf_counter() - start
        if waited > SLOW_S:
            log.warning("lock %s wait %.0f ms by %s", self._name, waited * 1000, who)
        self._local.depth = 1
        self._local.since = time.perf_counter()
        self._local.who = who
        return True

    def release(self) -> None:
        depth = self._local.depth - 1
        self._local.depth = depth
        if not depth:
            held = time.perf_counter() - self._local.since
            if held > SLOW_S:
                log.warning("lock %s held %.0f ms by %s", self._name, held * 1000, self._local.who)
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class RequestTimer:
    """``with RequestTimer(method, path): ...`` logs requests slower than ``SLOW_S``."""

    def __init__(self, method: str, path: str):
        self.method, self.path = method, path

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        took = time.perf_counter() - self.start
        if took > SLOW_S:
            log.warning("slow %s %s %.0f ms (thread %s)", self.method, self.path, took * 1000,
                        threading.current_thread().name)
        return False
