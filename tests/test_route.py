"""``GET /route``: lake detours, bridge/path preference, piers, deck height, gating, bounds, HTTP."""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import map_store
import route
import store_view
from api import TopographyAPI
from tests import helpers

LAND = 64
WATER_Y = 63
BED = 55
PLANKS = "minecraft:oak_planks"
PATH = "minecraft:dirt_path"
GRASS = helpers.GRASS


class W:
    """A world: ``col(x, z) -> (y, material, terrain_y, terrain_material, water)``."""

    def __init__(self, col):
        self.col = col


def flat(x, z):
    return (LAND, GRASS, LAND, GRASS, False)


def with_lake(base, x1=-20, x2=20, z1=-30, z2=30):
    def col(x, z):
        if x1 <= x <= x2 and z1 <= z <= z2:
            return (WATER_Y, "minecraft:water", BED, "minecraft:sand", True)
        return base(x, z)
    return col


def with_deck(base, x1=-20, x2=20, zs=(-1, 0, 1), y=LAND + 1):
    def col(x, z):
        if x1 <= x <= x2 and z in zs:
            under = base(x, z)
            return (y, PLANKS, under[2], under[3], False)
        return base(x, z)
    return col


def make_rec(cx, cz, colfn):
    ys, mats, tys, tms, water = [], [], [], [], []
    for lz in range(16):
        for lx in range(16):
            y, m, ty, tm, w = colfn(cx * 16 + lx, cz * 16 + lz)
            ys.append(y); mats.append(m); tys.append(ty); tms.append(tm); water.append(w)
    return {
        "cx": cx, "cz": cz,
        "surface": {"y": ys, "materials": mats, "ground_y": list(tys), "ground_materials": tms,
                    "water": water, "terrain_y": tys, "terrain_materials": tms},
        "composition": {"counts": {"natural": 4096, "engineered": 0, "unknown": 0, "liquid": 0, "air": 0},
                        "top_blocks": [], "dominant_biome": "minecraft:plains"},
        "source": "base",
    }


class RouteCase(unittest.TestCase):
    BOX = (-10, -10, 10, 10)

    def make(self, colfn, box=None, seen=None, agent="bot"):
        box = box or self.BOX
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        recs = {(cx, cz): make_rec(cx, cz, colfn)
                for cx in range(box[0], box[2] + 1) for cz in range(box[1], box[3] + 1)}
        store = map_store.MapStore(str(Path(tmp.name) / "m.db"), helpers.FakeLoader(recs))
        self.addCleanup(store.stop)
        chunks = seen if seen is not None else list(recs)
        store.mark_seen(agent, [list(k) for k in chunks], None)
        self.colfn = colfn
        return store

    def view(self, store, agent="bot"):
        return store_view.StoreView(store, agent)

    def plan(self, store, a, b, seed=1, **kw):
        return route.route(self.view(store), a[0], a[1], b[0], b[1], rng=random.Random(seed), **kw)

    def kinds(self, res):
        return [w["kind"] for w in res["waypoints"]]

    def assert_dry_and_off_cliff(self, res):
        for w in res["waypoints"]:
            y, _m, ty, _tm, water = self.colfn(w["x"], w["z"])
            if w["kind"] in ("bridge_start", "bridge_deck", "bridge_end", "target"):
                continue
            self.assertFalse(water, f"waypoint {w} stands in water")
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = self.colfn(w["x"] + dx, w["z"] + dz)
                if not n[4]:
                    self.assertLessEqual(abs(n[2] - ty), 3, f"waypoint {w} on a cliff edge")


class LakeTests(RouteCase):
    def test_flat_land_goes_straight(self):
        res = self.plan(self.make(flat), (-100, 0), (100, 0))
        self.assertEqual(res["status"], "complete")
        self.assertEqual(res["waypoints"][-1]["kind"], "target")
        self.assertEqual((res["waypoints"][-1]["x"], res["waypoints"][-1]["z"]), (100, 0))
        self.assertEqual(res["waypoints"][-1]["y"], LAND)
        self.assertLess(res["distance"], 215)
        self.assertEqual(res["bridges"], [])

    def test_detour_around_lake(self):
        res = self.plan(self.make(with_lake(flat)), (-60, 0), (60, 0))
        self.assertEqual(res["status"], "complete")
        self.assertTrue(any(abs(w["z"]) > 30 for w in res["waypoints"]), res["waypoints"])
        self.assertEqual(res["detours"][0]["around"], "water")
        self.assertNotIn("swim", " ".join(w.get("note", "") for w in res["waypoints"]))
        self.assert_dry_and_off_cliff(res)
        self.assertGreater(res["distance"], 130)

    def test_target_in_lake_still_reports_it(self):
        res = self.plan(self.make(with_lake(flat)), (-60, 0), (0, 0))
        self.assertEqual(res["status"], "complete")
        self.assertEqual(res["target"]["y"], WATER_Y)


