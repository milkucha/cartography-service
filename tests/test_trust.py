"""P0 trust tests: proto-chunk gating, save revalidation, diff accounting, snapshots."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import loader
import map_store
import terrain_rules
from api import TopographyAPI
from tests import helpers


def idx(x, z):
    return (x % 16) + (z % 16) * 16


def flat(x, z):
    return 66


def write_world(root, ground_fn=flat, status="minecraft:full", stamp=1000, chunks=((0, 0),)):
    region_dir = Path(root) / "world" / "region"
    roots = {}
    stamps = {}
    for cx, cz in chunks:
        roots[(cx, cz)] = helpers.build_chunk_root(cx, cz, ground_fn, status=status)
        stamps[(cx, cz)] = stamp
    helpers.write_region(region_dir / "r.0.0.mca", roots, timestamps=stamps)
    return Path(root) / "world"


class StoreCase(unittest.TestCase):
    def make_store(self, loader_obj, name="db.sqlite"):
        store = map_store.MapStore(str(Path(self.tmp.name) / name), loader_obj)
        self.addCleanup(self._safe_stop, store)
        return store

    @staticmethod
    def _safe_stop(store):
        try:
            store.stop()
        except Exception:
            pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class ProtoChunkTests(StoreCase):
    def test_proto_status_is_missing_and_never_cached(self):
        world = write_world(self.tmp.name, status="minecraft:carvers")
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        self.assertIsNone(ld.load_chunk(0, 0))
        store = self.make_store(ld)
        self.assertIsNone(store.get_or_load(0, 0))
        records, missing = store.touch_window(0, 0, 1)
        self.assertEqual((len(records), missing), (0, 1))
        self.assertEqual(store.stats()["warm_chunks"], 0)
        self.assertEqual(store.stats()["dirty_chunks"], 0)

    def test_proto_then_full_is_picked_up(self):
        world = write_world(self.tmp.name, status="minecraft:structure_starts")
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = self.make_store(ld)
        self.assertIsNone(store.get_or_load(0, 0))
        write_world(self.tmp.name, status="minecraft:full", stamp=2000)
        data = store.get_or_load(0, 0)
        self.assertIsNotNone(data)
        self.assertEqual(data["surface"]["ground_y"][0], 66)

    def test_missing_status_is_not_full(self):
        world = write_world(self.tmp.name, status=None)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        self.assertIsNone(ld.load_chunk(0, 0))

    def test_full_chunk_carries_save_ts(self):
        world = write_world(self.tmp.name, stamp=1234)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        record = ld.load_chunk(0, 0)
        self.assertEqual(record["save_ts"], 1234)
        self.assertEqual(ld.chunk_timestamp(0, 0), 1234)
        self.assertIsNone(ld.chunk_timestamp(5, 5))
        self.assertTrue(ld.world_id())


class RevalidationTests(StoreCase):
    def test_newer_save_replaces_cold_and_replays_only_later_diffs(self):
        world = write_world(self.tmp.name, flat, stamp=1000)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = self.make_store(ld)
        clock = [1100.0]
        store._clock = lambda: clock[0]

        store.get_or_load(0, 0)
        # diff A is received before the next save, diff B after it.
        store.apply_diff({"action": "place", "x": 1, "y": 80, "z": 1, "block_name": "minecraft:stone"})
        clock[0] = 2500.0
        store.apply_diff({"action": "place", "x": 2, "y": 81, "z": 2, "block_name": "minecraft:stone"})
        store.flush()
        store._hot.clear()
        store._warm.clear()

        # The server saved the chunk at t=2000 (ground 70); that save contains diff A.
        write_world(self.tmp.name, lambda x, z: 70, stamp=2000)
        data = store.get_chunk(0, 0)
        self.assertEqual(data["source"], "base")
        self.assertEqual(data["save_ts"], 2000)
        self.assertEqual(data["surface"]["ground_y"][idx(5, 5)], 70)
        self.assertEqual(data["surface"]["y"][idx(1, 1)], 70)  # diff A dropped (in the save)
        self.assertEqual(data["surface"]["y"][idx(2, 2)], 81)  # diff B replayed
        self.assertEqual(store.stats()["reloaded_stale"], 1)

    def test_unchanged_save_keeps_diffs(self):
        world = write_world(self.tmp.name, flat, stamp=1000)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = self.make_store(ld)
        store.apply_diff({"action": "place", "x": 1, "y": 80, "z": 1, "block_name": "minecraft:stone"})
        store.flush()
        store._warm.clear()
        data = store.get_chunk(0, 0)
        self.assertEqual(data["source"], "cold")
        self.assertEqual(data["surface"]["y"][idx(1, 1)], 80)

    def test_hot_record_is_revalidated_too(self):
        world = write_world(self.tmp.name, flat, stamp=1000)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = self.make_store(ld)
        store.touch_window(0, 0, 1)
        write_world(self.tmp.name, lambda x, z: 72, stamp=3000)
        records, _ = store.touch_window(0, 0, 1)
        self.assertEqual(records[0]["surface"]["ground_y"][0], 72)
        self.assertIn((0, 0), store._hot)


class ColdValidationTests(StoreCase):
    def _fill_and_close(self, world_id):
        store = map_store.MapStore(
            str(Path(self.tmp.name) / "db.sqlite"),
            helpers.FakeLoader({(0, 0): helpers.make_record(0, 0)}, world_id=world_id),
        )
        store.get_or_load(0, 0)
        store.flush()
        store.stop()

    def test_world_change_discards_cold_rows(self):
        self._fill_and_close("A")
        reopened = map_store.MapStore(
            str(Path(self.tmp.name) / "db.sqlite"), helpers.FakeLoader({}, world_id="B")
        )
        self.addCleanup(reopened.stop)
        self.assertEqual(reopened.discarded_cold, 1)
        self.assertEqual(reopened.stats()["cold_chunks"], 0)

    def test_same_world_keeps_cold_rows(self):
        self._fill_and_close("A")
        reopened = map_store.MapStore(
            str(Path(self.tmp.name) / "db.sqlite"), helpers.FakeLoader({}, world_id="A")
        )
        self.addCleanup(reopened.stop)
        self.assertEqual(reopened.discarded_cold, 0)
        self.assertEqual(reopened.stats()["cold_chunks"], 1)

    def test_legacy_schema_is_discarded(self):
        path = Path(self.tmp.name) / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE chunks (cx INTEGER, cz INTEGER, surface_json TEXT, "
            "composition_json TEXT, updated_at REAL, PRIMARY KEY (cx, cz))"
        )
        conn.execute("INSERT INTO chunks VALUES (0, 0, '{}', '{}', 1.0)")
        conn.commit()
        conn.close()
        store = map_store.MapStore(str(path), None)
        self.addCleanup(store.stop)
        self.assertEqual(store.discarded_cold, 1)
        self.assertIsNone(store.get_chunk(0, 0))

    def test_updated_at_is_not_save_ts(self):
        store = self.make_store(
            helpers.FakeLoader({(0, 0): helpers.make_record(0, 0)}, timestamps={(0, 0): 555})
        )
        store.get_or_load(0, 0)
        store.flush()
        store._warm.clear()
        data = store.get_chunk(0, 0)
        self.assertEqual(data["save_ts"], 555)
        self.assertNotEqual(data["updated_at"], 555)
        self.assertIsNotNone(data["loaded_at"])


class DiffCorrectnessTests(StoreCase):
    def setUp(self):
        super().setUp()
        rec = helpers.make_record(0, 0, height=70)
        rec["surface"]["terrain_y"] = [70] * 256
        rec["surface"]["terrain_materials"] = ["minecraft:grass_block"] * 256
        self.store = self.make_store(helpers.FakeLoader({(0, 0): rec}))

    def surf(self):
        return self.store.get_chunk(0, 0)["surface"]

    def test_underground_break_does_not_lower_surface_legacy(self):
        # Old plugin: reports the block below the hole as the "new top".
        self.store.apply_diff({
            "action": "break", "x": 3, "y": 60, "z": 4, "block_name": "minecraft:stone",
            "new_top_y": 59, "new_top_block": "minecraft:stone",
            "new_ground_y": 59, "new_ground_block": "minecraft:stone",
        })
        s = self.surf()
        i = idx(3, 4)
        self.assertEqual(s["y"][i], 70)
        self.assertEqual(s["materials"][i], "minecraft:grass_block")
        self.assertEqual(s["ground_y"][i], 70)
        self.assertEqual(s["terrain_y"][i], 70)

    def test_underground_break_with_trusted_surface_y(self):
        self.store.apply_diff({
            "action": "break", "x": 3, "y": 60, "z": 4, "block_name": "minecraft:stone",
            "surface_y": 70, "new_top_y": 70, "new_top_block": "minecraft:grass_block",
            "new_ground_y": 70, "new_ground_block": "minecraft:grass_block",
            "new_terrain_y": 70, "new_terrain_block": "minecraft:grass_block",
        })
        s = self.surf()
        self.assertEqual((s["y"][idx(3, 4)], s["terrain_y"][idx(3, 4)]), (70, 70))

    def test_surface_break_lowers_with_hints_and_terrain(self):
        self.store.apply_diff({
            "action": "break", "x": 3, "y": 70, "z": 4, "block_name": "minecraft:grass_block",
            "surface_y": 69, "new_top_y": 69, "new_top_block": "minecraft:dirt",
            "new_ground_y": 69, "new_ground_block": "minecraft:dirt",
            "new_terrain_y": 69, "new_terrain_block": "minecraft:dirt",
        })
        s = self.surf()
        i = idx(3, 4)
        self.assertEqual((s["y"][i], s["materials"][i]), (69, "minecraft:dirt"))
        self.assertEqual((s["terrain_y"][i], s["terrain_materials"][i]), (69, "minecraft:dirt"))

    def test_place_updates_terrain_only_for_ground_blocks(self):
        self.store.apply_diff({"action": "place", "x": 1, "y": 71, "z": 1, "block_name": "minecraft:oak_leaves"})
        self.store.apply_diff({"action": "place", "x": 2, "y": 71, "z": 2, "block_name": "minecraft:oak_planks"})
        s = self.surf()
        self.assertEqual(s["y"][idx(1, 1)], 71)
        self.assertEqual(s["terrain_y"][idx(1, 1)], 70)
        self.assertEqual(s["terrain_y"][idx(2, 2)], 71)
        self.assertTrue(terrain_rules.is_ground(s["terrain_materials"][idx(2, 2)]))

    def test_old_block_is_decremented_on_place(self):
        before = self.store.get_or_load(0, 0)["composition"]["counts"]["natural"]
        self.store.apply_diff({
            "action": "place", "x": 1, "y": 70, "z": 1,
            "block_name": "minecraft:oak_planks", "old_block": "minecraft:grass_block",
        })
        counts = self.store.get_chunk(0, 0)["composition"]["counts"]
        self.assertEqual(counts["natural"], before - 1)
        self.assertEqual(counts["engineered"], 1)

    def test_same_block_delta_is_ignored(self):
        gen = self.store.generation
        self.assertTrue(self.store.apply_diff({
            "action": "place", "x": 1, "y": 70, "z": 1,
            "block_name": "minecraft:oak_door", "old_block": "minecraft:oak_door",
        }))
        self.assertEqual(self.store.generation, gen)

    def test_strong_indicator_kept_while_any_remain(self):
        for x in (1, 2, 3):
            self.store.apply_diff({"action": "place", "x": x, "y": 71, "z": 1, "block_name": "minecraft:chest"})
        for x in (1, 2):
            self.store.apply_diff({"action": "break", "x": x, "y": 71, "z": 1, "block_name": "minecraft:chest"})
        comp = self.store.get_chunk(0, 0)["composition"]
        self.assertIn("minecraft:chest", comp["strong_indicators"])
        self.assertEqual(comp["strong_counts"]["minecraft:chest"], 1)
        self.store.apply_diff({"action": "break", "x": 3, "y": 71, "z": 1, "block_name": "minecraft:chest"})
        comp = self.store.get_chunk(0, 0)["composition"]
        self.assertNotIn("minecraft:chest", comp["strong_indicators"])

    def test_non_overworld_deltas_ignored(self):
        gen = self.store.generation
        self.store.apply_diff({
            "action": "place", "x": 1, "y": 99, "z": 1,
            "block_name": "minecraft:stone", "dimension": "minecraft:the_nether",
        })
        self.assertEqual(self.store.generation, gen)


def make_snapshot(y=65):
    return {
        "cx": 0, "cz": 0,
        "surface": {
            "y": [y] * 256, "materials": ["minecraft:grass_block"] * 256,
            "ground_y": [y] * 256, "ground_materials": ["minecraft:grass_block"] * 256,
            "water": [False] * 256,
            "terrain_y": [y] * 256, "terrain_materials": ["minecraft:grass_block"] * 256,
        },
        "counts": {"engineered": 7},
    }


class SnapshotTests(StoreCase):
    def test_snapshot_replaces_and_is_idempotent(self):
        store = self.make_store(helpers.FakeLoader({(0, 0): helpers.make_record(0, 0, height=64)}))
        gen = store.generation
        self.assertEqual(store.apply_snapshots([make_snapshot()]), (1, 0, 0))
        self.assertEqual(store.apply_snapshots([make_snapshot()]), (1, 0, 0))
        data = store.get_chunk(0, 0)
        self.assertEqual(data["surface"]["y"][5], 65)
        self.assertEqual(data["surface"]["terrain_y"][5], 65)
        self.assertEqual(data["composition"]["counts"]["engineered"], 7)
        self.assertEqual(store.generation, gen + 2)
        self.assertIn((0, 0), store._dirty)

    def test_snapshot_for_unknown_chunk_and_bad_shapes(self):
        store = self.make_store(None)
        bad = make_snapshot()
        bad["surface"]["y"] = [1, 2]
        nether = make_snapshot()
        nether["dimension"] = "minecraft:the_nether"
        self.assertEqual(store.apply_snapshots([make_snapshot(), bad, nether, "x"]), (1, 2, 1))
        self.assertIsNotNone(store.get_chunk(0, 0))

    def test_snapshot_survives_newer_save_if_received_later(self):
        world = write_world(self.tmp.name, flat, stamp=1000)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = self.make_store(ld)
        store._clock = lambda: 5000.0
        store.apply_snapshots([make_snapshot(90)])
        write_world(self.tmp.name, flat, stamp=2000)
        data = store.get_chunk(0, 0)
        self.assertEqual(data["save_ts"], 2000)
        self.assertEqual(data["surface"]["y"][3], 90)


class ApiTrustTests(StoreCase):
    def setUp(self):
        super().setUp()
        store = self.make_store(helpers.FakeLoader({(0, 0): helpers.make_record(0, 0)}))
        self.api = TopographyAPI(store, "127.0.0.1", 0)
        thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        thread.start()
        for _ in range(400):
            if self.api.server is not None:
                break
            time.sleep(0.005)
        self.addCleanup(self.api.shutdown)
        self.base = f"http://127.0.0.1:{self.api.server.server_address[1]}"

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read())

    def test_chunk_snapshot_endpoint(self):
        out = self.req(
            "POST", "/chunk_snapshot",
            {"dimension": "minecraft:overworld", "snapshots": [make_snapshot(77)]},
        )
        self.assertEqual(out, {"accepted": 1, "rejected": 0, "ignored": 0})
        chunk = self.req("GET", "/chunk?cx=0&cz=0")
        self.assertEqual(chunk["surface"]["y"][0], 77)

    def test_non_overworld_queries_are_unavailable(self):
        out = self.req("GET", "/perception?x=0&z=0&dimension=minecraft:the_nether")
        self.assertEqual(
            out,
            {"service": "topography", "unavailable": "dimension", "dimension": "minecraft:the_nether"},
        )
        out = self.req("POST", "/position", {"x": 0, "z": 0, "dimension": "the_end"})
        self.assertEqual(out["unavailable"], "dimension")
        ok = self.req("GET", "/perception?x=0&z=0&dimension=overworld&size_chunks=1")
        self.assertNotIn("unavailable", ok)

    def test_diff_with_nether_dimension_is_ignored(self):
        out = self.req("POST", "/diff", {"dimension": "minecraft:the_nether", "deltas": [
            {"action": "place", "x": 1, "y": 99, "z": 1, "block_name": "minecraft:stone"}]})
        self.assertEqual(out["accepted"], 0)
        self.assertEqual(out["ignored"], 1)


if __name__ == "__main__":
    unittest.main()


class TerrainRulesDecorationTests(unittest.TestCase):
    def test_decoration_never_counts_as_ground(self):
        import terrain_rules
        for name in ("minecraft:rail", "minecraft:powered_rail", "minecraft:torch",
                     "minecraft:oak_sign", "minecraft:stone_button", "minecraft:redstone_wire",
                     "minecraft:oak_leaves", "minecraft:water"):
            self.assertFalse(terrain_rules.is_ground(name), name)
        for name in ("minecraft:stone", "minecraft:oak_log", "minecraft:oak_planks",
                     "minecraft:grass_block"):
            self.assertTrue(terrain_rules.is_ground(name), name)
