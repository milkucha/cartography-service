"""Last-seen map: a chunk the agent does not have loaded is shown as it was when it last had it.

Covers frozen views (diff / snapshot / region reload), live while loaded, revisit refresh, the
untouched no-``agent=`` path, cache isolation between two agents, history pruning and bounds,
first-data grace, restart persistence, old ``/seen`` clients and the additive schema upgrade.
"""

from __future__ import annotations

import copy
import json
import random
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import landforms
import loader
import map_store
import perception
import route
import settlements
import store_view
import survey
from api import TopographyAPI
from tests import helpers
from tests.test_route import flat, make_rec, with_lake
from tests.test_settlements import G, village

PLACE = {"x": 0, "y": 70, "z": 0, "action": "place", "block_name": "minecraft:stone"}


class SimLoader:
    """Loader stand-in with deep-copied records (like the real one) and save timestamps."""

    available = True
    region_dir = "sim"
    region_count = 0

    def __init__(self):
        self.records = {}
        self.timestamps = {}

    def put(self, rec, ts=1):
        key = (rec["cx"], rec["cz"])
        self.records[key] = copy.deepcopy(rec)
        self.timestamps[key] = ts

    def load_chunk(self, cx, cz):
        rec = self.records.get((cx, cz))
        if rec is None:
            return None
        rec = copy.deepcopy(rec)
        rec["save_ts"] = self.timestamps.get((cx, cz))
        return rec

    def chunk_timestamp(self, cx, cz):
        return self.timestamps.get((cx, cz))

    def world_id(self):
        return None


def top(rec):
    return rec["surface"]["y"][0]


def snap(rec):
    return {"cx": rec["cx"], "cz": rec["cz"], "surface": copy.deepcopy(rec["surface"])}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "m.db")
        self.loader = SimLoader()
        for cx in range(-1, 3):
            for cz in range(-1, 3):
                self.loader.put(helpers.make_record(cx, cz, height=64))
        self.t = [1000.0]
        self.store = self.open()

    def open(self, ld=None):
        store = map_store.MapStore(self.path, ld or self.loader)
        store._clock = lambda: self.t[0]
        self.addCleanup(store.stop)
        return store

    def view(self, agent="a", store=None):
        return store_view.StoreView(store or self.store, agent)

    def load(self, agent, *chunks, **kw):
        return self.store.report_chunks(agent, list(chunks), kw.pop("unloaded", []), **kw)

    def unload(self, agent, *chunks):
        return self.store.report_chunks(agent, [], list(chunks))

    def top_for(self, agent, cx=0, cz=0):
        rec = self.view(agent).get_or_load(cx, cz)
        return None if rec is None else top(rec)

    def history_rows(self):
        return self.store._conn.execute("SELECT COUNT(*) FROM chunk_history").fetchone()[0]


