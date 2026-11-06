"""Entry-point wiring tests: argument parsing and preload against a save."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import loader
import main
import map_store
from tests import helpers


class MainWiringTests(unittest.TestCase):
    def test_parser_defaults_and_overrides(self):
        args = main.build_parser().parse_args(
            ["--world", "some/world", "--host", "0.0.0.0", "--port", "9001", "--preload"]
        )
        self.assertEqual(args.world, "some/world")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9001)
        self.assertTrue(args.preload)

    def test_default_db_is_per_world(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            a = loader.ChunkLoader(helpers.build_coast_world(one))
            b = loader.ChunkLoader(helpers.build_coast_world(two))
            path_a, path_b = main.default_db_path(a), main.default_db_path(b)
            self.assertNotEqual(path_a, path_b)
            self.assertEqual(Path(path_a).parent.name, "maps")
            self.assertEqual(main.default_db_path(a), path_a)  # stable for the same save
        self.assertEqual(main.default_db_path(loader.ChunkLoader("does/not/exist")), "map_store.db")

    def test_explicit_db_flag_still_wins(self):
        args = main.build_parser().parse_args(["--db", "x.db"])
        self.assertEqual(args.db, "x.db")

    def test_preload_populates_cold_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = helpers.build_coast_world(tmp)
            chunk_loader = loader.ChunkLoader(world)
            store = map_store.MapStore(str(Path(tmp) / "map.db"), chunk_loader)
            try:
                main.preload(store)
                stats = store.stats()
                self.assertEqual(stats["cold_chunks"], 4)
                self.assertEqual(stats["dirty_chunks"], 0)
            finally:
                store.stop()
                chunk_loader.close()

    def test_preload_is_a_noop_without_a_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunk_loader = loader.ChunkLoader(Path(tmp) / "missing")
            store = map_store.MapStore(str(Path(tmp) / "map.db"), chunk_loader)
            try:
                self.assertFalse(chunk_loader.available)
                main.preload(store)
                self.assertEqual(store.stats()["cold_chunks"], 0)
            finally:
                store.stop()
                chunk_loader.close()


if __name__ == "__main__":
    unittest.main()
