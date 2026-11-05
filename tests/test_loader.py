"""Base-state loader tests: NBT decoding, surfaces, water, and region files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import loader
from tests import helpers


class DecodeSurfaceTests(unittest.TestCase):
    def test_uniform_stone_section(self):
        root = {
            "sections": [
                {
                    "Y": 3,
                    "block_states": {"palette": [{"Name": "minecraft:stone"}]},
                }
            ]
        }
        surface = loader.decode_surface(root)
        self.assertEqual(set(surface["y"]), {63})
        self.assertEqual(set(surface["materials"]), {"minecraft:stone"})
        self.assertEqual(set(surface["ground_y"]), {63})
        self.assertFalse(any(surface["water"]))

    def test_water_column_tracks_surface_and_ground(self):
        root = {
            "sections": [
                {
                    "Y": 3,
                    "block_states": {
                        "palette": [
                            {"Name": "minecraft:air"},
                            {"Name": "minecraft:sand"},
                            {"Name": "minecraft:water"},
                        ],
                        "data": helpers.pack_indices(
                            [0] * 256 * 12 + [1] * 256 + [2] * 256 * 3,
                            4,
                        ),
                    },
                }
            ]
        }
        surface = loader.decode_surface(root)
        self.assertEqual(set(surface["y"]), {63})
        self.assertEqual(set(surface["materials"]), {"minecraft:water"})
        self.assertEqual(set(surface["ground_y"]), {60})
        self.assertEqual(set(surface["ground_materials"]), {"minecraft:sand"})
        self.assertTrue(all(surface["water"]))

    def test_air_only_chunk_has_no_surface(self):
        root = {"sections": [{"Y": 4, "block_states": {"palette": [{"Name": "minecraft:air"}]}}]}
        surface = loader.decode_surface(root)
        self.assertEqual(set(surface["y"]), {None})
        self.assertFalse(any(surface["water"]))


class ChunkLoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = helpers.build_coast_world(self._tmp.name)
        self.loader = loader.ChunkLoader(self.world)
        self.addCleanup(self.loader.close)

    def test_world_is_discoverable(self):
        self.assertTrue(self.loader.available)
        self.assertEqual(self.loader.region_count, 1)

    def test_land_chunk_surface(self):
        record = self.loader.load_chunk(0, 0)
        self.assertIsNotNone(record)
        self.assertEqual((record["cx"], record["cz"]), (0, 0))
        surface = record["surface"]
        self.assertEqual(set(surface["materials"]), {"minecraft:grass_block"})
        self.assertTrue(55 <= surface["y"][0] <= 66)
        self.assertFalse(any(surface["water"]))

    def test_water_chunk_is_flagged(self):
        record = self.loader.load_chunk(1, 0)
        self.assertIsNotNone(record)
        surface = record["surface"]
        self.assertEqual(set(surface["materials"]), {"minecraft:water"})
        self.assertEqual(set(surface["y"]), {63})
        self.assertEqual(set(surface["ground_y"]), {60})
        self.assertTrue(all(surface["water"]))

    def test_missing_chunk_returns_none(self):
        self.assertIsNone(self.loader.load_chunk(50, 50))

    def test_composition_includes_biome(self):
        record = self.loader.load_chunk(0, 0)
        composition = record["composition"]
        self.assertEqual(composition["dominant_biome"], "minecraft:plains")
        self.assertGreater(sum(composition["counts"].values()), 0)

    def test_iter_all_chunks_yields_every_chunk(self):
        chunks = {(record["cx"], record["cz"]) for record in self.loader.iter_all_chunks()}
        self.assertEqual(chunks, {(0, 0), (1, 0), (0, 1), (1, 1)})


if __name__ == "__main__":
    unittest.main()