class FrozenViewTests(Case):
    def test_live_while_loaded_frozen_after_unload_diff(self):
        self.load("a", (0, 0))
        self.assertEqual(self.top_for("a"), 64)
        self.store.apply_diff(dict(PLACE))
        self.assertEqual(self.top_for("a"), 70, "loaded: live")
        self.unload("a", (0, 0))
        self.assertEqual(self.top_for("a"), 70, "frozen at the state it left")
        self.store.apply_diff(dict(PLACE, y=80))
        rec = self.view("a").get_or_load(0, 0)
        self.assertEqual(top(rec), 70)
        self.assertTrue(rec["frozen"])
        self.assertEqual(top(self.store.get_or_load(0, 0)), 80, "no agent= stays live")
        self.assertEqual(self.store.stats()["history_rows"], 1)

    def test_frozen_after_snapshot(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_snapshot(snap(helpers.make_record(0, 0, height=90)))
        self.assertEqual(self.top_for("a"), 64)
        self.assertEqual(top(self.store.get_or_load(0, 0)), 90)

    def test_frozen_after_region_reload(self):
        self.load("a", (0, 0))
        self.top_for("a")                    # registers the chunk with save_ts 1
        self.unload("a", (0, 0))
        self.loader.put(helpers.make_record(0, 0, height=95), ts=2)
        self.assertEqual(top(self.store.get_or_load(0, 0)), 95)     # live reload
        self.assertEqual(self.store.reloaded_stale, 1)
        self.assertEqual(self.top_for("a"), 64)

    def test_revisit_refreshes(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE))
        gen = self.store.generation
        self.assertEqual(self.top_for("a"), 64)
        self.load("a", (0, 0))                # same session, chunk loads again
        self.assertEqual(self.top_for("a"), 70)
        self.assertGreater(self.store.generation, gen, "window_version moves when frozen turns live")
        self.assertEqual(self.history_rows(), 0, "history pruned once nobody references it")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE, y=75))
        self.assertEqual(self.top_for("a"), 70, "frozen again at the newer state")

    def test_unseen_stays_unexplored_and_other_agent_unaffected(self):
        self.load("a", (0, 0))
        view = self.view("a")
        self.assertIsNone(view.get_or_load(1, 1))
        self.assertEqual(view.unexplored, {(1, 1)})

    def test_touch_window_and_get_window_frozen(self):
        self.load("a", (0, 0), (1, 0))
        self.view("a").touch_window(0, 0, 4)
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE))
        self.store.apply_diff(dict(PLACE, x=16))
        recs, missing = self.view("a").touch_window(0, 0, 4)
        by = {(r["cx"], r["cz"]): r for r in recs}
        self.assertEqual(top(by[(0, 0)]), 64)
        self.assertTrue(by[(0, 0)].get("frozen"))
        self.assertEqual(top(by[(1, 0)]), 70, "still loaded -> live")
        self.assertEqual(missing, 14)
        recs, _ = self.view("a").get_window(0, 0, 1)
        self.assertEqual({top(r) for r in recs if r["cx"] == 0 and r["cz"] == 0}, {64})

    def test_frozen_not_promoted_to_tiers(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE))
        before = (set(self.store._hot), set(self.store._warm))
        for _ in range(3):
            self.view("a").get_or_load(0, 0)
        self.assertEqual((set(self.store._hot), set(self.store._warm)), before)
        for rec in list(self.store._hot.values()) + list(self.store._warm.values()):
            self.assertFalse(rec.get("frozen"))

    def test_survey_does_not_read_sections_of_frozen_chunks(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE))

        class Loader(SimLoader):
            def read_chunk_root(inner, cx, cz):
                raise AssertionError("read the live save for a frozen chunk")
        v = self.view("a")
        v._store.loader = Loader()
        rec0 = v.get_or_load(0, 0)
        self.assertTrue(rec0["frozen"])
        buildings = [{"_cols": {(1, 1): 60}, "bbox": {}, "center": (1, 1)}]
        out = survey._scan_sections(v, {(0, 0): rec0}, buildings, {(0, 0)})
        self.assertTrue(out[3])