class BridgeTests(RouteCase):
    def test_bridge_preferred_over_detour_and_uses_deck_height(self):
        store = self.make(with_deck(with_lake(flat)))
        res = self.plan(store, (-60, 0), (60, 0))
        self.assertEqual(res["status"], "complete")
        kinds = self.kinds(res)
        self.assertIn("bridge_start", kinds)
        self.assertIn("bridge_end", kinds)
        self.assertEqual(len(res["bridges"]), 1)
        deck = [w for w in res["waypoints"] if w["kind"].startswith("bridge")]
        self.assertGreaterEqual(len(deck), 3)
        for w in deck:
            self.assertEqual(w["y"], LAND + 1)  # deck y, not the lake bed (55) nor shore terrain (64)
            self.assertEqual(w["bridge"], res["bridges"][0]["id"])
        self.assertLess(abs(res["waypoints"][0]["z"]), 12)
        self.assertEqual(res["detours"], [])
        self.assertLess(res["distance"], 140)
        start = next(w for w in deck if w["kind"] == "bridge_start")
        end = next(w for w in deck if w["kind"] == "bridge_end")
        self.assertEqual((start["x"], end["x"]), (-20, 20))
        self.assert_dry_and_off_cliff(res)

    def test_bridge_walked_in_reverse(self):
        res = self.plan(self.make(with_deck(with_lake(flat))), (60, 0), (-60, 0))
        kinds = self.kinds(res)
        self.assertLess(kinds.index("bridge_start"), kinds.index("bridge_end"))
        start = next(w for w in res["waypoints"] if w["kind"] == "bridge_start")
        self.assertEqual(start["x"], 20)

    def test_pier_is_not_a_bridge(self):
        store = self.make(with_deck(with_lake(flat), x1=-20, x2=0))  # a dock that stops in the water
        grid = route.Grid(self.view(store))
        grid.detect_chunk(0, 0)
        grid.detect_chunk(-1, 0)
        self.assertEqual(grid.bridges, {})
        res = self.plan(store, (-60, 0), (60, 0))
        self.assertEqual(res["bridges"], [])
        self.assertTrue(any(abs(w["z"]) > 30 for w in res["waypoints"]))  # still goes around

    def test_island_dock_counts_only_with_land_at_both_ends(self):
        base = with_lake(flat)

        def col(x, z):  # island of land in the middle, deck from the west shore to it
            if -3 <= x <= 3 and -3 <= z <= 3:
                return flat(x, z)
            return base(x, z)
        store = self.make(with_deck(col, x1=-20, x2=-4))
        grid = route.Grid(self.view(store))
        grid.detect_chunk(-1, 0)
        self.assertEqual(len(grid.bridges), 1)

    def test_roof_is_not_a_bridge(self):
        def col(x, z):
            if 0 <= x <= 7 and 0 <= z <= 3:
                return (LAND + 4, PLANKS, LAND, GRASS, False)
            return flat(x, z)
        grid = route.Grid(self.view(self.make(col)))
        grid.detect_chunk(0, 0)
        self.assertEqual(grid.bridges, {})

    def test_bridge_in_unseen_chunk_is_not_detected(self):
        store = self.make(with_deck(with_lake(flat)))
        seen = [(cx, cz) for cx in range(-10, 11) for cz in range(-10, 11) if cx != 1]
        store.mark_seen("other", [list(k) for k in seen], None)
        grid = route.Grid(self.view(store, "other"))
        grid.detect_chunk(0, 0)  # the deck runs on into chunk 1, which "other" has not seen
        self.assertEqual(grid.bridges, {})


