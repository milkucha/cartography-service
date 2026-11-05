"""Heuristic classification of block names into natural vs. engineered
(player-built), for composition-based building/city detection.

Copied from the Archeology tools. Rule-based rather than an exhaustive
per-block enumeration: easier to audit, and unrecognized namespaced IDs
(modded blocks) fall through to "unknown" rather than being silently
misclassified.
"""

AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
LIQUID = {"minecraft:water", "minecraft:lava", "minecraft:bubble_column"}

# Explicit one-off blocks that generate naturally but don't fit a clean
# suffix rule (includes blocks that LOOK engineered but occur naturally,
# e.g. colored terracotta strata in badlands, and mined-not-crafted forms).
NATURAL_EXACT = {
    "minecraft:stone", "minecraft:granite", "minecraft:diorite", "minecraft:andesite",
    "minecraft:deepslate", "minecraft:cobblestone", "minecraft:cobbled_deepslate",
    "minecraft:tuff", "minecraft:calcite", "minecraft:dripstone_block",
    "minecraft:pointed_dripstone", "minecraft:amethyst_block", "minecraft:budding_amethyst",
    "minecraft:moss_block", "minecraft:moss_carpet", "minecraft:mud", "minecraft:clay",
    "minecraft:dirt", "minecraft:coarse_dirt", "minecraft:rooted_dirt", "minecraft:grass_block",
    "minecraft:podzol", "minecraft:mycelium", "minecraft:sand", "minecraft:red_sand",
    "minecraft:sandstone", "minecraft:red_sandstone", "minecraft:gravel",
    "minecraft:netherrack", "minecraft:soul_sand", "minecraft:soul_soil",
    "minecraft:basalt", "minecraft:blackstone", "minecraft:gilded_blackstone",
    "minecraft:magma_block", "minecraft:end_stone", "minecraft:obsidian",
    "minecraft:crying_obsidian", "minecraft:ice", "minecraft:packed_ice", "minecraft:blue_ice",
    "minecraft:snow", "minecraft:snow_block", "minecraft:powder_snow",
    "minecraft:kelp", "minecraft:kelp_plant", "minecraft:seagrass", "minecraft:tall_seagrass",
    "minecraft:sea_pickle", "minecraft:vine", "minecraft:cave_vines", "minecraft:cave_vines_plant",
    "minecraft:glow_lichen", "minecraft:spore_blossom", "minecraft:big_dripleaf",
    "minecraft:big_dripleaf_stem", "minecraft:small_dripleaf",
    "minecraft:short_grass", "minecraft:grass", "minecraft:tall_grass", "minecraft:fern",
    "minecraft:large_fern", "minecraft:dead_bush", "minecraft:azalea", "minecraft:flowering_azalea",
    "minecraft:bamboo", "minecraft:sugar_cane", "minecraft:cactus", "minecraft:lily_pad",
    "minecraft:brown_mushroom", "minecraft:red_mushroom",
    "minecraft:brown_mushroom_block", "minecraft:red_mushroom_block", "minecraft:mushroom_stem",
    "minecraft:sculk", "minecraft:sculk_vein", "minecraft:sculk_catalyst",
    "minecraft:sculk_shrieker", "minecraft:sculk_sensor", "minecraft:glowstone",
    "minecraft:shroomlight", "minecraft:nether_wart_block", "minecraft:warped_wart_block",
    "minecraft:bee_nest", "minecraft:cobweb",
}
for _color in ("", "orange_", "magenta_", "light_blue_", "yellow_", "lime_", "pink_", "gray_",
               "light_gray_", "cyan_", "purple_", "blue_", "brown_", "green_", "red_", "black_"):
    NATURAL_EXACT.add(f"minecraft:{_color}terracotta")

# Ores occur naturally regardless of stone/deepslate variant.
for _ore in ("coal", "iron", "copper", "gold", "redstone", "lapis", "diamond", "emerald"):
    NATURAL_EXACT.add(f"minecraft:{_ore}_ore")
    NATURAL_EXACT.add(f"minecraft:deepslate_{_ore}_ore")
NATURAL_EXACT.add("minecraft:nether_gold_ore")
NATURAL_EXACT.add("minecraft:nether_quartz_ore")
NATURAL_EXACT.add("minecraft:ancient_debris")