class UnloadProtocolTests(Case):
    def test_replace_unloads_the_rest_and_batch_order(self):
        self.load("a", (0, 0), (1, 0), (0, 1))
        self.store.report_chunks("a", [(0, 0)], [], replace=True)
        self.assertEqual(self.store.loaded_snapshot("a")[1], {(0, 0)})
        self.assertIsNotNone(self.store.pin_of("a", (1, 0)))
        self.assertIsNone(self.store.pin_of("a", (0, 0)))
        # a chunk in both lists ends unloaded, and is seen
        self.store.report_chunks("a", [(2, 2)], [(2, 2)])
        self.assertTrue(self.store.is_seen("a", 2, 2))
        self.assertNotIn((2, 2), self.store.loaded_snapshot("a")[1])
        self.assertIsNotNone(self.store.pin_of("a", (2, 2)))

    def test_second_unload_keeps_first_pin(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        pin = self.store.pin_of("a", (0, 0))
        self.store.apply_diff(dict(PLACE))
        self.unload("a", (0, 0))
        self.assertEqual(self.store.pin_of("a", (0, 0))[0], pin[0])
        self.assertEqual(self.top_for("a"), 64)

    def test_silent_agent_is_frozen_after_ttl(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.t[0] += map_store.LOADED_TTL_S - 1
        self.assertEqual(self.store.loaded_snapshot("a")[1], {(0, 0)})
        self.store.apply_diff(dict(PLACE))
        self.assertEqual(self.top_for("a"), 70)
        self.t[0] += 5
        self.store.sweep_loaded()
        self.store.apply_diff(dict(PLACE, y=90))
        self.assertEqual(self.top_for("a"), 70)
        self.store.report_chunks("a", [(0, 0)], [], replace=True)   # heartbeat revives it
        self.assertEqual(self.top_for("a"), 90)

    def test_old_clients_stay_live(self):
        self.store.mark_seen("old", [[0, 0]])
        self.top_for("old")
        self.store.apply_diff(dict(PLACE))
        self.assertEqual(self.top_for("old"), 70)
        self.assertFalse(self.store.loaded_snapshot("old")[0])
        self.assertEqual(self.store.stats()["history_rows"], 0)

    def test_dimension_other_than_overworld_only_marks_seen(self):
        self.store.report_chunks("a", [(0, 0)], [(1, 1)], dimension="the_nether")
        self.assertIsNone(self.store.pin_of("a", (1, 1)))


class HistoryTests(Case):
    def test_no_history_when_nobody_is_frozen(self):
        self.load("a", (0, 0))
        self.top_for("a")
        for i in range(30):
            self.store.apply_diff(dict(PLACE, y=65 + i))
        self.store.apply_snapshot(snap(helpers.make_record(0, 0, height=80)))
        self.assertEqual(self.store.history_saved, 0)
        self.assertEqual(self.history_rows(), 0)

    def test_one_copy_per_revision_shared_by_agents_and_pruned(self):
        for ag in ("a", "b"):
            self.load(ag, (0, 0))
        self.top_for("a")
        for ag in ("a", "b"):
            self.unload(ag, (0, 0))
        for i in range(5):
            self.store.apply_diff(dict(PLACE, y=70 + i))
        self.assertEqual(self.history_rows(), 1)
        self.assertEqual(self.top_for("a"), 64)
        self.assertEqual(self.top_for("b"), 64)
        self.load("a", (0, 0))
        self.assertEqual(self.history_rows(), 1, "b still references it")
        self.load("b", (0, 0))
        self.assertEqual(self.history_rows(), 0)
        self.assertEqual(self.store.stats()["history_bytes"], 0)

    def test_distinct_revisions_for_agents_pinned_at_different_times(self):
        self.load("a", (0, 0))
        self.load("b", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE, y=70))
        self.unload("b", (0, 0))
        self.store.apply_diff(dict(PLACE, y=80))
        self.assertEqual((self.top_for("a"), self.top_for("b")), (64, 70))
        self.assertEqual(self.history_rows(), 2)

    def test_per_chunk_bound_degrades_to_missing_never_live(self):
        with mock.patch.object(map_store, "HISTORY_MAX_PER_CHUNK", 2):
            agents = ["a", "b", "c"]
            for i, ag in enumerate(agents):
                self.load(ag, (0, 0))
            self.top_for("a")
            for i, ag in enumerate(agents):
                self.unload(ag, (0, 0))
                self.store.apply_diff(dict(PLACE, y=70 + i))
            self.assertEqual(self.history_rows(), 2)
            self.assertEqual(self.store.history_evicted, 1)
            self.assertIsNone(self.top_for("a"), "oldest revision evicted: missing, not live")
            self.assertEqual(self.top_for("b"), 70)
            self.assertEqual(self.top_for("c"), 71)
            self.load("a", (0, 0))
            self.assertEqual(self.top_for("a"), 72, "revisiting restores live data")

    def test_total_bytes_bound(self):
        with mock.patch.object(map_store, "HISTORY_MAX_BYTES", 3000):
            keys = [(cx, cz) for cx in range(-1, 3) for cz in range(-1, 3)]
            self.load("a", *keys)
            for k in keys:
                self.top_for("a", *k)
            self.unload("a", *keys)
            for cx, cz in keys:
                self.store.apply_diff(dict(PLACE, x=cx * 16, z=cz * 16))
            stats = self.store.stats()
            self.assertGreater(stats["history_evicted"], 0)
            self.assertLessEqual(stats["history_bytes"], 3000 + 2000)
            self.assertEqual(stats["history_rows"], self.history_rows())
            states = [self.top_for("a", *k) for k in keys]
            self.assertTrue(all(s in (None, 64) for s in states), states)
            self.assertIn(64, states)

    def test_orphan_history_pruned_on_restart(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE))
        self.assertEqual(self.history_rows(), 1)
        self.store._conn.execute("UPDATE seen SET frozen_rev=999999")   # nobody references it
        self.store._conn.commit()
        self.store.stop()
        again = self.open()
        self.assertEqual(again.stats()["history_rows"], 0)