class PathTests(RouteCase):
    def test_path_preferred_and_marked(self):
        def col(x, z):
            if -80 <= x <= 80 and z in (0, 1):
                return (LAND, PATH, LAND, PATH, False)
            return flat(x, z)
        res = self.plan(self.make(col), (-90, 14), (90, 14))
        self.assertEqual(res["status"], "complete")
        self.assertIn("path", self.kinds(res))
        near = [w for w in res["waypoints"] if abs(w["z"]) <= 3]
        self.assertTrue(near, res["waypoints"])

    def test_cost_ordering(self):
        land = {"kind": "land", "h": 64, "mult": 1.0}
        path = {"kind": "path", "h": 64, "mult": route.PATH_COST}
        bridge = {"kind": "bridge", "h": 64, "mult": route.BRIDGE_COST}
        water = {"kind": "water", "h": 63, "mult": route.WATER_COST}
        steep = {"kind": "land", "h": 70, "mult": 1.0}
        c = lambda a, b: route._edge_cost(a, b, False)
        self.assertLess(c(path, path), c(land, land))
        self.assertLess(c(bridge, bridge), c(land, land))
        self.assertGreater(c(water, water), c(land, land))
        self.assertGreater(c(land, steep), c(land, land))
        self.assertIsNone(c(bridge, water))  # a deck is not entered from the water beside it
        self.assertIsNone(c(land, {"kind": "land", "h": 80, "mult": 1.0}))  # a cliff-sized step


class GatingTests(RouteCase):
    def test_unseen_target_is_partial_and_never_read(self):
        store = self.make(flat)
        seen = [(cx, cz) for cx in range(-10, 11) for cz in range(-10, 11) if cx <= 2]
        store.mark_seen("g", [list(k) for k in seen], None)
        view = store_view.StoreView(store, "g")
        res = route.route(view, -100, 0, 140, 0, rng=random.Random(1))
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["reason"], "unexplored")
        last = res["waypoints"][-1]
        self.assertEqual(last["kind"], "frontier")
        self.assertLess(last["x"], 3 * 16 + 4)
        self.assertGreater(res["remaining"], 80)
        self.assertFalse(res["target"]["known"])
        self.assertIsNone(res["target"]["y"])
        self.assertGreater(res["unexplored_chunks"], 0)
        for w in res["waypoints"]:
            self.assertLess(w["x"] >> 4, 3)

    def test_unseen_chunks_never_loaded(self):
        store = self.make(flat)
        seen = [(cx, cz) for cx in range(-10, 11) for cz in range(-10, 11) if cx <= 2]
        store.mark_seen("g", [list(k) for k in seen], None)
        view = store_view.StoreView(store, "g")
        asked = []
        orig = store.get_or_load
        store.get_or_load = lambda cx, cz: (asked.append((cx, cz)), orig(cx, cz))[1]
        route.route(view, -100, 0, 140, 0, rng=random.Random(1))
        self.assertTrue(all(cx <= 2 for cx, _ in asked), [a for a in asked if a[0] > 2][:3])

    def test_start_unseen_is_blocked(self):
        store = self.make(flat, seen=[(5, 5)])
        res = self.plan(store, (-100, 0), (100, 0))
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["reason"], "start_unexplored")
        self.assertEqual(res["waypoints"], [])

    def test_gap_in_the_middle_routes_to_frontier(self):
        store = self.make(flat)
        seen = [(cx, cz) for cx in range(-10, 11) for cz in range(-10, 11) if not (-1 <= cx <= 1)]
        store.mark_seen("g", [list(k) for k in seen], None)
        res = route.route(store_view.StoreView(store, "g"), -100, 0, 100, 0, rng=random.Random(1))
        # a 3-chunk-wide unseen band can be walked around inside the seen box, or reported as partial
        self.assertIn(res["status"], ("complete", "partial"))
        for w in res["waypoints"]:
            self.assertNotIn(w["x"] >> 4, (-1, 0, 1)) if w["kind"] != "target" else None

    def test_blocked_by_cliff_wall(self):
        def col(x, z):
            if 0 <= x <= 7:
                return (LAND + 20, "minecraft:stone", LAND + 20, "minecraft:stone", False)
            return flat(x, z)
        res = self.plan(self.make(col), (-60, 0), (60, 0))
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["reason"], "blocked")
        self.assertLess(res["waypoints"][-1]["x"], 0)


