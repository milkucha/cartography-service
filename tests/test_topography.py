"""Topography analysis tests: lines, shoreline, area summaries, recall."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import map_store
import topography
from tests import helpers


class TopographyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        records = {}
        for cx in (0, 1):
            for cz in (0, 1):
                if cx == 0:
                    records[(cx, cz)] = helpers.make_record(cx, cz, height=64)
                else:
                    records[(cx, cz)] = helpers.make_record(
                        cx, cz, height=60, water_level=63, surface="minecraft:sand"
                    )
        self.store = map_store.MapStore(
            str(Path(self._tmp.name) / "map.db"), helpers.FakeLoader(records)
        )
        self.addCleanup(self.store.stop)

    def test_surface_column_land(self):
        column = topography.surface_column(self.store, 10, 8)
        self.assertEqual(column["y"], 64)
        self.assertEqual(column["material"], "minecraft:grass_block")
        self.assertFalse(column["water"])

    def test_surface_column_water(self):
        column = topography.surface_column(self.store, 20, 8)
        self.assertEqual(column["y"], 63)
        self.assertEqual(column["material"], "minecraft:water")
        self.assertEqual(column["ground_y"], 60)
        self.assertTrue(column["water"])

    def test_heightmap_line_reports_run_and_crossing(self):
        result = topography.heightmap_line(self.store, 10, 8, 22, 8)
        self.assertEqual(result["from"], {"x": 10, "z": 8})
        self.assertEqual(result["to"], {"x": 22, "z": 8})
        self.assertEqual(result["sample_count"], 13)
        self.assertEqual(result["min_y"], 63)
        self.assertEqual(result["max_y"], 64)
        self.assertEqual(len(result["transitions"]), 1)
        crossing = result["transitions"][0]
        self.assertEqual((crossing["x"], crossing["z"]), (16, 8))
        self.assertEqual((crossing["from"], crossing["to"]), ("land", "water"))
        self.assertEqual(result["runs"][0]["y"], 64)
        self.assertEqual(result["runs"][-1]["y"], 63)

    def test_heightmap_line_without_water(self):
        result = topography.heightmap_line(self.store, 2, 2, 8, 2)
        self.assertEqual(result["transitions"], [])
        self.assertEqual(result["min_y"], 64)

    def test_shoreline_finds_coast_polyline(self):
        result = topography.shoreline_polygon(self.store, 10, 8, 8)
        self.assertFalse(result["water_at_center"])
        self.assertGreater(result["point_count"], 0)
        self.assertTrue(result["segments"])
        points = [tuple(point) for segment in result["segments"] for point in segment]
        self.assertTrue(any(x == 16 and 0 <= z <= 16 for x, z in points))

    def test_shoreline_at_water_center(self):
        result = topography.shoreline_polygon(self.store, 18, 8, 4)
        self.assertTrue(result["water_at_center"])
        self.assertTrue(result["segments"])

    def test_area_summary_aggregates(self):
        result = topography.area_summary(self.store, 8, 8, 6)
        self.assertGreater(result["columns"], 0)
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["dominant_biome"], "minecraft:plains")
        self.assertEqual(result["surface"]["min_y"], 64)
        self.assertEqual(result["surface"]["max_y"], 64)
        self.assertEqual(result["surface"]["water_fraction"], 0.0)
        self.assertIn("natural", result["composition"])

    def test_area_summary_counts_water(self):
        result = topography.area_summary(self.store, 20, 8, 4)
        self.assertGreater(result["surface"]["water_fraction"], 0.0)

    def test_recall_returns_chunk_summary(self):
        result = topography.recall(self.store, 10, 8)
        self.assertIsNotNone(result)
        self.assertEqual(result["chunk"], {"cx": 0, "cz": 0})
        self.assertEqual(result["column"]["y"], 64)
        self.assertEqual(result["column"]["material"], "minecraft:grass_block")
        self.assertEqual(result["surface"]["min_y"], 64)
        self.assertEqual(result["surface"]["water_columns"], 0)

    def test_recall_unknown_chunk(self):
        self.assertIsNone(topography.recall(self.store, 5000, 5000))


if __name__ == "__main__":
    unittest.main()