class FirstDataTests(Case):
    def setUp(self):
        super().setUp()
        self.loader.records.pop((2, 2)), self.loader.timestamps.pop((2, 2))   # not on disk yet

    def test_data_within_grace_is_adopted(self):
        self.load("a", (2, 2))
        self.unload("a", (2, 2))
        self.assertEqual(self.store.pin_of("a", (2, 2))[0], 0)
        self.assertIsNone(self.top_for("a", 2, 2))
        self.t[0] += 30
        self.store.apply_snapshot(snap(helpers.make_record(2, 2, height=70)))
        self.assertEqual(self.top_for("a", 2, 2), 70, "first data after the unload")
        self.store.apply_diff(dict(PLACE, x=32, z=32, y=90))
        self.assertEqual(self.top_for("a", 2, 2), 70, "only the first data was adopted")

    def test_data_from_the_save_within_grace_is_adopted(self):
        self.load("a", (2, 2))
        self.unload("a", (2, 2))
        self.t[0] += 60
        self.loader.put(helpers.make_record(2, 2, height=66), ts=5)
        self.assertEqual(self.top_for("a", 2, 2), 66)

    def test_data_after_grace_is_not_attributed(self):
        self.load("a", (2, 2))
        self.unload("a", (2, 2))
        self.t[0] += map_store.FIRST_DATA_GRACE_S + 1
        self.store.apply_snapshot(snap(helpers.make_record(2, 2, height=70)))
        self.assertIsNone(self.top_for("a", 2, 2), "missing, not live")
        self.assertEqual(top(self.store.get_or_load(2, 2)), 70)
        self.load("a", (2, 2))
        self.assertEqual(self.top_for("a", 2, 2), 70)

    def test_data_while_loaded_is_live(self):
        self.load("a", (2, 2))
        self.store.apply_snapshot(snap(helpers.make_record(2, 2, height=70)))
        self.assertEqual(self.top_for("a", 2, 2), 70)


