"""Long-range overview: per-chunk summaries, gating, tiers, cells, features, API, far line."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import map_store
import overview
import perception
import store_view
from api import TopographyAPI
from tests import helpers

GRASS = helpers.GRASS
STONE = "minecraft:stone"
PLANKS = "minecraft:oak_planks"


def blank_surface():
    return {
        "y": [None] * 256,
        "materials": [None] * 256,
        "ground_y": [None] * 256,
        "ground_materials": [None] * 256,
        "water": [False] * 256,
        "terrain_y": [None] * 256,
        "terrain_materials": [None] * 256,
    }


def set_column(surface, index, y, material, terrain_y, terrain_material, water=False):
    surface["y"][index] = y
    surface["materials"][index] = material
    surface["ground_y"][index] = terrain_y
    surface["ground_materials"][index] = terrain_material
    surface["water"][index] = water
    surface["terrain_y"][index] = terrain_y
    surface["terrain_materials"][index] = terrain_material


class SummaryTests(unittest.TestCase):
    def test_summarize_every_column(self):
        surface = blank_surface()
        set_column(surface, 0, 70, GRASS, 68, GRASS)
        set_column(surface, 1, 63, "minecraft:water", 60, "minecraft:sand", water=True)
        set_column(surface, 2, 80, PLANKS, 65, GRASS)              # built
        set_column(surface, 3, 75, "minecraft:oak_leaves", 72, GRASS)  # tree
        set_column(surface, 4, 66, STONE, 66, STONE)
        set_column(surface, 5, 66, "minecraft:sand", 66, "minecraft:sand")
        summary = overview.summarize(surface)
        self.assertEqual(summary, {
            "known": 6, "water": 1, "h_min": 65, "h_max": 72, "h_sum": 337,
            "trees": 1, "built": 1, "cls": "grass",
        })

    def test_land_class(self):
        self.assertEqual(overview.land_class("minecraft:sandstone"), "sand")
        self.assertEqual(overview.land_class("minecraft:snow_block"), "snow")
        self.assertEqual(overview.land_class("minecraft:ice"), "snow")
        self.assertEqual(overview.land_class("minecraft:orange_terracotta"), "badlands")
        for rock in ("minecraft:stone", "minecraft:deepslate", "minecraft:gravel",
                     "minecraft:cobblestone", "minecraft:blackstone"):
            self.assertEqual(overview.land_class(rock), "rock")
        for grass in ("minecraft:grass_block", "minecraft:dirt", "minecraft:podzol",
                      "minecraft:mud"):
            self.assertEqual(overview.land_class(grass), "grass")
        self.assertEqual(overview.land_class(PLANKS), "other")
        self.assertEqual(overview.land_class(None), "other")
        self.assertEqual(overview.land_class(""), "other")


class StoreSummaryTests(unittest.TestCase):
    def setUp(self):
        overview._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, records=None, cleanup=True):
        store = map_store.MapStore(str(Path(self.tmp.name) / "m.db"),
                                   helpers.FakeLoader(records or {}))
        if cleanup:
            self.addCleanup(store.stop)
        return store

    def load(self, store, cx=0, cz=0):
        record = store.get_or_load(cx, cz)
        store.flush()
        return record

    def test_flush_writes_the_record_revision(self):
        record = helpers.make_record(0, 0)
        store = self.make({(0, 0): record})
        self.load(store)
        rows = store.summary_rows(-1, -1, 1, 1)
        self.assertIn((0, 0), rows)
        rev, summary = rows[(0, 0)][-1]
        self.assertEqual(rev, 1)
        self.assertEqual(summary, overview.summarize(record["surface"]))
        self.assertNotIn((0, 0), store._hot)
        self.assertIn((0, 0), store._warm)

    def test_changed_chunk_replaces_or_keeps_the_old_summary(self):
        record = helpers.make_record(0, 0)
        store = self.make({(0, 0): record})
        self.load(store)
        store.apply_diff({"action": "place", "x": 0, "y": 65, "z": 0,
                          "block_name": PLANKS})
        store.flush()
        revs = [rev for rev, _ in store.summary_rows(-1, -1, 1, 1)[(0, 0)]]
        self.assertEqual(revs, [2])

        store2 = self.make({(0, 0): helpers.make_record(0, 0)})
        store2.get_or_load(0, 0)
        store2.flush()
        store2.report_chunks("a", [[0, 0]], None)
        store2.report_chunks("a", [], [[0, 0]])
        before = [rev for rev, _ in store2.summary_rows(-1, -1, 1, 1)[(0, 0)]]
        store2.apply_diff({"action": "place", "x": 0, "y": 65, "z": 0,
                           "block_name": PLANKS})
        store2.flush()
        revs = [rev for rev, _ in store2.summary_rows(-1, -1, 1, 1)[(0, 0)]]
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[0], before[0])
        self.assertLess(revs[0], revs[1])
        self.assertIn((0, 0), store2._pins)

    def test_backfill_summarises_existing_rows(self):
        record = helpers.make_record(0, 0)
        store = self.make({(0, 0): record}, cleanup=False)
        self.load(store)
        store._conn.execute("DELETE FROM chunk_summary")
        store._conn.commit()
        store.stop()
        reopened = map_store.MapStore(str(Path(self.tmp.name) / "m.db"),
                                      helpers.FakeLoader({(0, 0): record}))
        self.addCleanup(reopened.stop)
        rows = reopened.summary_rows(-1, -1, 1, 1)
        self.assertIn((0, 0), rows)
        self.assertEqual(rows[(0, 0)][-1][1], overview.summarize(record["surface"]))

    def test_drop_path_drops_summaries(self):
        record = helpers.make_record(0, 0)
        store = self.make({(0, 0): record}, cleanup=False)
        self.load(store)
        self.assertTrue(store.summary_rows(-1, -1, 1, 1))
        store._conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        store._conn.commit()
        store.stop()
        reopened = map_store.MapStore(str(Path(self.tmp.name) / "m.db"),
                                      helpers.FakeLoader({(0, 0): record}))
        self.addCleanup(reopened.stop)
        self.assertEqual(reopened.summary_rows(-100, -100, 100, 100), {})
        self.assertEqual(reopened.stats()["cold_chunks"], 0)

    def test_gating_only_shows_seen_chunks(self):
        store = self.make({(0, 0): helpers.make_record(0, 0),
                           (1, 0): helpers.make_record(1, 0)})
        self.load(store, 0, 0)
        self.load(store, 1, 0)
        store.mark_seen("Topo", [[0, 0]], None)
        rows = store_view.StoreView(store, "Topo").summaries(-5, -5, 5, 5)
        self.assertEqual(set(rows), {(0, 0)})
        self.assertEqual(store_view.StoreView(store, "Topo").seen_chunks(-5, -5, 5, 5), 1)

    def test_seen_lake_survives_client_disconnect(self):
        """A Mindcraft restart disconnects (replace with an empty loaded set) and reconnects
        elsewhere: the lake the agent saw must still be on its overview (long-range test M1)."""
        lake = {(cx, cz): helpers.make_record(cx, cz, height=55, water_level=62)
                for cx in range(-30, -20) for cz in range(-2, 3)}
        here = {(0, 0): helpers.make_record(0, 0)}
        store = self.make({**lake, **here})
        for cx, cz in lake:
            self.load(store, cx, cz)
        store.report_chunks("Topo", [list(k) for k in lake], None, replace=True)
        store.report_chunks("Topo", [], None, replace=True)        # disconnect
        self.load(store)
        store.report_chunks("Topo", [[0, 0]], None, replace=True)   # reconnect far away
        rows = store_view.StoreView(store, "Topo").summaries(-31, -3, 1, 3)
        self.assertTrue(set(lake) <= set(rows), "seen lake chunks dropped after disconnect")
        self.assertTrue(all(rows[k]["water"] == 256 for k in lake))
        data = overview.build(store_view.StoreView(store, "Topo"), 0, 0, 2048)
        self.assertTrue(any(c["dir"] == "west" and c.get("water") for c in data["cells"]),
                        data["cells"])

    def test_last_seen_summary_is_the_pinned_revision(self):
        store = self.make({(0, 0): helpers.make_record(0, 0)})
        self.load(store)
        store.report_chunks("away", [[0, 0]], None)
        store.report_chunks("away", [], [[0, 0]])
        store.report_chunks("here", [[0, 0]], None)
        store.apply_diff({"action": "place", "x": 0, "y": 70, "z": 0,
                          "block_name": PLANKS})
        store.flush()
        away = store_view.StoreView(store, "away").summaries(-1, -1, 1, 1)[(0, 0)]
        here = store_view.StoreView(store, "here").summaries(-1, -1, 1, 1)[(0, 0)]
        self.assertEqual(away["built"], 0)
        self.assertEqual(here["built"], 1)

    def test_pin_at_revision_zero_is_skipped(self):
        store = self.make({(0, 0): helpers.make_record(0, 0)})
        self.load(store)
        store.report_chunks("blind", [[0, 0]], None)
        store.report_chunks("blind", [], [[0, 0]])
        store._pins[(0, 0)]["blind"] = (0, time.time() - 1000)
        self.assertEqual(store_view.StoreView(store, "blind").summaries(-1, -1, 1, 1), {})

    def test_overview_never_touches_the_tiers(self):
        store = self.make({(0, 0): helpers.make_record(0, 0)})
        self.load(store)
        store.mark_seen("a", [[0, 0]], None)
        view = store_view.StoreView(store, "a")
        before = (len(store._hot), len(store._warm))
        with mock.patch.object(map_store.MapStore, "get_chunk", side_effect=AssertionError("get_chunk")), \
             mock.patch.object(map_store.MapStore, "get_or_load", side_effect=AssertionError("get_or_load")), \
             mock.patch.object(map_store.MapStore, "touch_window", side_effect=AssertionError("touch_window")):
            overview.build(view, 0, 0, 2048)
        self.assertEqual((len(store._hot), len(store._warm)), before)


def add_summary(store, cx, cz, rev=1, **values):
    row = {"known": 256, "water": 0, "h_min": 64, "h_max": 64, "h_sum": 64 * 256,
           "trees": 0, "built": 0, "cls": "grass"}
    row.update(values)
    store._conn.execute(
        "INSERT OR REPLACE INTO chunk_summary "
        "(cx, cz, rev, known, water, h_min, h_max, h_sum, trees, built, cls) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cx, cz, rev, row["known"], row["water"], row["h_min"], row["h_max"],
         row["h_sum"], row["trees"], row["built"], row["cls"]),
    )
    store._conn.commit()


class BuildTests(unittest.TestCase):
    def setUp(self):
        overview._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = map_store.MapStore(str(Path(self.tmp.name) / "m.db"),
                                        helpers.FakeLoader({}))
        self.addCleanup(self.store.stop)

    def test_cells_sectors_rings_land_water_and_y(self):
        for cx in range(-20, -10):
            for cz in range(-2, 3):
                add_summary(self.store, cx, cz, water=256, h_min=None, h_max=None, h_sum=None,
                            cls=None)
        add_summary(self.store, 0, -20, h_min=100, h_max=100, h_sum=100 * 256, cls="rock")
        data = overview.build(self.store, 0, 0, 2048)
        west = [cell for cell in data["cells"] if cell["dir"] == "west"]
        self.assertTrue(west)
        lake = west[0]
        self.assertEqual(lake["from"], 128)
        self.assertEqual(lake["to"], 512)
        self.assertEqual(lake["land"], "water")
        self.assertEqual(lake["water"], 1.0)
        self.assertTrue(lake["partial"])
        north = [cell for cell in data["cells"] if cell["dir"] == "north"]
        self.assertEqual(north[0]["land"], "rock")
        self.assertEqual(north[0]["y"], [100, 100])
        self.assertEqual(data["summarised_chunks"], 51)

    def test_features_in_order_with_limits(self):
        for cx in range(-5, 6):
            for cz in range(-5, 6):
                add_summary(self.store, cx, cz)   # baseline ground 64
        add_summary(self.store, 0, -20, h_min=120, h_max=120, h_sum=120 * 256, cls="rock")
        add_summary(self.store, 0, -40, h_min=130, h_max=130, h_sum=130 * 256, cls="rock")
        for cx in (40, 41):
            for cz in (-1, 0):
                add_summary(self.store, cx, cz, water=200, h_min=None, h_max=None, h_sum=None)
        for cz in (30, 31):
            add_summary(self.store, 0, cz, built=10)
        data = overview.build(self.store, 0, 0, 2048)
        kinds = [f["kind"] for f in data["features"]]
        self.assertEqual(kinds[0], "high_ground")
        self.assertEqual(data["features"][0]["direction"], "north")
        self.assertIn("water", kinds)
        self.assertIn("built", kinds)
        self.assertLess(kinds.index("high_ground"), kinds.index("water"))
        self.assertLess(kinds.index("water"), kinds.index("built"))
        water = next(f for f in data["features"] if f["kind"] == "water")
        self.assertEqual(water["chunks"], 4)
        self.assertTrue(water["open"])
        self.assertIn("centre", water)
        built = next(f for f in data["features"] if f["kind"] == "built")
        self.assertEqual(built["chunks"], 2)

    def test_feature_limits(self):
        for cx in range(-1, 2):
            for cz in range(-1, 2):
                add_summary(self.store, cx, cz)   # low ground for the median
        for index in range(6):
            add_summary(self.store, -60, -60 + index * 20,
                        h_min=120 + index, h_max=120 + index,
                        h_sum=(120 + index) * 256, cls="rock")
        for index in range(6):
            for dx in range(2):
                for dz in range(2):
                    add_summary(self.store, 60 + index * 6 + dx, -60 + dz,
                                water=200, h_min=None, h_max=None, h_sum=None)
        for index in range(6):
            for dx in range(2):
                add_summary(self.store, -60 + index * 6 + dx, 60, built=10)
        data = overview.build(self.store, 0, 0, 4096)
        self.assertLessEqual(sum(1 for f in data["features"] if f["kind"] == "high_ground"), 2)
        self.assertLessEqual(sum(1 for f in data["features"] if f["kind"] == "water"), 3)
        self.assertLessEqual(sum(1 for f in data["features"] if f["kind"] == "built"), 3)
        self.assertLessEqual(len(data["features"]), overview.MAX_FEATURES)

    def test_nothing_nearer_than_near(self):
        add_summary(self.store, 0, 0, water=256, h_min=None, h_max=None, h_sum=None)
        data = overview.build(self.store, 0, 0, 2048)
        self.assertEqual(data["cells"], [])
        self.assertEqual(data["features"], [])

    def test_size_cap_trims_the_outer_ring(self):
        for cx in range(-60, 61, 4):
            for cz in range(-60, 61, 4):
                add_summary(self.store, cx, cz, h_min=60 + cx % 5, h_max=70 + cz % 5,
                            h_sum=65 * 256)
        normal = overview.build(self.store, 0, 0, 4096)
        self.assertNotIn("trimmed", normal)
        overview._cache.clear()
        with mock.patch.object(overview, "MAX_BYTES", 900):
            trimmed = overview.build(self.store, 0, 0, 4096)
        self.assertTrue(trimmed.get("trimmed"))
        self.assertLess(len(trimmed["cells"]), len(normal["cells"]))
        self.assertLessEqual(len(json.dumps(trimmed, separators=(",", ":")).encode()), 900)

    def test_cache_reuses_and_generation_bump_recomputes(self):
        self.store.mark_seen("a", [[0, 0]], None)
        add_summary(self.store, 0, -20, h_min=100, h_max=100, h_sum=100 * 256, cls="rock")
        first = overview.build(self.store, 0, 0, 2048)
        second = overview.build(self.store, 0, 0, 2048)
        self.assertIs(first, second)
        self.store._generation += 1
        third = overview.build(self.store, 0, 0, 2048)
        self.assertIsNot(first, third)

    def test_speed_with_ten_thousand_rows(self):
        rows = []
        for cx in range(-100, 100):
            for cz in range(-25, 25):
                water = 256 if (cx + cz) % 17 == 0 else 0
                rows.append((cx, cz, 1, 256, water, 60, 70, 65 * 256,
                             30 if (cx % 7 == 0) else 0, 10 if (cx % 11 == 0) else 0, "grass"))
        self.store._conn.executemany(
            "INSERT OR REPLACE INTO chunk_summary "
            "(cx, cz, rev, known, water, h_min, h_max, h_sum, trees, built, cls) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.store._conn.commit()
        self.assertEqual(len(rows), 10000)
        overview._cache.clear()
        started = time.perf_counter()
        data = overview.build(self.store, 0, 0, 2048)
        elapsed = time.perf_counter() - started
        print(f"\noverview.build over 10,000 summary rows: {elapsed * 1000:.1f} ms")
        self.assertLess(elapsed, 1.0)
        self.assertTrue(data["cells"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        overview._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = map_store.MapStore(str(Path(self.tmp.name) / "m.db"),
                                        helpers.FakeLoader({}))
        self.addCleanup(self.store.stop)
        add_summary(self.store, 0, -20, h_min=100, h_max=100, h_sum=100 * 256, cls="rock")
        self.store.mark_seen("Topo", [[0, 0], [0, -20]], None)
        self.api = TopographyAPI(self.store, "127.0.0.1", 0)
        import http.server
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.api.make_handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, query, token=None):
        request = urllib.request.Request(self.base + "/overview?" + query)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_defaults_and_parameters(self):
        status, data = self.get("x=0&z=0")
        self.assertEqual(status, 200)
        self.assertEqual(data["radius"], 2048)
        self.assertEqual(data["center"], {"x": 0, "z": 0})
        status, data = self.get("x=0&z=0&radius=256")
        self.assertEqual(status, 200)
        self.assertEqual(data["radius"], 256)
        for bad in ("x=0&z=0&radius=100", "x=0&z=0&radius=5000",
                    "z=0", "x=nope&z=0"):
            self.assertEqual(self.get(bad)[0], 400, bad)

    def test_agent_gating(self):
        status, data = self.get("x=0&z=0&agent=Topo")
        self.assertEqual(status, 200)
        self.assertTrue(data["cells"])
        self.assertEqual(data["seen_chunks"], 2)
        status, data = self.get("x=0&z=0&agent=Nobody")
        self.assertEqual(status, 200)
        self.assertEqual(data["cells"], [])
        self.assertEqual(data["seen_chunks"], 0)

    def test_non_overworld(self):
        status, data = self.get("x=0&z=0&dimension=minecraft:the_nether")
        self.assertEqual(status, 200)
        self.assertEqual(data["unavailable"], "dimension")

    def test_token(self):
        api = TopographyAPI(self.store, "127.0.0.1", 0, token="s3cret")
        import http.server
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), api.make_handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = urllib.request.Request(base + "/overview?x=0&z=0")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 401)
        request = urllib.request.Request(base + "/overview?x=0&z=0",
                                         headers={"Authorization": "Bearer s3cret"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)


class FarLineTests(unittest.TestCase):
    def setUp(self):
        overview._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = map_store.MapStore(str(Path(self.tmp.name) / "m.db"),
                                        helpers.FakeLoader({}))
        self.addCleanup(self.store.stop)
        add_summary(self.store, 0, 0)
        add_summary(self.store, 0, -20, h_min=120, h_max=120, h_sum=120 * 256, cls="rock")
        self.store.mark_seen("Topo", [[0, 0], [0, -20]], None)

    def test_far_is_present_only_for_a_gated_view(self):
        with_agent = perception.build_perception(store_view.StoreView(self.store, "Topo"), 0, 0)
        self.assertIn("far", with_agent)
        self.assertTrue(with_agent["far"])
        self.assertEqual(with_agent["far"][0]["kind"], "high_ground")
        self.assertEqual(set(with_agent["far"][0]), {"kind", "direction", "distance", "y"})
        without = perception.build_perception(self.store, 0, 0)
        self.assertNotIn("far", without)

    def test_perception_survives_an_overview_failure(self):
        view = store_view.StoreView(self.store, "Topo")
        with mock.patch.object(overview, "build", side_effect=RuntimeError("boom")):
            data = perception.build_perception(view, 0, 0)
        self.assertEqual(data["far"], [])
        self.assertIn("lod", data)


if __name__ == "__main__":
    unittest.main()
