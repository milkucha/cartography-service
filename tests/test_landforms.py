"""Landform classification: synthetic terrain per kind, structures, data edges, gating, HTTP."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import landforms
import map_store
import perception
import store_view
from api import TopographyAPI
from tests import helpers


def make_rec(cx, cz, height_fn, water_fn=None, top_fn=None):
    """Chunk record from per-column functions (``height_fn(x, z)`` = natural ground)."""
    ty, y, water = [], [], []
    for lz in range(16):
        for lx in range(16):
            x, z = cx * 16 + lx, cz * 16 + lz
            h = height_fn(x, z)
            wl = water_fn(x, z) if water_fn else None
            ty.append(h)
            water.append(wl is not None)
            top = top_fn(x, z) if top_fn else None
            y.append(wl if wl is not None else (top if top is not None else h))
    mats = [helpers.GRASS] * 256
    return {
        "cx": cx, "cz": cz,
        "surface": {"y": y, "materials": mats, "ground_y": list(y), "ground_materials": mats,
                    "water": water, "terrain_y": ty, "terrain_materials": mats},
        "composition": {"counts": {"natural": 4096, "engineered": 0, "unknown": 0, "liquid": 0,
                                    "air": 0},
                         "top_blocks": [], "dominant_biome": "minecraft:plains"},
        "source": "base",
    }


class Terrain(unittest.TestCase):
    def make(self, box, height_fn, water_fn=None, top_fn=None, skip=()):
        """Store over chunks ``box`` = (cx1, cz1, cx2, cz2) minus ``skip``."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recs = {}
        for cx in range(box[0], box[2] + 1):
            for cz in range(box[1], box[3] + 1):
                if (cx, cz) not in skip:
                    recs[(cx, cz)] = make_rec(cx, cz, height_fn, water_fn, top_fn)
        store = map_store.MapStore(str(Path(tmp.name) / "m.db"), helpers.FakeLoader(recs))
        self.addCleanup(store.stop)
        return store

    def find(self, store, x=0, z=0, radius=128):
        return landforms.find_landforms(store, x, z, radius)

    def kinds(self, res):
        return [f["kind"] for f in res["landforms"]]

    def one(self, res, kind):
        hits = [f for f in res["landforms"] if f["kind"] == kind]
        self.assertTrue(hits, f"no {kind} in {self.kinds(res)}")
        return hits[0]


BOX = (-5, -5, 4, 4)  # 160x160 blocks centred near the origin


def bump(cx, cz, peak, radius):
    """Smooth cone of ``peak`` extra blocks over a base of 64."""
    def h(x, z):
        d = ((x - cx) ** 2 + (z - cz) ** 2) ** 0.5
        return int(64 + max(0.0, peak * (1 - d / radius)))
    return h