class PersistenceTests(Case):
    def test_frozen_data_survives_restart(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.unload("a", (0, 0))
        self.store.apply_diff(dict(PLACE))
        self.store.stop()
        again = self.open()
        self.assertEqual(top(self.view("a", again).get_or_load(0, 0)), 64)
        self.assertEqual(top(again.get_or_load(0, 0)), 70)
        again.apply_diff(dict(PLACE, y=80))
        self.assertEqual(top(self.view("a", again).get_or_load(0, 0)), 64)

    def test_loaded_at_shutdown_means_frozen_from_restart_on(self):
        self.load("a", (0, 0))
        self.top_for("a")
        self.store.apply_diff(dict(PLACE))
        self.store.stop()
        again = self.open()
        self.assertEqual(top(self.view("a", again).get_or_load(0, 0)), 70, "frozen at the state at restart")
        again.apply_diff(dict(PLACE, y=80))
        self.assertEqual(top(self.view("a", again).get_or_load(0, 0)), 70)
        again.report_chunks("a", [(0, 0)], [], replace=True)      # the client's heartbeat resync
        self.assertEqual(top(self.view("a", again).get_or_load(0, 0)), 80)

    def test_revisions_never_go_backwards(self):
        self.load("a", (0, 0))
        for i in range(5):
            self.store.apply_diff(dict(PLACE, y=70 + i))
        before = self.store._rev_counter
        self.store.stop()
        again = self.open()
        self.assertGreaterEqual(again._rev_counter, before)


class UpgradeTests(unittest.TestCase):
    def test_schema_upgrade_keeps_chunks_and_seen(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '4')")
        conn.execute(map_store.SCHEMA)
        conn.execute(map_store.SEEN_SCHEMA)
        rec = helpers.make_record(0, 0, height=64)
        conn.execute(
            "INSERT INTO chunks (cx, cz, surface_json, composition_json, updated_at, save_ts, loaded_at) "
            "VALUES (0, 0, ?, ?, 1.0, NULL, NULL)", (json.dumps(rec["surface"]), json.dumps(rec["composition"])))
        conn.execute("INSERT INTO seen (agent, dimension, cx, cz, first_seen, last_seen) "
                     "VALUES ('a', 'minecraft:overworld', 0, 0, 1.0, 2.0)")
        conn.commit()
        conn.close()
        store = map_store.MapStore(path, SimLoader())
        self.addCleanup(store.stop)
        self.assertEqual(store.discarded_cold, 0)
        self.assertEqual(store.stats()["cold_chunks"], 1)
        self.assertTrue(store.is_seen("a", 0, 0))
        view = store_view.StoreView(store, "a")
        self.assertEqual(top(view.get_or_load(0, 0)), 64)
        store.apply_diff(dict(PLACE))
        self.assertEqual(top(view.get_or_load(0, 0)), 70, "legacy agent (no unload reports) stays live")
        # the agent upgrades: what it had becomes frozen at the then-current revision
        store.report_chunks("a", [], [], replace=True)
        store.apply_diff(dict(PLACE, y=80))
        view = store_view.StoreView(store, "a")
        self.assertEqual(top(view.get_or_load(0, 0)), 70)
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(seen)")}
        self.assertTrue({"frozen_rev", "frozen_at"} <= cols)

    def test_new_database_has_the_same_columns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = map_store.MapStore(str(Path(tmp.name) / "n.db"), SimLoader())
        self.addCleanup(store.stop)
        self.assertIn("rev", {r[1] for r in store._conn.execute("PRAGMA table_info(chunks)")})
        self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM chunk_history").fetchone()[0], 0)


class IsolationTests(unittest.TestCase):
    """Two agents, same chunks, different views: no cache may serve one agent's view to the other."""

    def build(self, ld):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = map_store.MapStore(str(Path(tmp.name) / "m.db"), ld)
        self.addCleanup(store.stop)
        return store

    def two_agents(self, store, keys):
        keys = list(keys)
        for ag in ("A", "B"):
            store.report_chunks(ag, keys, [])
        for k in keys:
            store.get_or_load(*k)
        store.report_chunks("A", [], keys)        # A walks away; B stays

    def test_perception_landforms_route_isolated(self):
        base = flat
        lake = with_lake(flat)
        ld = SimLoader()
        box = range(-10, 11)
        for cx in box:
            for cz in box:
                ld.put(make_rec(cx, cz, base))
        store = self.build(ld)
        keys = [(cx, cz) for cx in box for cz in box]
        self.two_agents(store, keys)
        pristine_A = {
            "perception": json.dumps(perception.build_perception(store_view.StoreView(store, "A"), 0, 0, 16)["terrain"], sort_keys=True),
            "route": route.route(store_view.StoreView(store, "A"), -100, 0, 100, 0, rng=random.Random(1)),
        }
        pristine_A["route"].pop("ms", None)
        for cx, cz in keys:                           # the lake appears while A is away
            store.apply_snapshot(snap(make_rec(cx, cz, lake)))
        live = store_view.StoreView(store, "B")
        frozen = store_view.StoreView(store, "A")

        def snapshot_of(view):
            p = perception.build_perception(view, 0, 0, 16)
            lf = landforms.find_landforms(view, 0, 0, 96)
            rt = route.route(view, -100, 0, 100, 0, rng=random.Random(1))
            rt.pop("ms", None)
            return (json.dumps(p["terrain"], sort_keys=True), [f["kind"] for f in lf["landforms"]],
                    [f["kind"] for f in p["landforms"]], rt)

        a1 = snapshot_of(frozen)
        b1 = snapshot_of(live)
        a2 = snapshot_of(store_view.StoreView(store, "A"))
        b2 = snapshot_of(store_view.StoreView(store, "B"))
        self.assertEqual(a1, a2)
        self.assertEqual(b1, b2)
        self.assertNotEqual(a1[0], b1[0], "perception differs between the two views")
        self.assertEqual(a1[0], pristine_A["perception"], "A still sees the pre-lake world")
        self.assertNotIn("lake", a1[1] + a1[2])
        self.assertIn("lake", b1[1] + b1[2])
        self.assertEqual(a1[3], pristine_A["route"])
        self.assertNotEqual(a1[3]["waypoints"], b1[3]["waypoints"], "B detours around the lake, A does not")
        # live window (no agent) matches B
        self.assertEqual(json.dumps(perception.build_perception(store, 0, 0, 16)["terrain"], sort_keys=True), b1[0])

    def test_route_replanning_sees_newly_loaded_chunks_live(self):
        ld = SimLoader()
        box = range(-10, 11)
        for cx in box:
            for cz in box:
                ld.put(make_rec(cx, cz, flat))
        store = self.build(ld)
        keys = [(cx, cz) for cx in box for cz in box]
        self.two_agents(store, keys)
        for cx, cz in keys:
            store.apply_snapshot(snap(make_rec(cx, cz, with_lake(flat))))
        before = route.route(store_view.StoreView(store, "A"), -100, 0, 100, 0, rng=random.Random(1))
        store.report_chunks("A", keys, [])            # A walks back and loads everything again
        after = route.route(store_view.StoreView(store, "A"), -100, 0, 100, 0, rng=random.Random(1))
        self.assertNotEqual(before["waypoints"], after["waypoints"])
        live = route.route(store_view.StoreView(store, "B"), -100, 0, 100, 0, rng=random.Random(1))
        self.assertEqual(after["waypoints"], live["waypoints"])

    def test_settlements_and_survey_isolated(self):
        extras, surface, water = village(80, 80)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        world = helpers.build_structure_world(tmp.name, [(4, 4, 9, 9)], extras, surface=surface, water=water)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = map_store.MapStore(str(Path(tmp.name) / "m.db"), ld)
        self.addCleanup(store.stop)
        keys = [(cx, cz) for cx in range(4, 10) for cz in range(4, 10)]
        self.two_agents(store, keys)
        before = settlements.find_settlements(store_view.StoreView(store, "A"), 94, 92, 96)
        self.assertEqual(len(before["settlements"]), 1)
        walls_before = survey.survey(store_view.StoreView(store, "A"), 70, 70, 130, 130)["palette"]["walls"]
        self.assertTrue(walls_before)
        for cx, cz in keys:                           # the village is levelled while A is away
            store.apply_snapshot(snap(helpers.make_record(cx, cz, height=G)))
        for _ in range(2):
            a = settlements.find_settlements(store_view.StoreView(store, "A"), 94, 92, 96)
            b = settlements.find_settlements(store_view.StoreView(store, "B"), 94, 92, 96)
            self.assertEqual(len(a["settlements"]), 1)
            self.assertEqual(b["settlements"], [])
        sa = survey.survey(store_view.StoreView(store, "A"), 70, 70, 130, 130)
        sb = survey.survey(store_view.StoreView(store, "B"), 70, 70, 130, 130)
        self.assertEqual(sa["building_count"], 5)
        self.assertEqual(sb["building_count"], 0)
        self.assertFalse(sa["palette"]["walls"], "frozen chunks do not read the live save")
        again = settlements.find_settlements(store_view.StoreView(store, "A"), 94, 92, 96)
        self.assertEqual(again["settlements"], a["settlements"])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ld = SimLoader()
        for cx in range(0, 3):
            for cz in range(0, 3):
                cls.ld.put(helpers.make_record(cx, cz, height=64))
        cls.store = map_store.MapStore(str(Path(cls._tmp.name) / "m.db"), cls.ld)
        cls.api = TopographyAPI(cls.store, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(400):
            if cls.api.server is not None:
                break
            threading.Event().wait(0.01)
        cls.base = f"http://127.0.0.1:{cls.api.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.store.stop()
        cls._tmp.cleanup()

    def req(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_old_and_new_clients_over_http(self):
        st, res = self.req("POST", "/seen", body={"agent": "old", "dimension": "overworld", "chunks": [[0, 0]]})
        self.assertEqual(res, {"accepted": 1})
        st, res = self.req("POST", "/seen", body={"agent": "new", "dimension": "overworld",
                                                  "chunks": [[0, 0], [1, 0]], "unloaded": [], "replace": True})
        self.assertEqual(res, {"accepted": 2, "unloaded": 0})
        self.assertEqual(self.req("POST", "/seen", body={"agent": "new", "chunks": [], "unloaded": "x"})[0], 400)
        st, chunk = self.req("GET", "/chunk", {"cx": 0, "cz": 0, "agent": "new"})
        self.assertEqual(chunk["surface"]["y"][0], 64)
        st, res = self.req("POST", "/seen", body={"agent": "new", "dimension": "overworld",
                                                  "chunks": [], "unloaded": [[0, 0]]})
        self.assertEqual(res, {"accepted": 0, "unloaded": 1})
        self.req("POST", "/diff", body={"deltas": [dict(PLACE)]})
        st, chunk = self.req("GET", "/chunk", {"cx": 0, "cz": 0, "agent": "new"})
        self.assertEqual((chunk["surface"]["y"][0], chunk.get("frozen")), (64, True))
        st, rec = self.req("GET", "/recall", {"x": 1, "z": 1, "agent": "new"})
        self.assertEqual(st, 200)
        st, chunk = self.req("GET", "/chunk", {"cx": 0, "cz": 0, "agent": "old"})
        self.assertEqual(chunk["surface"]["y"][0], 70)
        st, chunk = self.req("GET", "/chunk", {"cx": 0, "cz": 0})
        self.assertEqual(chunk["surface"]["y"][0], 70)
        st, health = self.req("GET", "/health")
        self.assertIn("history_rows", health)


if __name__ == "__main__":
    unittest.main()
