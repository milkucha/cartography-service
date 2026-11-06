"""Leaf-free terrain, flat_areas, bridge_span, and block-list aggregation tests."""

from __future__ import annotations

import json
import random
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import loader
import map_store
import perception
import terrain_queries
import terrain_rules
import topography
from api import TopographyAPI
from tests import helpers

PLANKS = "minecraft:oak_planks"
RAIL = "minecraft:rail"
FLAT_Y = 70
RIVER_X = range(20, 27)


def ground(x, z):
    if z >= 32:
        return 61 if x in RIVER_X else 66
    if x < 32:
        return FLAT_Y
    return 60 + (x - 32)  # slope: +1 per block


def water_level(x, z):
    return 65 if (z >= 32 and x in RIVER_X) else None


def extras(x, z):
    out = {}
    if (x, z) == (10, 10):
        out.update({71: helpers.LOG, 72: helpers.LOG, 73: helpers.LOG, 74: helpers.LOG})
    if abs(x - 10) <= 2 and abs(z - 10) <= 2 and (x, z) != (10, 10):
        out.update({73: helpers.LEAVES, 74: helpers.LEAVES})
    if abs(x - 10) <= 1 and abs(z - 10) <= 1 and (x, z) != (10, 10):
        out[75] = helpers.LEAVES
    if x in (28, 29) and z in (28, 29):
        out[71] = PLANKS
    if z == 5 and x in (36, 37, 38):
        out[ground(x, z) + 1] = RAIL
    return out


def surface(x, z):
    return helpers.SAND if (z >= 32 and x in RIVER_X) else helpers.GRASS


def build_world():
    tmp = tempfile.TemporaryDirectory()
    region = Path(tmp.name) / "world" / "region"
    chunks = {}
    for cx in range(4):
        for cz in range(4):
            chunks[(cx, cz)] = helpers.build_chunk_root(
                cx, cz, ground, surface, water_level, extras_fn=extras
            )
    helpers.write_region(region / "r.0.0.mca", chunks)
    ldr = loader.ChunkLoader(Path(tmp.name) / "world")
    store = map_store.MapStore(str(Path(tmp.name) / "map_store.db"), ldr)
    return tmp, ldr, store


class TerrainBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp, cls.loader, cls.store = build_world()

    @classmethod
    def tearDownClass(cls):
        cls.store.stop()
        cls.loader.close()
        cls._tmp.cleanup()


