"""terrain_y is NATURAL ground: roofs, decks, floors, walls and trunks are skipped."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import loader
import map_store
import settlements
import terrain_queries
import terrain_rules
from tests import helpers

G = helpers.FLAT_GROUND
BOX = [(0, 0, 3, 3)]


class WorldCase(unittest.TestCase):
    def make(self, extras, surface=None, water=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        world = helpers.build_structure_world(tmp.name, BOX, extras, surface=surface, water=water)
        ld = loader.ChunkLoader(world)
        self.addCleanup(ld.close)
        store = map_store.MapStore(str(Path(tmp.name) / "m.db"), ld)
        self.addCleanup(store.stop)
        return store

    @staticmethod
    def col(store, x, z):
        s = store.get_or_load(x >> 4, z >> 4)["surface"]
        i = (x & 15) + (z & 15) * 16
        return {k: s[k][i] for k in s}


def rect(x1, z1, x2, z2):
    return [(x, z) for x in range(x1, x2 + 1) for z in range(z1, z2 + 1)]


class RuleTests(unittest.TestCase):
    def test_whitelist_and_name_rules(self):
        yes = ["grass_block", "dirt", "coarse_dirt", "podzol", "farmland", "dirt_path", "mud",
               "clay", "sand", "red_sand", "gravel", "sandstone", "red_sandstone", "snow_block",
               "ice", "packed_ice", "stone", "granite", "tuff", "deepslate", "bedrock",
               "netherrack", "end_stone", "terracotta", "red_terracotta", "moss_block",
               "ancient_debris", "obsidian", "coal_ore", "deepslate_diamond_ore", "nether_gold_ore"]
        no = ["cobblestone", "mossy_cobblestone", "stone_bricks", "oak_planks", "oak_stairs",
              "oak_log", "oak_leaves", "white_wool", "white_concrete", "glass", "cut_sandstone",
              "smooth_sandstone", "chiseled_sandstone", "red_glazed_terracotta", "water",
              "air", "short_grass", "oak_fence", "oak_trapdoor", "torch"]
        for n in yes:
            self.assertTrue(terrain_rules.is_ground(f"minecraft:{n}"), n)
        for n in no:
            self.assertFalse(terrain_rules.is_ground(f"minecraft:{n}"), n)
        self.assertFalse(terrain_rules.is_ground(None))
        self.assertFalse(terrain_rules.is_ground(""))


class JavaSyncTests(unittest.TestCase):
    def test_java_whitelist_matches_when_plugin_is_present(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import check_terrain_rules_sync as sync
        if not sync.DEFAULT_JAVA.exists():
            self.skipTest("plugin checkout not next to the service")
        self.assertEqual(sync.compare(sync.DEFAULT_JAVA.read_text(encoding="utf-8")), [])

    def test_compare_detects_drift(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import check_terrain_rules_sync as sync
        src = ('GROUND_EXACT = prefixed("dirt");\n'
               'GROUND_SUFFIXES = {"_ore"};\n'
               'GROUND_SUFFIX_EXCLUDED = {"glazed_terracotta"};')
        self.assertTrue(sync.compare(src))


class DecodeTests(WorldCase):
    def test_plank_roof_over_grass(self):
        store = self.make({(5, 5): {G + 4: helpers.PLANKS}})
        c = self.col(store, 5, 5)
        self.assertEqual((c["y"], c["terrain_y"], c["terrain_materials"]), (G + 4, G, helpers.GRASS))

    def test_stair_roof(self):
        store = self.make({(5, 5): {G + 4: helpers.STAIRS, G + 5: "minecraft:oak_slab"}})
        c = self.col(store, 5, 5)
        self.assertEqual((c["y"], c["terrain_y"]), (G + 5, G))

    def test_bridge_deck_over_water_gives_bed(self):
        deck = {p: {G + 1: helpers.PLANKS} for p in rect(4, 4, 6, 9)}
        water = {p: G for p in deck}
        store = self.make(deck, surface={p: helpers.SAND for p in deck}, water=water)
        c = self.col(store, 5, 6)
        self.assertEqual(c["y"], G + 1)
        self.assertFalse(c["water"])
        self.assertEqual((c["terrain_y"], c["terrain_materials"]), (G - 2, helpers.SAND))

    def test_tree_gives_dirt_floor(self):
        extras = helpers.tree_extras(8, 8)
        store = self.make(extras, surface={p: helpers.DIRT for p in extras})
        for p in ((8, 8), (9, 8)):
            c = self.col(store, *p)
            self.assertEqual((c["terrain_y"], c["terrain_materials"]), (G, helpers.DIRT), p)

    def test_cobblestone_house_foundation(self):
        extras = helpers.house_extras(4, 4, 7, 7, wall=helpers.COBBLE, roof=helpers.COBBLE)
        store = self.make(extras)
        for p in ((4, 4), (7, 7), (5, 7)):
            c = self.col(store, *p)
            self.assertEqual((c["terrain_y"], c["terrain_materials"]), (G, helpers.GRASS), p)

    def test_ores_and_stone_at_cliff(self):
        extras = {(5, 5): {G + 1: "minecraft:stone", G + 2: "minecraft:stone",
                           G + 3: "minecraft:coal_ore"},
                  (6, 5): {G + 1: "minecraft:cobblestone", G + 2: "minecraft:cobblestone"}}
        store = self.make(extras)
        c = self.col(store, 5, 5)
        self.assertEqual((c["terrain_y"], c["terrain_materials"]), (G + 3, "minecraft:coal_ore"))
        self.assertEqual(self.col(store, 6, 5)["terrain_y"], G)

    def test_farmland_and_path_count(self):
        store = self.make({}, surface={(2, 2): "minecraft:farmland", (3, 3): helpers.PATH})
        self.assertEqual(self.col(store, 2, 2)["terrain_materials"], "minecraft:farmland")
        self.assertEqual(self.col(store, 3, 3)["terrain_materials"], helpers.PATH)
        self.assertEqual(self.col(store, 3, 3)["terrain_y"], G)

    def test_overhang_is_terrain_and_void_is_none(self):
        # a stone overhang above air is accepted as terrain; a column without any
        # whitelisted block has no terrain
        store = self.make({(5, 5): {G + 5: "minecraft:stone"},
                           (9, 9): {y: helpers.PLANKS for y in range(0, G + 1)}})
        self.assertEqual(self.col(store, 5, 5)["terrain_y"], G + 5)
        c = self.col(store, 9, 9)
        self.assertIsNone(c["terrain_y"])
        self.assertIsNone(c["terrain_materials"])
        self.assertEqual(c["y"], G)


class QueryTests(WorldCase):
    def test_flat_areas_skip_cobble_roof_and_plank_deck(self):
        roof = {p: {G + 6: helpers.COBBLE} for p in rect(8, 8, 25, 25)}
        deck = {p: {G + 1: helpers.PLANKS} for p in rect(34, 34, 47, 47)}
        extras = {**roof, **deck}
        water = {p: G for p in deck}
        store = self.make(extras, surface={p: helpers.SAND for p in deck}, water=water)
        res = terrain_queries.flat_areas(store, 28, 28, radius=40, width=5, depth=5)
        self.assertTrue(res["areas"])
        for a in res["areas"]:
            self.assertNotEqual(a["floor_material"], helpers.COBBLE)
            overlaps_roof = a["x1"] <= 25 and a["x2"] >= 8 and a["z1"] <= 25 and a["z2"] >= 8
            overlaps_deck = a["x1"] <= 47 and a["x2"] >= 34 and a["z1"] <= 47 and a["z2"] >= 34
            self.assertFalse(overlaps_roof, a)
            self.assertFalse(overlaps_deck, a)

    def test_bridge_deck_is_not_a_building(self):
        deck = {p: {G + 1: helpers.PLANKS} for p in rect(20, 30, 23, 45)}
        water = {p: G for p in rect(16, 28, 27, 47)}
        store = self.make(deck, surface={p: helpers.SAND for p in water}, water=water)
        recs = {(cx, cz): store.get_or_load(cx, cz) for cx in range(4) for cz in range(4)}
        buildings, _ = settlements.detect_buildings(recs)
        self.assertEqual(buildings, [])

    def test_building_height_is_above_own_terrain(self):
        extras = helpers.house_extras(10, 10, 7, 7)
        store = self.make(extras)
        recs = {(cx, cz): store.get_or_load(cx, cz) for cx in range(4) for cz in range(4)}
        buildings, _ = settlements.detect_buildings(recs)
        self.assertEqual(len(buildings), 1)
        self.assertEqual(buildings[0]["base_y"], G)


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        rec = helpers.make_record(0, 0, height=70)
        rec["surface"]["terrain_y"] = [70] * 256
        rec["surface"]["terrain_materials"] = ["minecraft:grass_block"] * 256
        self.store = map_store.MapStore(str(Path(self.tmp.name) / "m.db"), helpers.FakeLoader({(0, 0): rec}))
        self.addCleanup(self.store.stop)

    def surf(self):
        return self.store.get_chunk(0, 0)["surface"]

    def test_place_roof_keeps_terrain_and_stone_raises_it(self):
        for y in (72, 73):
            self.store.apply_diff({"action": "place", "x": 1, "y": y, "z": 1, "block_name": "minecraft:oak_stairs"})
        s = self.surf()
        self.assertEqual((s["y"][17], s["terrain_y"][17]), (73, 70))
        self.store.apply_diff({"action": "place", "x": 1, "y": 74, "z": 1, "block_name": "minecraft:stone"})
        self.assertEqual(self.surf()["terrain_y"][17], 74)

    def test_break_with_hint_and_legacy_break(self):
        self.store.apply_diff({
            "action": "break", "x": 2, "y": 70, "z": 2, "block_name": "minecraft:grass_block",
            "surface_y": 69, "new_top_y": 69, "new_top_block": "minecraft:dirt",
            "new_terrain_y": 69, "new_terrain_block": "minecraft:dirt"})
        s = self.surf()
        self.assertEqual((s["terrain_y"][34], s["terrain_materials"][34]), (69, "minecraft:dirt"))
        # legacy: no hint, ground below is planks -> terrain unknown until next snapshot
        self.store.apply_diff({
            "action": "break", "x": 3, "y": 70, "z": 3, "block_name": "minecraft:grass_block",
            "new_ground_y": 69, "new_ground_block": "minecraft:oak_planks"})
        self.assertIsNone(self.surf()["terrain_y"][51])
        # breaking a roof block above terrain never moves terrain
        self.store.apply_diff({"action": "break", "x": 4, "y": 75, "z": 4, "block_name": "minecraft:oak_planks"})
        self.assertEqual(self.surf()["terrain_y"][68], 70)


if __name__ == "__main__":
    unittest.main()
