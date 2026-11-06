"""Compact LOD perception summaries for automatic prompt injection.

The perception window is a square of chunk columns centred on the player's
chunk.  It is summarised as a pyramid of overlapping square LOD levels: the
inner ``inner`` chunks are reported one chunk per cell, and each successive
level doubles the cell size while keeping the same number of cells per side.
Consumers (and :func:`unique_level_cells`) emit the innermost level in full
and only the outer ring of each larger level, so the prompt has no duplicate
cells.

Only the standard library is used.
"""

from __future__ import annotations

import math
import time
from collections import Counter

MAX_PERCEPTION_CHUNKS = 1024
DEFAULT_SIZE_CHUNKS = 16
INNER_CHUNKS = 4
TOP_BLOCKS_LIMIT = 6

_DIRECTIONS = (
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
    "north",
    "northeast",
)


def window_bounds(center_cx: int, center_cz: int, size_chunks: int):
    """Return ``(cx1, cz1, cx2, cz2)`` for a centred chunk window.

    ``half = size // 2`` means even windows extend one chunk further on the
    negative side (16x16 -> 8 chunks negative, 7 positive).
    """
    half = int(size_chunks) // 2
    cx1 = int(center_cx) - half
    cz1 = int(center_cz) - half
    return cx1, cz1, cx1 + int(size_chunks) - 1, cz1 + int(size_chunks) - 1


def lod_levels(cx1: int, cz1: int, size: int, inner: int = INNER_CHUNKS):
    """Build the overlapping square LOD levels for a window.

    Each level keeps ``cells_per_side = inner`` and doubles ``cell_chunks``
    (1, 2, 4, ...), clipped to the largest aligned level that fits inside the
    requested window.  Sizes smaller than ``inner`` produce a single level.
    """
    size = max(1, int(size))
    inner = max(1, min(int(inner), size))
    center_cx = int(cx1) + size // 2
    center_cz = int(cz1) + size // 2

    levels = []
    cell_chunks = 1
    side = inner
    while side <= size:
        half = side // 2
        levels.append(
            {
                "cell_chunks": cell_chunks,
                "cells_per_side": inner,
                "cx1": center_cx - half,
                "cz1": center_cz - half,
            }
        )
        cell_chunks *= 2
        side *= 2
    return levels


def unique_level_cells(levels):
    """Return the de-duplicated cells a prompt formatter should emit.

    The first level is emitted in full; every later level contributes only its
    outer ring.  Each entry is ``(cx, cz, cell_chunks)``.
    """
    cells = []
    for index, level in enumerate(levels):
        cells_per_side = level["cells_per_side"]
        cell_chunks = level["cell_chunks"]
        x0 = level["cx1"]
        z0 = level["cz1"]
        for row in range(cells_per_side):
            for col in range(cells_per_side):
                if index and 0 < row < cells_per_side - 1 and 0 < col < cells_per_side - 1:
                    continue
                cells.append((x0 + col * cell_chunks, z0 + row * cell_chunks, cell_chunks))
    return cells


def build_perception(store, x: int, z: int, size_chunks: int = DEFAULT_SIZE_CHUNKS,
                     materials: bool = False):
    """Return the compact LOD perception payload for a player position."""
    x = int(x)
    z = int(z)
    size_chunks = int(size_chunks)
    if size_chunks < 1:
        raise ValueError("size_chunks must be at least 1")
    if size_chunks * size_chunks > MAX_PERCEPTION_CHUNKS:
        raise ValueError(
            f"Perception window too large ({size_chunks * size_chunks} chunks); "
            f"max {MAX_PERCEPTION_CHUNKS}"
        )

    center_cx = x >> 4
    center_cz = z >> 4
    cx1, cz1, cx2, cz2 = window_bounds(center_cx, center_cz, size_chunks)
    records, missing = _load_window(
        store, center_cx, center_cz, size_chunks, cx1, cz1, cx2, cz2
    )

    levels = []
    for level in lod_levels(cx1, cz1, size_chunks):
        levels.append(_build_level(level, records, materials))

    composition, biome = _composition_summary(records)
    updated_at = max(
        (float(record.get("updated_at") or 0.0) for record in records.values()),
        default=0.0,
    )
    if updated_at <= 0:
        updated_at = time.time()

    return {
        "service": "topography",
        "center": {"x": x, "z": z},
        "size_chunks": size_chunks,
        "window": {"cx1": cx1, "cz1": cz1, "cx2": cx2, "cz2": cz2},
        "resolution": "lod",
        "lod": levels,
        "surface": _surface_summary(records),
        "water": _water_summary(records, x, z),
        "biome": biome,
        "composition": composition,
        "missing_chunks": missing,
        "updated_at": updated_at,
        "window_version": int(getattr(store, "generation", 0) or 0),
    }


# --------------------------------------------------------------------- window
def _load_window(store, center_cx, center_cz, size_chunks, cx1, cz1, cx2, cz2):
    """Load the window and return ``({(cx, cz): record}, missing)``."""
    touch_window = getattr(store, "touch_window", None)
    if touch_window is not None:
        records, _ = touch_window(center_cx, center_cz, size_chunks)
    else:
        # TODO(1B/Phase 2): remove this temporary shim once 1A's touch_window
        # lands. get_window uses an inclusive radius, so it loads a slightly
        # larger square and we clip it back to the requested window below.
        radius = size_chunks // 2
        records, _ = store.get_window(center_cx, center_cz, radius)

    record_map = {}
    for record in records or []:
        try:
            cx = int(record["cx"])
            cz = int(record["cz"])
        except (KeyError, TypeError, ValueError):
            continue
        if cx1 <= cx <= cx2 and cz1 <= cz <= cz2:
            record_map[(cx, cz)] = record
    return record_map, size_chunks * size_chunks - len(record_map)


