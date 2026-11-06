"""Shared definition of "terrain ground" used by every writer of ``terrain_y``.

``terrain_y`` / ``terrain_materials`` (per-column, 256 entries per chunk) are the
height and block of the topmost NATURAL GROUND-FORMING block. The scan goes down
from the top of the column and skips everything that is not on the whitelist:
roofs, floors, bridge decks, walls, logs, leaves, plants, liquids, decoration.
So under a plank roof it is the grass below, under a tree it is the dirt, and
under water or a bridge deck it is the bed.

Whitelist = :data:`GROUND_EXACT` plus the name rules (``*_ore``, ``*_terracotta``
except glazed). ``dirt_path`` and ``farmland`` are included although a player
makes them: they replace the top dirt/grass block, so they sit exactly at
natural ground level. Known limitation: stone-family blocks (stone, granite,
deepslate, ...) that a player placed as a pillar or floor still count, and a
natural overhang counts as terrain (no air-gap/cave detection). A column with no
whitelisted block at all (e.g. a floating build over the void) has no terrain.

Both the save loader (``loader.decode_surface``) and the live diff/snapshot path
(``map_store``) classify blocks through :func:`is_ground`. The Java plugin
(``ColumnSurface.isGroundId``) implements the SAME list - keep in sync;
``tools/check_terrain_rules_sync.py`` verifies it.
"""

from __future__ import annotations

GROUND_EXACT_NAMES = (
    "grass_block", "dirt", "coarse_dirt", "rooted_dirt", "podzol", "mycelium",
    "dirt_path", "farmland", "mud", "clay", "sand", "red_sand", "gravel",
    "sandstone", "red_sandstone", "snow_block", "powder_snow",
    "ice", "packed_ice", "blue_ice",
    "stone", "granite", "diorite", "andesite", "tuff", "calcite", "deepslate",
    "dripstone_block", "basalt", "blackstone", "netherrack", "end_stone",
    "bedrock", "terracotta", "moss_block", "ancient_debris", "obsidian",
    "soul_sand", "soul_soil", "crimson_nylium", "warped_nylium",
)
GROUND_EXACT = frozenset(f"minecraft:{n}" for n in GROUND_EXACT_NAMES)
# Name rules (checked on the full id).
GROUND_SUFFIXES = ("_ore", "_terracotta")
GROUND_SUFFIX_EXCLUDED = ("glazed_terracotta",)


def is_ground(name: str | None) -> bool:
    """True when ``name`` is a natural ground-forming block (terrain)."""
    if not name:
        return False
    if name in GROUND_EXACT:
        return True
    if name.endswith(GROUND_SUFFIX_EXCLUDED):
        return False
    return name.endswith(GROUND_SUFFIXES)
