"""Settlement detection, survey digest, seen ledger and agent gating."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import loader
import map_store
import settlements
import store_view
import survey
from api import TopographyAPI
from tests import helpers

G = helpers.FLAT_GROUND


def village(cx0, cz0, extra_house=False):
    """Five houses from (cx0, cz0) plus a path, a well (cobble ring + water) and a bell."""
    extras = {}
    houses = [(0, 0), (10, 0), (20, 0), (0, 12), (20, 12)]
    if extra_house:
        houses.append((10, 12))
    for i, (dx, dz) in enumerate(houses):
        marks = {(cx0 + dx + 2, cz0 + dz + 2): {G + 1: "minecraft:torch"}}
        if i == 0:
            marks[(cx0 + dx + 3, cz0 + dz + 3)] = {G + 1: "minecraft:bell"}
        extras.update(helpers.house_extras(cx0 + dx, cz0 + dz, 7, 7, extras=marks))
    surface, water = {}, {}
    for x in range(cx0, cx0 + 28):
        surface[(x, cz0 + 9)] = helpers.PATH
    wx, wz = cx0 + 12, cz0 + 21
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            surface[(wx + dx, wz + dz)] = helpers.COBBLE
    surface.pop((wx, wz))
    water[(wx, wz)] = G - 1
    return extras, surface, water


class WorldCase(unittest.TestCase):
    def make(self, chunk_boxes, extras, surface=None, water=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        world = helpers.build_structure_world(tmp.name, chunk_boxes, extras, surface=surface, water=water)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = map_store.MapStore(str(Path(tmp.name) / "m.db"), ld)
        self.addCleanup(store.stop)
        return store


class DetectionTests(WorldCase):
    def test_village_is_one_settlement(self):
        extras, surface, water = village(80, 80)
        store = self.make([(4, 4, 9, 9)], extras, surface, water)
        res = settlements.find_settlements(store, 94, 92, 96)
        self.assertEqual(len(res["settlements"]), 1)
        s = res["settlements"][0]
        self.assertEqual(s["kind"], "settlement")
        self.assertEqual(s["building_count"], 5)
        self.assertTrue(s["inside"])
        self.assertTrue(s["id"].startswith("s:"))
        self.assertGreaterEqual(s["confidence"], 0.5)
        self.assertIn("5 roofed structures", s["evidence"])
        self.assertLessEqual(s["bbox"]["x1"], 80)

    def test_forest_is_not_a_settlement(self):
        extras = {}
        for i in range(6):
            for j in range(6):
                extras.update(helpers.tree_extras(70 + i * 7, 70 + j * 7))
        store = self.make([(4, 4, 9, 9)], extras)
        res = settlements.find_settlements(store, 96, 96, 96)
        self.assertEqual(res["settlements"], [])

    def test_single_hut_is_outpost(self):
        store = self.make([(4, 4, 7, 7)], helpers.house_extras(80, 80, 6, 6))
        res = settlements.find_settlements(store, 85, 85, 64)
        self.assertEqual(len(res["settlements"]), 1)
        s = res["settlements"][0]
        self.assertEqual(s["kind"], "outpost")
        self.assertEqual(s["building_count"], 1)
        self.assertLess(s["confidence"], 0.5)

    def test_two_far_villages_have_distinct_ids(self):
        e1, s1, w1 = village(80, 80)
        e2, s2, w2 = village(400, 80)
        store = self.make([(4, 4, 9, 9), (24, 4, 29, 9)], {**e1, **e2}, {**s1, **s2}, {**w1, **w2})
        res = settlements.find_settlements(store, 256, 96, 256)
        self.assertEqual(len(res["settlements"]), 2)
        self.assertEqual(len({s["id"] for s in res["settlements"]}), 2)

    def test_growth_keeps_id(self):
        e1, s1, w1 = village(80, 80)
        a = self.make([(4, 4, 9, 9)], e1, s1, w1)
        e2, s2, w2 = village(80, 80, extra_house=True)
        b = self.make([(4, 4, 9, 9)], e2, s2, w2)
        ra = settlements.find_settlements(a, 94, 92, 96)["settlements"][0]
        rb = settlements.find_settlements(b, 94, 92, 96)["settlements"][0]
        self.assertEqual(rb["building_count"], 6)
        self.assertEqual(ra["id"], rb["id"])

    def test_missing_chunks_and_memo(self):
        store = self.make([(4, 4, 5, 5)], helpers.house_extras(70, 70, 6, 6))
        r1 = settlements.find_settlements(store, 72, 72, 64)
        self.assertGreater(r1["missing_chunks"], 0)
        self.assertEqual(r1, settlements.find_settlements(store, 72, 72, 64))

    def test_radius_validated(self):
        store = self.make([(4, 4, 5, 5)], {})
        with self.assertRaises(ValueError):
            settlements.find_settlements(store, 0, 0, 8)


class SurveyTests(WorldCase):
    def test_palette_roles_and_landmarks(self):
        extras, surface, water = village(80, 80)
        store = self.make([(4, 4, 9, 9)], extras, surface, water)
        res = survey.survey(store, 64, 64, 159, 159)
        self.assertEqual(res["building_count"], 5)
        pal = res["palette"]
        self.assertEqual(pal["walls"][0][0], helpers.PLANKS)
        self.assertEqual(pal["roof"][0][0], helpers.STAIRS)
        self.assertEqual(pal["doors"][0][0], "minecraft:oak_door")
        self.assertEqual(pal["glass"][0][0], "minecraft:glass_pane")
        self.assertEqual(pal["lighting"][0][0], "minecraft:torch")
        self.assertEqual(pal["paths"][0][0], helpers.PATH)
        for role in pal.values():
            self.assertLessEqual(len(role), 5)
        kinds = {lm["kind"] for lm in res["landmarks"]}
        self.assertTrue({"tallest", "bell", "well"} <= kinds, kinds)
        self.assertLessEqual(len(res["landmarks"]), 8)
        self.assertLessEqual(len(res["summary"]), 400)
        self.assertIn("5 buildings", res["summary"])
        self.assertEqual(res["buildings"][0]["wall_materials"][0], helpers.PLANKS)
        self.assertEqual(res["dimensions"]["footprint_max"], 49)
        self.assertEqual(res["missing_chunks"], 0)
        self.assertEqual(res["unexplored_chunks"], 0)
        self.assertEqual(res["sampled_chunks"], 36)

    def test_bridge_landmark(self):
        water = {(x, z): G - 1 for x in range(64, 96) for z in range(70, 90)}
        extras = {}
        for x in range(66, 94):
            for z in (79, 80):
                extras[(x, z)] = {G: helpers.PLANKS}
                water.pop((x, z), None)
        store = self.make([(4, 4, 5, 5)], extras, None, water)
        res = survey.survey(store, 64, 64, 95, 95)
        self.assertIn("bridge", {lm["kind"] for lm in res["landmarks"]})

    def test_size_caps(self):
        store = self.make([(4, 4, 5, 5)], {})
        with self.assertRaises(ValueError):
            survey.survey(store, 0, 0, 200, 50)
        with self.assertRaises(ValueError):
            survey.survey(store, 0, 0, 50, 161)
        res = survey.survey(store, 64, 64, 95, 95)
        self.assertEqual(res["building_count"], 0)
        self.assertEqual(res["summary"], "no buildings detected")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "m.db")

    def test_mark_filter_stats_and_persistence(self):
        store = map_store.MapStore(self.path, helpers.FakeLoader({}))
        self.assertEqual(store.mark_seen("a", [[0, 0], [1, 1], ["x", 2]]), 2)
        self.assertEqual(store.seen_filter("a", [(0, 0), (5, 5)]), {(0, 0)})
        self.assertEqual(store.seen_filter("b", [(0, 0)]), set())
        st = store.seen_stats("a")
        self.assertEqual(st["chunks"], 2)
        self.assertLessEqual(st["first_seen"], st["last_seen"])
        store.stop()
        again = map_store.MapStore(self.path, helpers.FakeLoader({}))
        self.addCleanup(again.stop)
        self.assertEqual(again.seen_filter("a", [(0, 0), (1, 1)]), {(0, 0), (1, 1)})

    def test_cap_per_call(self):
        store = map_store.MapStore(self.path, helpers.FakeLoader({}))
        self.addCleanup(store.stop)
        n = store.mark_seen("a", [[i, 0] for i in range(6000)])
        self.assertEqual(n, map_store.MAX_SEEN_PER_CALL)

    def test_v2_database_migrates_without_losing_chunks(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
        conn.execute(map_store.SCHEMA)
        conn.execute("INSERT INTO chunks VALUES (3, 4, '{}', '{}', 1.0, NULL, NULL)")
        conn.commit()
        conn.close()
        store = map_store.MapStore(self.path, None)
        self.addCleanup(store.stop)
        self.assertEqual(store.discarded_cold, 0)
        self.assertEqual(store.stats()["cold_chunks"], 1)
        self.assertEqual(store.mark_seen("a", [[3, 4]]), 1)


class GatingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        extras, surface, water = village(80, 80)
        world = helpers.build_structure_world(
            cls._tmp.name, [(4, 4, 9, 9)], extras, surface=surface, water=water
        )
        cls.loader = loader.ChunkLoader(world)
        cls.store = map_store.MapStore(str(Path(cls._tmp.name) / "m.db"), cls.loader)
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

    def req(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def test_without_agent_everything_visible(self):
        st, res = self.req("GET", "/settlements", {"x": 94, "z": 92, "radius": 96})
        self.assertEqual(st, 200)
        self.assertEqual(len(res["settlements"]), 1)

    def test_gating_and_seen_flow(self):
        agent = "bob"
        q = {"x": 94, "z": 92, "radius": 96, "agent": agent}
        st, res = self.req("GET", "/settlements", q)
        self.assertEqual(res["settlements"], [])
        self.assertGreater(res["unexplored_chunks"], 0)
        self.assertGreaterEqual(res["missing_chunks"], res["unexplored_chunks"])
        st, res = self.req("GET", "/chunk", {"cx": 5, "cz": 5, "agent": agent})
        self.assertEqual(st, 404)
        self.assertIn("not been explored", res["error"])
        st, res = self.req("GET", "/recall", {"x": 90, "z": 90, "agent": agent})
        self.assertEqual(st, 404)
        st, res = self.req("GET", "/perception", {"x": 94, "z": 92, "size_chunks": 4, "agent": agent})
        self.assertEqual(st, 200)
        self.assertEqual(res["unexplored_chunks"], 16)
        self.assertEqual(res["missing_chunks"], 16)
        st, res = self.req("GET", "/survey", {"x1": 70, "z1": 70, "x2": 130, "z2": 130, "agent": agent})
        self.assertEqual(res["building_count"], 0)
        self.assertGreater(res["unexplored_chunks"], 0)
        # another agent that has seen the area is unaffected
        chunks = [[cx, cz] for cx in range(-4, 20) for cz in range(-4, 20)]
        st, res = self.req("POST", "/seen", body={"agent": "alice", "dimension": "overworld", "chunks": chunks})
        self.assertEqual(res, {"accepted": len(chunks)})
        st, res = self.req("GET", "/settlements", {"x": 94, "z": 92, "radius": 96, "agent": "alice"})
        self.assertEqual(len(res["settlements"]), 1)
        self.assertEqual(res["unexplored_chunks"], 0)
        # bob sees exactly one chunk
        self.req("POST", "/seen", body={"agent": agent, "dimension": "minecraft:overworld", "chunks": [[5, 5]]})
        self.assertEqual(self.req("GET", "/chunk", {"cx": 5, "cz": 5, "agent": agent})[0], 200)
        self.assertEqual(self.req("GET", "/chunk", {"cx": 6, "cz": 5, "agent": agent})[0], 404)
        st, res = self.req("GET", "/seen_stats", {"agent": agent})
        self.assertEqual(res["chunks"], 1)
        self.assertEqual(res["agent"], agent)

    def test_non_overworld_seen_ignored(self):
        st, res = self.req("POST", "/seen", body={"agent": "n", "dimension": "the_nether", "chunks": [[0, 0], [1, 1]]})
        self.assertEqual(res, {"accepted": 0, "ignored": 2})
        self.assertEqual(self.req("GET", "/seen_stats", {"agent": "n"})[1]["chunks"], 0)

    def test_survey_endpoint_caps(self):
        self.assertEqual(self.req("GET", "/survey", {"x1": 0, "z1": 0, "x2": 300, "z2": 10})[0], 400)
        st, res = self.req("GET", "/survey", {"x1": 70, "z1": 70, "x2": 130, "z2": 130})
        self.assertEqual(st, 200)
        self.assertEqual(res["building_count"], 5)

    def test_gated_view_exposes_no_write_path(self):
        view = store_view.StoreView(self.store, "zed")
        with self.assertRaises(AttributeError):
            view.put_chunk


if __name__ == "__main__":
    unittest.main()