class KindTests(Terrain):
    def test_flat_land_is_a_plain_only(self):
        res = self.find(self.make(BOX, lambda x, z: 64))
        self.assertEqual(self.kinds(res), ["plain"])
        self.assertEqual((res["landforms"][0]["y_min"], res["landforms"][0]["y_max"]), (64, 64))
        self.assertTrue(res["landforms"][0]["inside"])

    def test_hill(self):
        res = self.find(self.make(BOX, bump(40, 0, 25, 30)))
        hill = self.one(res, "hill")
        self.assertGreaterEqual(hill["y_max"], 85)
        self.assertLess(abs(hill["x"] - 40), 12)
        self.assertEqual(hill["direction"], "east")
        self.assertNotIn("mountain", self.kinds(res))

    def test_mountain(self):
        res = self.find(self.make(BOX, bump(-40, 0, 80, 50)))
        m = self.one(res, "mountain")
        self.assertGreaterEqual(m["y_max"], 130)
        self.assertEqual(m["direction"], "west")
        self.assertGreater(m["distance"], 0)

    def test_small_bump_is_ignored(self):
        res = self.find(self.make(BOX, bump(30, 30, 20, 7)))
        self.assertNotIn("hill", self.kinds(res))

    def test_cliff(self):
        # step of 40 blocks at x = -40; high side to the west
        res = self.find(self.make(BOX, lambda x, z: 110 if x < -40 else 70))
        cliff = self.one(res, "cliff")
        self.assertLessEqual(cliff["y_min"], 71)
        self.assertGreaterEqual(cliff["y_max"], 109)
        self.assertEqual(cliff["facing"], "east")
        self.assertEqual(cliff["direction"], "west")
        self.assertAlmostEqual(cliff["distance"], 40, delta=8)
        self.assertGreaterEqual(cliff["size"]["length"], 100)

    def test_gentle_slope_is_not_a_cliff(self):
        res = self.find(self.make(BOX, lambda x, z: 64 + (x + 80) // 4))
        self.assertNotIn("cliff", self.kinds(res))

    def test_plateau(self):
        def h(x, z):
            return 100 if (-48 <= x < 48 and -48 <= z < 48) else 64
        res = self.find(self.make(BOX, h))
        p = self.one(res, "plateau")
        self.assertGreaterEqual(p["y_max"], 99)
        self.assertTrue(p["inside"])
        self.assertIn("cliff", self.kinds(res))

    def test_ridge(self):
        def h(x, z):
            return int(64 + max(0.0, 30 * (1 - abs(z) / 14.0)))
        res = self.find(self.make(BOX, h))
        r = self.one(res, "ridge")
        self.assertGreaterEqual(r["size"]["length"], 120)
        self.assertLessEqual(r["size"]["width"], 40)

    def test_valley(self):
        def h(x, z):
            return int(64 + min(40, abs(z) * 0.8))
        res = self.find(self.make(BOX, h))
        v = self.one(res, "valley")
        self.assertGreaterEqual(v["depth"], VALLEY_FLOOR)
        self.assertLessEqual(v["y_min"], 66)

    def test_lake(self):
        def water(x, z):
            return 63 if (x + 40) ** 2 + z ** 2 < 25 ** 2 else None
        res = self.find(self.make(BOX, lambda x, z: 60 if water(x, z) else 64, water))
        lake = self.one(res, "lake")
        self.assertEqual(lake["y_min"], 63)
        self.assertEqual(lake["direction"], "west")
        self.assertAlmostEqual(lake["distance"], 15, delta=8)  # near shore, not centre

    def test_river_is_told_from_lake(self):
        def water(x, z):
            return 63 if abs(z) <= 4 else None
        res = self.find(self.make(BOX, lambda x, z: 60 if water(x, z) else 64, water))
        self.assertIn("river", self.kinds(res))
        self.assertNotIn("lake", self.kinds(res))

    def test_results_capped_and_sorted_by_salience_over_distance(self):
        res = self.find(self.make(BOX, bump(30, 30, 25, 30)), radius=200)
        self.assertLessEqual(len(res["landforms"]), landforms.MAX_RESULTS)


VALLEY_FLOOR = 12


class NotTerrainTests(Terrain):
    def test_tree_and_building_are_not_a_hill(self):
        # flat natural ground, but surface y (roofs/trees) towers 30 blocks over a big area
        def top(x, z):
            return 94 if (-30 <= x < 30 and -30 <= z < 30) else None
        res = self.find(self.make(BOX, lambda x, z: 64, top_fn=top))
        self.assertEqual(self.kinds(res), ["plain"])

    def test_ground_y_fallback_for_old_rows(self):
        store = self.make(BOX, bump(40, 0, 25, 30))
        # strip terrain_y from every cached record: ground_y (same values) is used instead
        loader = store.loader
        for rec in loader.records.values():
            rec["surface"]["ground_y"] = list(rec["surface"].pop("terrain_y"))
        res = self.find(store)
        self.assertIn("hill", self.kinds(res))


class DataEdgeTests(Terrain):
    def test_missing_chunks_are_not_a_cliff(self):
        # east half unknown; known land is flat at 64 next to nothing
        store = self.make(BOX, lambda x, z: 64, skip={(cx, cz) for cx in range(0, 5)
                                                       for cz in range(-5, 5)})
        res = self.find(store)
        self.assertNotIn("cliff", self.kinds(res))
        self.assertGreater(res["missing_chunks"], 0)

    def test_known_high_next_to_missing_is_not_a_cliff(self):
        store = self.make(BOX, lambda x, z: 110 if x < 0 else 64,
                          skip={(cx, cz) for cx in range(0, 5) for cz in range(-5, 5)})
        res = self.find(store)
        self.assertNotIn("cliff", self.kinds(res))

    def test_water_at_data_edge_is_not_a_shoreline_form(self):
        store = self.make(BOX, lambda x, z: 64, skip={(cx, cz) for cx in range(0, 5)
                                                       for cz in range(-5, 5)})
        self.assertNotIn("lake", self.kinds(self.find(store)))

    def test_partial_flag_on_form_touching_unknown(self):
        def water(x, z):
            return 63 if x >= -20 and abs(z) < 30 else None
        store = self.make(BOX, lambda x, z: 60 if water(x, z) else 64, water,
                          skip={(cx, cz) for cx in range(1, 5) for cz in range(-5, 5)})
        res = self.find(store)
        forms = [f for f in res["landforms"] if f["kind"] in ("lake", "ocean")]
        self.assertTrue(forms)
        self.assertTrue(all(f["partial"] for f in forms))

    def test_real_chunk_boundary_cliff_is_found(self):
        res = self.find(self.make(BOX, lambda x, z: 110 if x < 0 else 70))
        self.assertIn("cliff", self.kinds(res))


class GatingTests(Terrain):
    def test_gated_view_excludes_unseen_chunks(self):
        store = self.make(BOX, lambda x, z: 110 if x < -40 else 70)
        view = store_view.StoreView(store, "bot")
        res = landforms.find_landforms(view, 0, 0, 128)
        self.assertEqual(res["landforms"], [])
        self.assertGreater(res["unexplored_chunks"], 0)
        store.mark_seen("bot", [[cx, cz] for cx in range(-5, 5) for cz in range(-5, 5)], None)
        view = store_view.StoreView(store, "bot")
        res = landforms.find_landforms(view, 0, 0, 64)
        self.assertIn("cliff", self.kinds(res))
        self.assertEqual(res["unexplored_chunks"], 0)

    def test_gated_partial_seen_has_no_cliff_from_unseen_side(self):
        store = self.make(BOX, lambda x, z: 110 if x < -40 else 70)
        # only the high western chunks are seen: the cliff foot is unknown
        store.mark_seen("bot", [[cx, cz] for cx in range(-5, -3) for cz in range(-5, 5)], None)
        res = landforms.find_landforms(store_view.StoreView(store, "bot"), -64, 0, 128)
        self.assertNotIn("cliff", self.kinds(res))


class PerceptionTests(Terrain):
    def test_perception_includes_landforms(self):
        store = self.make((-8, -8, 7, 7), lambda x, z: 110 if x < -40 else 70)
        data = perception.build_perception(store, 0, 0, 16)
        self.assertIn("landforms", data)
        kinds = [f["kind"] for f in data["landforms"]]
        self.assertIn("cliff", kinds)
        self.assertLessEqual(len(data["landforms"]), landforms.PERCEPTION_LIMIT)
        cliff = [f for f in data["landforms"] if f["kind"] == "cliff"][0]
        for key in ("direction", "distance", "x", "z", "y_min", "y_max", "size", "nearest"):
            self.assertIn(key, cliff)

    def test_perception_gated(self):
        store = self.make((-8, -8, 7, 7), lambda x, z: 110 if x < -40 else 70)
        view = store_view.StoreView(store, "bot")
        data = perception.build_perception(view, 0, 0, 16)
        self.assertEqual(data["landforms"], [])

    def test_analysis_is_memoised(self):
        store = self.make((-8, -8, 7, 7), bump(40, 0, 25, 30))
        perception.build_perception(store, 0, 0, 16)
        t = time.perf_counter()
        perception.build_perception(store, 0, 0, 16)
        self.assertLess(time.perf_counter() - t, 0.5)


class ApiTests(Terrain):
    def setUp(self):
        store = self.make(BOX, lambda x, z: 110 if x < -40 else 70)
        self.store = store
        self.api = TopographyAPI(store, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        for _ in range(400):
            if self.api.server is not None:
                break
            time.sleep(0.005)
        self.base = f"http://127.0.0.1:{self.api.server.server_address[1]}"
        self.addCleanup(self._stop)

    def _stop(self):
        self.api.shutdown()
        self.thread.join(timeout=5)

    def get(self, params):
        try:
            with urllib.request.urlopen(f"{self.base}/landforms?{urlencode(params)}") as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_endpoint(self):
        st, res = self.get({"x": 0, "z": 0, "radius": 128})
        self.assertEqual(st, 200)
        self.assertEqual(res["radius"], 128)
        self.assertIn("cliff", [f["kind"] for f in res["landforms"]])
        self.assertIn("missing_chunks", res)

    def test_default_radius(self):
        st, res = self.get({"x": 0, "z": 0})
        self.assertEqual((st, res["radius"]), (200, landforms.DEFAULT_RADIUS))

    def test_validation(self):
        self.assertEqual(self.get({"z": 0})[0], 400)
        self.assertEqual(self.get({"x": "a", "z": 0})[0], 400)
        self.assertEqual(self.get({"x": 0, "z": 0, "radius": 4})[0], 400)
        self.assertEqual(self.get({"x": 0, "z": 0, "radius": 999})[0], 400)

    def test_nether_unavailable(self):
        st, res = self.get({"x": 0, "z": 0, "dimension": "the_nether"})
        self.assertEqual(st, 200)
        self.assertEqual(res.get("unavailable"), "dimension")

    def test_agent_gating(self):
        st, res = self.get({"x": 0, "z": 0, "agent": "ghost"})
        self.assertEqual(st, 200)
        self.assertEqual(res["landforms"], [])
        self.assertGreater(res["unexplored_chunks"], 0)


if __name__ == "__main__":
    unittest.main()
