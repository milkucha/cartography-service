"""Shared definition of "terrain ground" used by every writer of ``terrain_y``.

``terrain_y`` / ``terrain_materials`` (per-column, 256 entries per chunk) are the
height and block of the topmost *solid, non-liquid, non-vegetation, non-leaf* block.
Tree canopies, plants, snow layers and water are skipped, so under a forest this
is the forest floor, and under water it is the bed. Logs/planks/etc. count as
ground (a trunk or a roof is an obstacle, not nothing).

Both the save loader (``loader.decode_surface``) and the live diff/snapshot path
(``map_store``) must classify blocks through :func:`is_ground`.
"""

from __future__ import annotations

import mc_blocks

_VEGETATION = {
    "kelp", "kelp_plant", "seagrass", "tall_seagrass", "sea_pickle", "lily_pad",
    "short_grass", "grass", "tall_grass", "fern", "large_fern", "dead_bush",
    "sugar_cane", "bamboo", "cactus", "snow", "vine", "glow_lichen", "sculk_vein",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip",
    "orange_tulip", "white_tulip", "pink_tulip", "oxeye_daisy", "cornflower",
    "lily_of_the_valley", "wither_rose", "torchflower", "sunflower", "lilac",
    "rose_bush", "peony", "pink_petals", "spore_blossom", "hanging_roots",
    "brown_mushroom", "red_mushroom", "sweet_berry_bush", "fire", "soul_fire",
    "light", "structure_void", "moving_piston",
}
# Floor/wall decoration that sits ON the ground (or hangs from it) and must not
# raise the terrain height. Matched by name suffix; the Java snapshot code in
# topography-plugin (ColumnSurface.isGroundId) implements the SAME list - keep in sync.
DECORATION_SUFFIXES = (
    "rail", "torch", "sign", "banner", "button", "pressure_plate",
)
DECORATION_EXACT = {
    "minecraft:redstone_wire", "minecraft:lever", "minecraft:tripwire",
    "minecraft:tripwire_hook", "minecraft:repeater", "minecraft:comparator",
    "minecraft:ladder", "minecraft:string",
}

NON_GROUND = (
    {f"minecraft:{n}" for n in _VEGETATION}
    | set(mc_blocks.AIR)
    | set(mc_blocks.LIQUID)
)


def is_ground(name: str | None) -> bool:
    """True when ``name`` is a block that counts as terrain ground."""
    if not name:
        return False
    return (
        name not in NON_GROUND
        and name not in DECORATION_EXACT
        and not name.endswith("_leaves")
        and not name.endswith(DECORATION_SUFFIXES)
    )
