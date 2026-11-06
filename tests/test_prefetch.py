"""Tests for the background perception-window prefetch worker."""

from __future__ import annotations

import threading
import time
import unittest

import prefetch


class _FakeStore:
    def __init__(self):
        self.calls = []
        self.called = threading.Event()
        self.gate = None

    def touch_window(self, center_cx, center_cz, size_chunks):
        self.calls.append((center_cx, center_cz, size_chunks))
        self.called.set()
        if self.gate is not None:
            self.gate.wait(2.0)
        return [], 0


class PrefetchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store = _FakeStore()
        self.worker = prefetch.PrefetchWorker(self.store)

    def tearDown(self):
        self.worker.stop()

    def _wait_for_calls(self, count, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.store.calls) >= count:
                return True
            time.sleep(0.01)
        return False

    def test_submit_queues_the_window_on_the_worker(self):
        self.worker.start()
        queued = self.worker.submit(120, -46, 16)
        self.assertEqual(queued, 256)
        self.assertTrue(self._wait_for_calls(1))
        self.assertEqual(self.store.calls[0], (7, -3, 16))

    def test_duplicate_centers_are_debounced(self):
        self.worker.start()
        self.worker.submit(0, 0, 8)
        self.assertTrue(self._wait_for_calls(1))
        self.worker.submit(0, 0, 8)
        self.worker.submit(0, 0, 8)
        time.sleep(0.2)
        self.assertEqual(len(self.store.calls), 1)

    def test_newer_position_supersedes_a_pending_one(self):
        first = self.worker.submit(0, 0, 8)
        second = self.worker.submit(160, 160, 8)
        self.assertEqual(first, 64)
        self.assertEqual(second, 64)
        self.worker.start()
        self.assertTrue(self._wait_for_calls(1))
        time.sleep(0.2)
        self.assertEqual(self.store.calls, [(10, 10, 8)])

    def test_moving_to_a_new_chunk_is_prefetched(self):
        self.worker.start()
        self.worker.submit(0, 0, 8)
        self.assertTrue(self._wait_for_calls(1))
        self.worker.submit(160, 160, 8)
        self.assertTrue(self._wait_for_calls(2))
        self.assertEqual(self.store.calls[-1], (10, 10, 8))

    def test_invalid_size_is_rejected(self):
        with self.assertRaises(ValueError):
            self.worker.submit(0, 0, 0)

    def test_stop_is_idempotent(self):
        self.worker.start()
        self.worker.stop()
        self.worker.stop()


if __name__ == "__main__":
    unittest.main()