class BoundsTests(RouteCase):
    def test_node_limit(self):
        res = self.plan(self.make(flat), (-150, 0), (150, 0), max_nodes=40)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["reason"], "limit")
        self.assertLessEqual(res["nodes"], 40)

    def test_time_limit_with_injected_clock(self):
        ticks = iter(range(0, 100000))
        res = self.plan(self.make(flat), (-150, 0), (150, 0), now=lambda: next(ticks) * 0.5, time_limit=1.0)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["reason"], "limit")

    def test_distance_clamp(self):
        res = self.plan(self.make(flat), (-150, 0), (150, 0), max_distance=100)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["reason"], "distance")
        self.assertGreater(res["remaining"], 150)
        self.assertLess(res["waypoints"][-1]["x"], -40)

    def test_chunk_limit(self):
        res = self.plan(self.make(flat), (-150, 0), (150, 0), max_chunks=6)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["reason"], "limit")

    def test_same_cell_is_complete(self):
        res = self.plan(self.make(flat), (1, 1), (2, 2))
        self.assertEqual(res["status"], "complete")
        self.assertEqual(res["remaining"], 0)

    def test_waypoint_count_and_spacing(self):
        res = self.plan(self.make(flat), (-150, 0), (150, 0))
        self.assertLessEqual(len(res["waypoints"]), route.MAX_WAYPOINTS)
        prev = (-150, 0)
        for w in res["waypoints"]:
            self.assertLessEqual(((w["x"] - prev[0]) ** 2 + (w["z"] - prev[1]) ** 2) ** 0.5, route.MAX_LEG + 12)
            prev = (w["x"], w["z"])


class VariationTests(RouteCase):
    def hilly(self):
        def col(x, z):
            if 40 <= x <= 44:  # a sheer drop beside the route
                return (LAND + 12, "minecraft:stone", LAND + 12, "minecraft:stone", False)
            return with_lake(flat, -20, 20, -30, 30)(x, z)
        return col

    def test_seeded_runs_are_deterministic_and_vary(self):
        store = self.make(self.hilly())
        a = self.plan(store, (-70, 0), (90, 40), seed=3)
        b = self.plan(store, (-70, 0), (90, 40), seed=3)
        self.assertEqual(a["waypoints"], b["waypoints"])
        others = {json.dumps(self.plan(store, (-70, 0), (90, 40), seed=s)["waypoints"]) for s in range(12)}
        self.assertGreater(len(others), 1)

    def test_variation_never_lands_in_water_or_on_a_cliff(self):
        store = self.make(self.hilly())
        for seed in range(25):
            res = self.plan(store, (-70, 0), (90, 40), seed=seed)
            self.assertIn(res["status"], ("complete", "partial"))
            self.assertTrue(res["waypoints"])
            self.assert_dry_and_off_cliff(res)


class HttpTests(RouteCase):
    def setUp(self):
        store = self.make(with_deck(with_lake(flat)))
        self.api = TopographyAPI(store, "127.0.0.1", 0)
        self.server = None
        import http.server
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.api.make_handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, params):
        try:
            with urllib.request.urlopen(f"{self.base}/route?{urlencode(params)}") as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_route_ok(self):
        status, data = self.get({"x": -60, "z": 0, "x2": 60, "z2": 0, "agent": "bot"})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "complete")
        self.assertIn("bridge_start", [w["kind"] for w in data["waypoints"]])
        self.assertEqual(data["bridges"][0]["a"]["y"], LAND + 1)

    def test_other_agent_has_seen_nothing(self):
        status, data = self.get({"x": -60, "z": 0, "x2": 60, "z2": 0, "agent": "stranger"})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "blocked")
        self.assertEqual(data["waypoints"], [])

    def test_validation(self):
        self.assertEqual(self.get({"x": 0, "z": 0, "x2": 5, "z2": 5})[0], 400)  # no agent
        self.assertEqual(self.get({"x": 0, "z": 0, "x2": 5, "agent": "bot"})[0], 400)  # no z2
        self.assertEqual(self.get({"x": "a", "z": 0, "x2": 5, "z2": 5, "agent": "bot"})[0], 400)
        self.assertEqual(self.get({"x": 0, "z": 0, "x2": 99999999999, "z2": 5, "agent": "bot"})[0], 400)

    def test_nether_is_unavailable(self):
        status, data = self.get({"x": 0, "z": 0, "x2": 50, "z2": 50, "agent": "bot",
                                 "dimension": "minecraft:the_nether"})
        self.assertEqual(status, 200)
        self.assertEqual(data["unavailable"], "dimension")
        self.assertNotIn("waypoints", data)

    def test_response_is_small(self):
        status, data = self.get({"x": -150, "z": 0, "x2": 150, "z2": 0, "agent": "bot"})
        self.assertLess(len(json.dumps(data)), 8000)


if __name__ == "__main__":
    unittest.main()