ENGINEERED_EXACT = {
    "minecraft:crafting_table", "minecraft:smithing_table", "minecraft:cartography_table",
    "minecraft:fletching_table", "minecraft:loom", "minecraft:stonecutter",
    "minecraft:grindstone", "minecraft:lectern", "minecraft:barrel", "minecraft:chest",
    "minecraft:trapped_chest", "minecraft:ender_chest", "minecraft:hopper",
    "minecraft:dispenser", "minecraft:dropper", "minecraft:brewing_stand", "minecraft:cauldron",
    "minecraft:water_cauldron", "minecraft:lava_cauldron", "minecraft:powder_snow_cauldron",
    "minecraft:composter", "minecraft:anvil", "minecraft:chipped_anvil", "minecraft:damaged_anvil",
    "minecraft:ladder", "minecraft:scaffolding", "minecraft:iron_bars", "minecraft:chain",
    "minecraft:torch", "minecraft:wall_torch", "minecraft:soul_torch", "minecraft:soul_wall_torch",
    "minecraft:lantern", "minecraft:soul_lantern", "minecraft:redstone_lamp",
    "minecraft:sea_lantern", "minecraft:campfire", "minecraft:soul_campfire",
    "minecraft:note_block", "minecraft:jukebox", "minecraft:target", "minecraft:lightning_rod",
    "minecraft:bell", "minecraft:flower_pot", "minecraft:cake", "minecraft:bookshelf",
    "minecraft:chiseled_bookshelf", "minecraft:beacon", "minecraft:conduit",
    "minecraft:respawn_anchor", "minecraft:lodestone", "minecraft:end_rod",
    "minecraft:redstone_wire", "minecraft:redstone_torch", "minecraft:redstone_wall_torch",
    "minecraft:repeater", "minecraft:comparator", "minecraft:piston", "minecraft:sticky_piston",
    "minecraft:piston_head", "minecraft:moving_piston", "minecraft:observer",
    "minecraft:tripwire", "minecraft:tripwire_hook", "minecraft:daylight_detector",
    "minecraft:rail", "minecraft:powered_rail", "minecraft:detector_rail", "minecraft:activator_rail",
    "minecraft:smooth_stone", "minecraft:smooth_sandstone", "minecraft:smooth_red_sandstone",
    "minecraft:smooth_quartz", "minecraft:smooth_basalt", "minecraft:cut_sandstone",
    "minecraft:cut_red_sandstone", "minecraft:bricks", "minecraft:nether_bricks",
    "minecraft:red_nether_bricks", "minecraft:end_stone_bricks", "minecraft:purpur_block",
    "minecraft:purpur_pillar", "minecraft:quartz_block", "minecraft:quartz_pillar",
    "minecraft:quartz_bricks", "minecraft:prismarine", "minecraft:prismarine_bricks",
    "minecraft:dark_prismarine", "minecraft:mud_bricks", "minecraft:packed_mud",
    "minecraft:honeycomb_block", "minecraft:tinted_glass", "minecraft:glass",
    "minecraft:hay_block", "minecraft:composter", "minecraft:calibrated_sculk_sensor",
    "minecraft:raw_iron_block", "minecraft:raw_copper_block", "minecraft:raw_gold_block",
    "minecraft:iron_block", "minecraft:gold_block", "minecraft:diamond_block",
    "minecraft:emerald_block", "minecraft:lapis_block", "minecraft:redstone_block",
    "minecraft:coal_block", "minecraft:netherite_block", "minecraft:copper_block",
    "minecraft:exposed_copper", "minecraft:weathered_copper", "minecraft:oxidized_copper",
    "minecraft:waxed_copper_block", "minecraft:waxed_exposed_copper",
    "minecraft:waxed_weathered_copper", "minecraft:waxed_oxidized_copper",
    "minecraft:cut_copper", "minecraft:exposed_cut_copper", "minecraft:weathered_cut_copper",
    "minecraft:oxidized_cut_copper",
}

ENGINEERED_SUFFIXES = (
    "_planks", "_stairs", "_slab", "_wall", "_fence", "_fence_gate", "_door", "_trapdoor",
    "_wool", "_carpet", "_concrete", "_concrete_powder", "_stained_glass", "_stained_glass_pane",
    "_bed", "_banner", "_wall_banner", "_sign", "_wall_sign", "_hanging_sign",
    "_wall_hanging_sign", "_button", "_pressure_plate", "_glazed_terracotta",
    "_bricks", "_brick_stairs", "_brick_slab", "_brick_wall", "_boat", "_chest_boat",
    "_shulker_box",
)
ENGINEERED_PREFIXES = (
    "polished_", "cut_", "smooth_", "chiseled_", "cracked_", "mossy_", "stripped_",
    "waxed_", "infested_",
)