class LeafFreeTerrainTests(TerrainBase):
    def col(self, x, z):
        data = self.store.get_or_load(x >> 4, z >> 4)
        idx = (x & 15) + (z & 15) * 16
        s = data["surface"]
        return {k: s[k][idx] for k in s}

    def test_canopy_columns_use_floor(self):
        col = self.col(8, 8)  # leaves only, ground below
        self.assertEqual(col["y"], 74)
        self.assertEqual(col["ground_y"], 74)  # legacy value includes leaves
        self.assertEqual(col["terrain_y"], FLAT_Y)
        self.assertEqual(col["terrain_materials"], helpers.GRASS)

    def test_trunk_is_not_ground(self):
        col = self.col(10, 10)  # logs 71..74 on grass at 70
        self.assertEqual(col["terrain_y"], FLAT_Y)
        self.assertEqual(col["terrain_materials"], helpers.GRASS)

    def test_water_bed(self):
        col = self.col(22, 40)
        self.assertTrue(col["water"])
        self.assertEqual(col["terrain_y"], 61)
        self.assertEqual(col["terrain_materials"], helpers.SAND)

    def test_decode_matches_block_scan(self):
        rng = random.Random(7)
        names = ["minecraft:air", "minecraft:stone", "minecraft:oak_leaves",
                 "minecraft:water", "minecraft:short_grass", helpers.LOG,
                 "minecraft:snow", "minecraft:sand"]
        for trial in range(6):
            sections = []
            blocks = {}
            for sy in (0, 1, 2):
                idx = [rng.choice(names[: 3 + trial]) if rng.random() < 0.7 else names[0]
                       for _ in range(4096)]
                for i, n in enumerate(idx):
                    blocks[(sy * 16 + i // 256, i % 256)] = n
                pal = sorted(set(idx))
                if len(pal) == 1:
                    bs = {"palette": [{"Name": pal[0]}]}
                else:
                    bits = max(4, (len(pal) - 1).bit_length())
                    bs = {"palette": [{"Name": n} for n in pal],
                          "data": helpers.pack_indices([pal.index(n) for n in idx], bits)}
                sections.append({"Y": sy, "block_states": bs})
            result = loader.decode_surface({"sections": sections})
            for col in range(256):
                expect = (None, None)
                for y in range(47, -1, -1):
                    if terrain_rules.is_ground(blocks[(y, col)]):
                        expect = (y, blocks[(y, col)])
                        break
                got = (result["terrain_y"][col], result["terrain_materials"][col])
                self.assertEqual(got, expect)

    def test_empty_surface_has_terrain_arrays(self):
        s = loader._empty_surface()
        self.assertEqual(len(s["terrain_y"]), 256)
        self.assertEqual(len(s["terrain_materials"]), 256)


class PerceptionTerrainTests(unittest.TestCase):
    def test_highest_uses_terrain_y(self):
        rec = helpers.make_record(0, 0, height=90, terrain_height=70)
        summary = perception._terrain_summary({(0, 0): rec}, 8, 8)
        self.assertEqual(summary["highest"]["y"], 70)
        self.assertEqual(summary["relief"], 0)

    def test_old_rows_fall_back_to_ground_y(self):
        rec = helpers.make_record(0, 0, height=90)
        del rec["surface"]["terrain_y"], rec["surface"]["terrain_materials"]
        summary = perception._terrain_summary({(0, 0): rec}, 8, 8)
        self.assertEqual(summary["highest"]["y"], 90)

    def test_pruned_summary_matches_bruteforce(self):
        rng = random.Random(3)
        records = {}
        for cx in range(-3, 4):
            for cz in range(-3, 4):
                rec = helpers.make_record(cx, cz)
                rec["surface"]["terrain_y"] = [rng.randint(50, 80) for _ in range(256)]
                rec["surface"]["water"] = [rng.random() < 0.1 for _ in range(256)]
                records[(cx, cz)] = rec
        px, pz = 5, -9
        got = perception._terrain_summary(records, px, pz)
        best = {}
        hi = lo = None
        for (cx, cz), rec in records.items():
            for i, y in enumerate(rec["surface"]["terrain_y"]):
                if rec["surface"]["water"][i]:
                    continue
                dx, dz = cx * 16 + i % 16 - px, cz * 16 + i // 16 - pz
                d = perception._direction(dx, dz)
                best[d] = max(best.get(d, -999), y)
                hi = y if hi is None else max(hi, y)
                lo = y if lo is None else min(lo, y)
        self.assertEqual(got["highest"]["y"], hi)
        self.assertEqual(got["lowest"]["y"], lo)
        self.assertEqual({k: v["y"] for k, v in got["highest_by_direction"].items()}, best)


class CompositionTests(TerrainBase):
    def test_air_not_in_top_blocks(self):
        data = self.store.get_or_load(0, 0)
        names = [n for n, _ in data["composition"]["top_blocks"]]
        self.assertNotIn("minecraft:air", names)
        summary = topography.area_summary(self.store, 8, 8, 10)
        self.assertNotIn("minecraft:air", [n for n, _ in summary["top_blocks"]])
        comp, _ = perception._composition_summary(
            {(0, 0): data, (1, 0): self.store.get_or_load(1, 0)}
        )
        self.assertNotIn("minecraft:air", [n for n, _ in comp["top_blocks"]])

    def test_small_structure_surfaces_in_engineered_blocks(self):
        data = self.store.get_or_load(1, 1)  # planks at (28..29, 28..29)
        engineered = dict((n, c) for n, c in data["composition"]["engineered_blocks"])
        self.assertEqual(engineered.get(PLANKS), 4)
        summary = topography.area_summary(self.store, 28, 28, 8)
        self.assertIn(PLANKS, [n for n, _ in summary["engineered_blocks"]])

    def test_old_rows_without_engineered_list(self):
        comp = {"top_blocks": [[PLANKS, 3], ["minecraft:air", 99]]}
        top, eng = Counter(), Counter()
        perception.accumulate_blocks(comp, top, eng)
        self.assertEqual(dict(top), {PLANKS: 3})
        self.assertEqual(dict(eng), {PLANKS: 3})

    def test_rail_columns_kept(self):
        data = self.store.get_or_load(2, 0)
        self.assertIn([36, 5], data["composition"]["rail_columns"])
        self.assertEqual(data["composition"]["rail_column_count"], 3)


class FlatAreasTests(TerrainBase):
    def test_finds_plateau_nearest_first(self):
        result = terrain_queries.flat_areas(self.store, 22, 22, radius=20)
        areas = result["areas"]
        self.assertTrue(areas)
        self.assertLessEqual(len(areas), 5)
        first = areas[0]
        self.assertEqual((first["y_min"], first["y_max"], first["center_y"]), (70, 70, 70))
        self.assertEqual(first["x2"] - first["x1"], 8)
        self.assertEqual(first["z2"] - first["z1"], 8)
        self.assertEqual(first["floor_material"], helpers.GRASS)
        self.assertEqual(first["water_columns"], 0)
        self.assertLessEqual(first["distance"], 3.0)
        self.assertEqual(result["missing_chunks"], 0)
        for i, a in enumerate(areas):
            for b in areas[i + 1:]:
                self.assertTrue(a["x2"] < b["x1"] or b["x2"] < a["x1"]
                                or a["z2"] < b["z1"] or b["z2"] < a["z1"])

    def test_rejects_tree_and_buildings(self):
        result = terrain_queries.flat_areas(self.store, 14, 14, radius=24, width=5, depth=5)
        self.assertTrue(result["areas"])
        for a in result["areas"]:
            self.assertFalse(a["x1"] <= 10 <= a["x2"] and a["z1"] <= 10 <= a["z2"])
            self.assertFalse(a["x1"] <= 28 <= a["x2"] and a["z1"] <= 28 <= a["z2"])

    def test_rejects_slope_until_delta_allowed(self):
        strict = terrain_queries.flat_areas(self.store, 48, 16, radius=10)
        self.assertEqual(strict["areas"], [])
        loose = terrain_queries.flat_areas(self.store, 48, 16, radius=10, max_delta=8)
        self.assertTrue(loose["areas"])
        self.assertEqual(loose["areas"][0]["y_max"] - loose["areas"][0]["y_min"], 8)

    def test_rejects_water(self):
        result = terrain_queries.flat_areas(self.store, 23, 48, radius=3, width=5, depth=5)
        self.assertEqual(result["areas"], [])

    def test_rectangular_orientations(self):
        result = terrain_queries.flat_areas(self.store, 22, 22, radius=12, width=12, depth=3)
        sizes = {(a["x2"] - a["x1"] + 1, a["z2"] - a["z1"] + 1) for a in result["areas"]}
        self.assertTrue(sizes)
        self.assertTrue(sizes <= {(12, 3), (3, 12)})

    def test_unknown_columns_disqualify_and_count_missing(self):
        result = terrain_queries.flat_areas(self.store, 62, 8, radius=12, max_delta=64)
        self.assertGreater(result["missing_chunks"], 0)
        for a in result["areas"]:
            self.assertLessEqual(a["x2"], 63)

    def test_param_validation(self):
        with self.assertRaises(ValueError):
            terrain_queries.flat_areas(self.store, 0, 0, radius=500)
        with self.assertRaises(ValueError):
            terrain_queries.flat_areas(self.store, 0, 0, width=0)

    def test_sliding_window_helper(self):
        rng = random.Random(1)
        values = [rng.randint(0, 9) for _ in range(40)]
        lo, hi = terrain_queries._sliding(values, 5)
        for i in range(len(values) - 4):
            self.assertEqual(lo[i], min(values[i:i + 5]))
            self.assertEqual(hi[i], max(values[i:i + 5]))


class BridgeSpanTests(TerrainBase):
    def test_crossing_depth_and_deck(self):
        r = terrain_queries.bridge_span(self.store, 17, 48, direction="east")
        self.assertEqual(r["near_shore"]["x"], 19)
        self.assertEqual(r["near_shore"]["y"], 66)
        self.assertEqual(r["far_shore"]["x"], 27)
        self.assertEqual(r["span"], 7)
        self.assertEqual(r["water_y"], 65)
        self.assertEqual(r["depth"], {"min": 4, "max": 4, "avg": 4.0})
        self.assertEqual(r["bed_y"], {"min": 61, "max": 61})
        self.assertEqual(r["longest_water_run"], 7)
        self.assertEqual(r["deck_y"], 67)
        self.assertEqual(r["caution"], [])
        self.assertEqual(len(r["water"]), 7)
        self.assertEqual(r["water"][0]["bed_y"], 61)

    def test_west_and_target_modes(self):
        west = terrain_queries.bridge_span(self.store, 30, 48, direction="west")
        self.assertEqual(west["far_shore"]["x"], 19)
        target = terrain_queries.bridge_span(self.store, 17, 48, target=(30, 48))
        self.assertEqual(target["span"], 7)
        self.assertEqual(target["far_shore"]["x"], 27)

    def test_no_water_and_missing(self):
        r = terrain_queries.bridge_span(self.store, 5, 40, direction="south")
        self.assertEqual(r["span"], 0)
        self.assertIn("no water found along this line", r["caution"])
        self.assertTrue(any("not loaded" in c for c in r["caution"]))

    def test_bad_direction(self):
        with self.assertRaises(ValueError):
            terrain_queries.bridge_span(self.store, 0, 0, direction="up")


class QueryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp, cls.loader, cls.store = build_world()
        cls.api = TopographyAPI(cls.store, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(400):
            if cls.api.server is not None:
                break
            time.sleep(0.005)
        cls.base = f"http://127.0.0.1:{cls.api.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.thread.join(timeout=5)
        cls.store.stop()
        cls.loader.close()
        cls._tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_flat_areas_endpoint(self):
        status, body = self.get("/flat_areas?x=22&z=22&radius=20&width=5&depth=5")
        self.assertEqual(status, 200)
        self.assertTrue(body["areas"])
        self.assertIn("floor_material", body["areas"][0])

    def test_flat_areas_bounds(self):
        self.assertEqual(self.get("/flat_areas?x=0&z=0&radius=129")[0], 400)
        self.assertEqual(self.get("/flat_areas?x=0&z=0&max_delta=-1")[0], 400)

    def test_bridge_span_endpoint(self):
        status, body = self.get("/bridge_span?x=17&z=48&dir=east")
        self.assertEqual(status, 200)
        self.assertEqual(body["span"], 7)
        status, body = self.get("/bridge_span?x=17&z=48&x2=30&z2=48")
        self.assertEqual(body["far_shore"]["x"], 27)
        self.assertEqual(self.get("/bridge_span?x=17&z=48&dir=sideways")[0], 400)

    def test_topography_reports_bed(self):
        status, body = self.get("/topography?x1=18&z1=48&x2=28&z2=48")
        water_runs = [r for r in body["runs"] if r.get("bed_min") is not None]
        self.assertEqual(water_runs[0]["bed_min"], 61)
        self.assertEqual(body["transitions"][0]["bed_y"], 61)
        self.assertEqual(body["points"][3]["terrain_y"], 61)


if __name__ == "__main__":
    unittest.main()
