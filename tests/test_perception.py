"""Perception LOD summary tests.

JSON budget note: the synthetic full 16x16 fixture below serialises to about
970 characters of compact JSON without per-cell materials (~240 tokens at the
usual ~4 characters/token estimate) and about 2.2 kB with materials (~540
tokens).  The Mindcraft prompt formatter compresses this into its
character-budgeted text block; the JSON measurement is the upper bound.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import loader
import map_store
import perception
from tests import helpers

GRASS = "minecraft:grass_block"
WATER = "minecraft:water"


def expand_chunks(cells):
    chunks = set()
    for cx, cz, cell_chunks in cells:
        for dx in range(cell_chunks):
            for dz in range(cell_chunks):
                chunks.add((cx + dx, cz + dz))
    return chunks


class LodLevelTests(unittest.TestCase):
    def test_16x16_has_three_levels(self):
        levels = perception.lod_levels(-8, -8, 16)
        self.assertEqual([level["cell_chunks"] for level in levels], [1, 2, 4])
        self.assertEqual([level["cells_per_side"] for level in levels], [4, 4, 4])
        self.assertEqual([(level["cx1"], level["cz1"]) for level in levels],
                         [(-2, -2), (-4, -4), (-8, -8)])

    def test_32x32_has_four_levels(self):
        levels = perception.lod_levels(-16, -16, 32)
        self.assertEqual([level["cell_chunks"] for level in levels], [1, 2, 4, 8])
        self.assertEqual([(level["cx1"], level["cz1"]) for level in levels],
                         [(-2, -2), (-4, -4), (-8, -8), (-16, -16)])

    def test_16x16_ring_coverage(self):
        cells = perception.unique_level_cells(perception.lod_levels(-8, -8, 16))
        self.assertEqual(len(cells), 40)
        expanded = expand_chunks(cells)
        self.assertEqual(len(expanded), 256)
        self.assertEqual(expanded, {(x, z) for x in range(-8, 8) for z in range(-8, 8)})

    def test_32x32_ring_coverage(self):
        cells = perception.unique_level_cells(perception.lod_levels(-16, -16, 32))
        self.assertEqual(len(cells), 52)
        expanded = expand_chunks(cells)
        self.assertEqual(len(expanded), 1024)
        self.assertEqual(
            expanded, {(x, z) for x in range(-16, 16) for z in range(-16, 16)}
        )

    def test_unique_cells_have_no_duplicates(self):
        for size, start in ((16, -8), (32, -16)):
            cells = perception.unique_level_cells(perception.lod_levels(start, start, size))
            self.assertEqual(len(cells), len(set(cells)))


class SyntheticPerceptionTests(unittest.TestCase):
    SIZE = 16
    MISSING = {(7, 7)}
    LOW_CHUNK = (-1, -1)
    WATER_CHUNKS = {(2, 0), (3, 0), (2, 1), (3, 1)}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        records = {}
        half = cls.SIZE // 2
        for cx in range(-half, half):
            for cz in range(-half, half):
                if (cx, cz) in cls.MISSING:
                    continue
                if (cx, cz) == cls.LOW_CHUNK:
                    record = helpers.make_record(cx, cz, height=50)
                elif (cx, cz) in cls.WATER_CHUNKS:
                    record = helpers.make_record(
                        cx, cz, height=40, water_level=63, surface=helpers.SAND
                    )
                else:
                    record = helpers.make_record(cx, cz, height=64)
                records[(cx, cz)] = record

        cls.loader = helpers.FakeLoader(records)
        cls.store = map_store.MapStore(
            str(Path(cls._tmp.name) / "map_store.db"), cls.loader
        )
        cls.data = perception.build_perception(cls.store, 0, 0, cls.SIZE)

    @classmethod
    def tearDownClass(cls):
        cls.store.stop()
        cls._tmp.cleanup()

    def test_window_bounds(self):
        self.assertEqual(
            self.data["window"],
            {"cx1": -8, "cz1": -8, "cx2": 7, "cz2": 7},
        )
        self.assertEqual(self.data["center"], {"x": 0, "z": 0})
        self.assertEqual(self.data["size_chunks"], 16)
        self.assertEqual(self.data["resolution"], "lod")

    def test_lod_levels_and_lengths(self):
        levels = self.data["lod"]
        self.assertEqual([level["cell_chunks"] for level in levels], [1, 2, 4])
        for level in levels:
            self.assertEqual(level["cells_per_side"], 4)
            self.assertEqual(len(level["heights"]), 16)
            self.assertEqual(len(level["water"]), 16)

    def test_heights(self):
        inner = self.data["lod"][0]["heights"]
        self.assertEqual(inner[5], 50)
        self.assertEqual(inner[0], 64)
        self.assertEqual(inner[15], 64)
        self.assertEqual(self.data["surface"]["min_y"], 50)
        self.assertEqual(self.data["surface"]["max_y"], 64)
        self.assertEqual(self.data["surface"]["relief"], 14)
        self.assertIsNotNone(self.data["surface"]["avg_y"])

    def test_water_flags(self):
        self.assertEqual(self.data["lod"][0]["water"], [0] * 16)
        self.assertEqual(self.data["lod"][1]["water"][11], 1)
        self.assertEqual(self.data["lod"][1]["heights"][11], 63)

    def test_nearest_water(self):
        water = self.data["water"]
        self.assertFalse(water["at_center"])
        self.assertEqual(water["nearest"]["dx"], 32)
        self.assertEqual(water["nearest"]["dz"], 0)
        self.assertEqual(water["nearest"]["distance"], 32)
        self.assertEqual(water["nearest"]["direction"], "east")

    def test_composition_and_biome(self):
        composition = self.data["composition"]
        self.assertGreater(composition["natural"], 0)
        self.assertEqual(composition["liquid"], 4 * 4096)
        self.assertEqual(composition["engineered"], 0)
        self.assertEqual(composition["artificiality_score"], 0.0)
        self.assertGreater(composition["water_fraction"], 0)
        self.assertLessEqual(len(composition["top_blocks"]), 6)
        self.assertEqual(composition["top_blocks"][0][0], GRASS)
        self.assertEqual(self.data["biome"], "minecraft:plains")

    def test_missing_chunks(self):
        self.assertEqual(self.data["missing_chunks"], 1)

    def test_materials_optional(self):
        without = perception.build_perception(self.store, 0, 0, self.SIZE, materials=False)
        with_materials = perception.build_perception(
            self.store, 0, 0, self.SIZE, materials=True
        )
        for level in without["lod"]:
            self.assertNotIn("materials", level)
        for level in with_materials["lod"]:
            self.assertEqual(len(level["materials"]), 16)
        self.assertEqual(with_materials["lod"][0]["materials"][5], GRASS)
        self.assertEqual(with_materials["lod"][1]["materials"][11], WATER)

    def test_json_size_budget(self):
        compact = json.dumps(self.data, separators=(",", ":"))
        self.assertLess(len(compact), 4000)
        self.assertGreater(len(compact), 0)


class CoastPerceptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        world = helpers.build_coast_world(cls._tmp.name)
        cls.loader = loader.ChunkLoader(world)
        cls.store = map_store.MapStore(
            str(Path(cls._tmp.name) / "map_store.db"), cls.loader
        )
        cls.data = perception.build_perception(cls.store, 8, 8, 4)

    @classmethod
    def tearDownClass(cls):
        cls.store.stop()
        cls.loader.close()
        cls._tmp.cleanup()

    def test_real_world_window_and_missing(self):
        self.assertEqual(
            self.data["window"],
            {"cx1": -2, "cz1": -2, "cx2": 1, "cz2": 1},
        )
        self.assertEqual(len(self.data["lod"]), 1)
        self.assertEqual(self.data["missing_chunks"], 12)

    def test_real_world_heights_and_nulls(self):
        heights = self.data["lod"][0]["heights"]
        self.assertIsNone(heights[0])
        self.assertIsNotNone(heights[10])
        self.assertEqual(heights[11], 63)
        self.assertIsNotNone(heights[14])
        self.assertEqual(heights[15], 63)

    def test_real_world_water_and_nearest(self):
        water_flags = self.data["lod"][0]["water"]
        self.assertEqual(water_flags[10], 0)
        self.assertEqual(water_flags[11], 1)
        self.assertEqual(water_flags[14], 0)
        self.assertEqual(water_flags[15], 1)

        water = self.data["water"]
        self.assertFalse(water["at_center"])
        self.assertEqual(water["nearest"]["dx"], 8)
        self.assertEqual(water["nearest"]["dz"], 0)
        self.assertEqual(water["nearest"]["distance"], 8)
        self.assertEqual(water["nearest"]["direction"], "east")

    def test_real_world_composition_and_biome(self):
        self.assertGreater(self.data["composition"]["natural"], 0)
        self.assertEqual(self.data["biome"], "minecraft:plains")

    def test_real_world_materials(self):
        data = perception.build_perception(self.store, 8, 8, 4, materials=True)
        materials = data["lod"][0]["materials"]
        self.assertEqual(materials[10], GRASS)
        self.assertEqual(materials[11], WATER)


if __name__ == "__main__":
    unittest.main()
