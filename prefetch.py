"""Background perception-window prefetch worker.

The ``POST /position`` endpoint accepts the player's current position and
returns immediately.  The worker keeps only the most recent request, so a newer
position silently supersedes an older one that has not started yet.  Repeated
requests for the same chunk window are debounced: once a window has been
touched, identical requests are ignored until the player leaves the window.

The worker never raises into the HTTP request path; failures are logged.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("topography.prefetch")


class PrefetchWorker:
    def __init__(self, store, debounce_seconds: float = 0.0):
        self.store = store
        self.debounce_seconds = float(debounce_seconds)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._latest: tuple[int, int, int, int] | None = None
        self._last_completed: tuple[int, int, int] | None = None
        self._seq = 0
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ api
    def submit(self, x: int, z: int, size_chunks: int) -> int:
        """Queue a window around block ``(x, z)``; return its chunk count."""
        center_cx = int(x) >> 4
        center_cz = int(z) >> 4
        size = int(size_chunks)
        if size < 1:
            raise ValueError("size_chunks must be positive")
        key = (center_cx, center_cz, size)
        with self._lock:
            if self._latest is not None and self._latest[:3] == key:
                return size * size
            if self._latest is None and key == self._last_completed:
                return size * size
            self._seq += 1
            self._latest = (center_cx, center_cz, size, self._seq)
        self._wake.set()
        return size * size

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="topography-prefetch", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    # --------------------------------------------------------------- worker
    def _resolve_step(self):
        """One bounded step of the store's eager resolution of chunks pinned with no data."""
        step = getattr(self.store, "resolve_pending", None)
        if step is None:
            return
        try:
            step()
        except Exception:  # pragma: no cover - defensive
            log.exception("Resolving pinned chunks failed")

    def _run(self):
        while not self._stop.is_set():
            if not self._wake.wait(0.5):
                self._resolve_step()
                continue
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    item = self._latest
                    self._latest = None
                if item is None:
                    break
                center_cx, center_cz, size, _seq = item
                try:
                    self.store.touch_window(center_cx, center_cz, size)
                except Exception:  # pragma: no cover - defensive
                    log.exception(
                        "Prefetch failed for chunk (%s, %s) size %s",
                        center_cx,
                        center_cz,
                        size,
                    )
                else:
                    with self._lock:
                        self._last_completed = (center_cx, center_cz, size)
            self._resolve_step()   # keeps running even while positions arrive continuously