def classify_block(name: str) -> str:
    """Returns "air", "liquid", "natural", "engineered", or "unknown"."""
    if name in AIR:
        return "air"
    if name in LIQUID:
        return "liquid"
    if name in NATURAL_EXACT:
        return "natural"
    if name in ENGINEERED_EXACT:
        return "engineered"
    if name.startswith("minecraft:glass") or name == "minecraft:glass_pane":
        return "engineered"
    if name.endswith(ENGINEERED_SUFFIXES):
        return "engineered"
    if name.startswith(ENGINEERED_PREFIXES):
        return "engineered"
    if name.endswith("_log") or name.endswith("_wood"):
        return "natural"
    if name.endswith("_leaves"):
        return "natural"
    if name.endswith("_shulker_box"):
        return "engineered"
    return "unknown"


# Blocks that are a strong "someone built here" signal even in tiny
# quantities, regardless of the overall engineered/natural ratio.
STRONG_INDICATOR_EXACT = {
    "minecraft:chest", "minecraft:trapped_chest", "minecraft:barrel", "minecraft:furnace",
    "minecraft:blast_furnace", "minecraft:smoker", "minecraft:brewing_stand",
    "minecraft:crafting_table", "minecraft:smithing_table", "minecraft:cartography_table",
    "minecraft:fletching_table", "minecraft:loom", "minecraft:stonecutter",
    "minecraft:grindstone", "minecraft:lectern", "minecraft:anvil", "minecraft:chipped_anvil",
    "minecraft:damaged_anvil", "minecraft:note_block", "minecraft:jukebox", "minecraft:bell",
    "minecraft:composter", "minecraft:cauldron", "minecraft:water_cauldron",
    "minecraft:lava_cauldron", "minecraft:campfire", "minecraft:soul_campfire",
    "minecraft:torch", "minecraft:wall_torch", "minecraft:soul_torch", "minecraft:soul_wall_torch",
    "minecraft:lantern", "minecraft:soul_lantern", "minecraft:ladder", "minecraft:rail",
    "minecraft:powered_rail", "minecraft:detector_rail", "minecraft:activator_rail",
    "minecraft:redstone_wire", "minecraft:repeater", "minecraft:comparator", "minecraft:piston",
    "minecraft:sticky_piston", "minecraft:observer", "minecraft:tripwire_hook",
    "minecraft:daylight_detector", "minecraft:beacon", "minecraft:conduit",
    "minecraft:respawn_anchor", "minecraft:lodestone", "minecraft:end_rod",
    "minecraft:scaffolding", "minecraft:chain", "minecraft:iron_bars", "minecraft:flower_pot",
    "minecraft:cake", "minecraft:hopper", "minecraft:dispenser", "minecraft:dropper",
}
STRONG_INDICATOR_SUFFIXES = (
    "_door", "_trapdoor", "_fence_gate", "_sign", "_wall_sign", "_hanging_sign",
    "_wall_hanging_sign", "_banner", "_wall_banner", "_bed", "_shulker_box",
)


def is_strong_indicator(name: str) -> bool:
    return name in STRONG_INDICATOR_EXACT or name.endswith(STRONG_INDICATOR_SUFFIXES)


# Blocks worth tracking at exact positions (not just per-chunk counts) for
# line-tracing infrastructure like railroads.
RAIL_TYPES = {
    "minecraft:rail", "minecraft:powered_rail",
    "minecraft:detector_rail", "minecraft:activator_rail",
}
LINE_TRACKED_BLOCKS = frozenset(RAIL_TYPES)


# Vegetation/growth types that can indicate a structure has gone
# unmaintained and nature is reclaiming it.
DECAY_INDICATOR_EXACT = {
    "minecraft:vine", "minecraft:cave_vines", "minecraft:cave_vines_plant",
    "minecraft:moss_block", "minecraft:moss_carpet", "minecraft:glow_lichen",
    "minecraft:cobweb", "minecraft:brown_mushroom", "minecraft:red_mushroom",
    "minecraft:brown_mushroom_block", "minecraft:red_mushroom_block",
}


def is_decay_indicator(name: str) -> bool:
    return name in DECAY_INDICATOR_EXACT
