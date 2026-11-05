"""Tiered map store tests: hot/warm/cold behavior and diff ingestion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import map_store
from tests import helpers


def column_index(x: int, z: int) -> int:
    return (x % 16) + (z % 16) * 16


class MapStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "map_store.db"
        self.records = {
            (0, 0): helpers.make_record(0, 0, height=64),
            (1, 0): helpers.make_record(
                1, 0, height=60, water_level=63, surface="minecraft:sand"
            ),
        }
        self.loader = helpers.FakeLoader(self.records)
        self.store = map_store.MapStore(str(self.db_path), self.loader)
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        store = getattr(self, "store", None)
        if store is not None:
            try:
                store.stop()
            except Exception:
                pass
            self.store = None

    def test_loads_from_base_loader(self):
        data = self.store.get_or_load(0, 0)
        self.assertIsNotNone(data)
        self.assertEqual(data["source"], "base")
        self.assertEqual(data["surface"]["y"][0], 64)

    def test_get_window_reports_missing_chunks(self):
        records, missing = self.store.get_window(0, 0, 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(missing, 7)

    def test_place_updates_surface_and_composition(self):
        x, y, z = 3, 70, 4
        accepted = self.store.apply_diff(
            {
                "action": "place",
                "x": x,
                "y": y,
                "z": z,
                "block_name": "minecraft:oak_planks",
            }
        )
        self.assertTrue(accepted)
        data = self.store.get_chunk(0, 0)
        index = column_index(x, z)
        self.assertEqual(data["surface"]["y"][index], 70)
        self.assertEqual(data["surface"]["materials"][index], "minecraft:oak_planks")
        self.assertFalse(data["surface"]["water"][index])
        self.assertEqual(data["surface"]["ground_y"][index], 70)
        self.assertEqual(data["composition"]["counts"]["engineered"], 1)

    def test_break_uses_plugin_surface_hints(self):
        self.store.apply_diff(
            {
                "action": "place",
                "x": 3,
                "y": 70,
                "z": 4,
                "block_name": "minecraft:oak_planks",
            }
        )
        accepted = self.store.apply_diff(
            {
                "action": "break",
                "x": 3,
                "y": 70,
                "z": 4,
                "block_name": "minecraft:oak_planks",
                "new_top_y": 64,
                "new_top_block": "minecraft:grass_block",
                "new_ground_y": 64,
                "new_ground_block": "minecraft:grass_block",
            }
        )
        self.assertTrue(accepted)
        data = self.store.get_chunk(0, 0)
        index = column_index(3, 4)
        self.assertEqual(data["surface"]["y"][index], 64)
        self.assertEqual(data["surface"]["materials"][index], "minecraft:grass_block")
        self.assertEqual(data["composition"]["counts"]["engineered"], 0)

    def test_break_to_air_above_surface_does_not_lower_terrain(self):
        self.store.apply_diff(
            {"action": "break", "x": 3, "y": 80, "z": 4, "block_name": "minecraft:stone"}
        )
        data = self.store.get_chunk(0, 0)
        index = column_index(3, 4)
        self.assertEqual(data["surface"]["y"][index], 64)

    def test_diff_to_unloaded_chunk_loads_base_state_first(self):
        accepted = self.store.apply_diff(
            {
                "action": "place",
                "x": 20,
                "y": 70,
                "z": 4,
                "block_name": "minecraft:cobblestone",
            }
        )
        self.assertTrue(accepted)
        data = self.store.get_chunk(1, 0)
        other_index = column_index(21, 9)
        self.assertEqual(data["surface"]["materials"][other_index], "minecraft:water")
        placed_index = column_index(20, 4)
        self.assertEqual(data["surface"]["materials"][placed_index], "minecraft:cobblestone")

    def test_chunk_lifecycle_deltas_are_accepted_as_noops(self):
        self.assertTrue(
            self.store.apply_diff(
                {"action": "chunk_load", "cx": 0, "cz": 0, "x": 0, "y": -64, "z": 0}
            )
        )
        self.assertTrue(
            self.store.apply_diff(
                {"action": "chunk_unload", "cx": 0, "cz": 0, "x": 0, "y": -64, "z": 0}
            )
        )

    def test_invalid_deltas_are_rejected(self):
        self.assertFalse(self.store.apply_diff({"action": "place"}))
        self.assertFalse(self.store.apply_diff({"action": "explode", "x": 0, "y": 0, "z": 0}))
        self.assertFalse(
            self.store.apply_diff({"action": "place", "x": 0, "y": 0, "z": 0})
        )

    def test_dirty_chunks_are_persisted_and_recalled_cold(self):
        self.store.apply_diff(
            {
                "action": "place",
                "x": 2,
                "y": 72,
                "z": 2,
                "block_name": "minecraft:oak_planks",
            }
        )
        self.assertGreaterEqual(self.store.flush(), 1)
        self.store.stop()
        self.store = None

        reopened = map_store.MapStore(str(self.db_path), loader=None)
        self.addCleanup(reopened.stop)
        cold = reopened.get_chunk(0, 0)
        self.assertIsNotNone(cold)
        self.assertEqual(cold["source"], "cold")
        index = column_index(2, 2)
        self.assertEqual(cold["surface"]["y"][index], 72)
        self.assertEqual(cold["surface"]["materials"][index], "minecraft:oak_planks")
        self.assertEqual(reopened.stats()["cold_chunks"], 1)


if __name__ == "__main__":
    unittest.main()