# ---------------------------------------------------------------------- levels
def _build_level(level, records, materials):
    cells_per_side = level["cells_per_side"]
    cell_chunks = level["cell_chunks"]
    x0 = level["cx1"]
    z0 = level["cz1"]

    heights = []
    water = []
    material_cells = [] if materials else None
    for row in range(cells_per_side):
        for col in range(cells_per_side):
            height, is_water, material = _cell_stats(
                records,
                x0 + col * cell_chunks,
                z0 + row * cell_chunks,
                cell_chunks,
                materials,
            )
            heights.append(height)
            water.append(1 if is_water else 0)
            if materials:
                material_cells.append(material)

    result = {
        "cell_chunks": cell_chunks,
        "cells_per_side": cells_per_side,
        "cx1": x0,
        "cz1": z0,
        "heights": heights,
        "water": water,
    }
    if materials:
        result["materials"] = material_cells
    return result


def _cell_stats(records, cx0, cz0, cell_chunks, materials):
    heights = []
    water_count = 0
    columns = 0
    material_counts = Counter()

    for cx in range(cx0, cx0 + cell_chunks):
        for cz in range(cz0, cz0 + cell_chunks):
            record = records.get((cx, cz))
            if record is None:
                continue
            surface = record.get("surface") or {}
            ys = surface.get("y") or []
            water = surface.get("water") or []
            materials_list = surface.get("materials") or []
            for index, y in enumerate(ys):
                if y is None:
                    continue
                columns += 1
                heights.append(y)
                if index < len(water) and water[index]:
                    water_count += 1
                if materials and index < len(materials_list) and materials_list[index]:
                    material_counts[materials_list[index]] += 1

    height = int(round(sum(heights) / len(heights))) if heights else None
    is_water = columns > 0 and water_count * 2 > columns
    material = material_counts.most_common(1)[0][0] if material_counts else None
    return height, is_water, material


# -------------------------------------------------------------------- summaries
def _surface_summary(records):
    heights = []
    for record in records.values():
        surface = record.get("surface") or {}
        for y in surface.get("y") or []:
            if y is not None:
                heights.append(y)
    if not heights:
        return {"min_y": None, "max_y": None, "avg_y": None, "relief": None}
    min_y = min(heights)
    max_y = max(heights)
    return {
        "min_y": min_y,
        "max_y": max_y,
        "avg_y": round(sum(heights) / len(heights), 1),
        "relief": max_y - min_y,
    }


def _water_summary(records, x, z):
    at_center = False
    center = records.get((x >> 4, z >> 4))
    if center is not None:
        surface = center.get("surface") or {}
        water = surface.get("water") or []
        index = (x % 16) + (z % 16) * 16
        if index < len(water):
            at_center = bool(water[index])

    best = None
    for (cx, cz), record in records.items():
        surface = record.get("surface") or {}
        water = surface.get("water") or []
        for index, is_water in enumerate(water):
            if not is_water:
                continue
            world_x = cx * 16 + (index % 16)
            world_z = cz * 16 + (index // 16)
            dx = world_x - x
            dz = world_z - z
            distance = math.hypot(dx, dz)
            if best is None or distance < best[0]:
                best = (distance, dx, dz)

    if best is None:
        nearest = None
    else:
        distance, dx, dz = best
        nearest = {
            "dx": dx,
            "dz": dz,
            "distance": round(distance, 1),
            "direction": _direction(dx, dz),
        }
    return {"at_center": at_center, "nearest": nearest}


def _direction(dx, dz):
    if dx == 0 and dz == 0:
        return "here"
    angle = math.degrees(math.atan2(dz, dx))
    return _DIRECTIONS[int(round(angle / 45.0)) % 8]


def _composition_summary(records):
    totals = Counter()
    top_blocks = Counter()
    biomes = Counter()

    for record in records.values():
        composition = record.get("composition") or {}
        counts = composition.get("counts") or {}
        for key in ("natural", "engineered", "liquid", "air", "unknown"):
            totals[key] += int(counts.get(key, 0) or 0)

        for entry in composition.get("top_blocks") or []:
            try:
                name = entry[0]
                count = int(entry[1])
            except (IndexError, TypeError, ValueError):
                continue
            top_blocks[name] += count

        biome = composition.get("dominant_biome")
        if biome:
            biomes[biome] += 1

    natural = totals["natural"]
    engineered = totals["engineered"]
    liquid = totals["liquid"]
    total = natural + engineered + liquid + totals["air"] + totals["unknown"]
    noise = natural + engineered

    composition = {
        "natural": natural,
        "engineered": engineered,
        "liquid": liquid,
        "air": totals["air"],
        "water_fraction": round(liquid / total, 3) if total else 0.0,
        "top_blocks": [[name, count] for name, count in top_blocks.most_common(TOP_BLOCKS_LIMIT)],
        "artificiality_score": round(engineered / noise, 3) if noise else 0.0,
    }
    biome = biomes.most_common(1)[0][0] if biomes else None
    return composition, biome
