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
from functools import lru_cache

import mc_blocks

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
        "terrain": _terrain_summary(records, x, z),
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
    records, _ = store.touch_window(center_cx, center_cz, size_chunks)

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
def terrain_arrays(surface):
    """Return ``(terrain_y, terrain_materials)`` with a ``ground_y`` fallback.

    Old cached rows have no ``terrain_*`` arrays; they fall back to the
    pre-existing ground arrays (which may include tree canopies).
    """
    ys = surface.get("terrain_y")
    if ys is None:
        return surface.get("ground_y") or [], surface.get("ground_materials") or []
    mats = surface.get("terrain_materials")
    if mats is None:
        mats = surface.get("ground_materials") or []
    return ys, mats


def _terrain_relief(records):
    """max-min of leaf-free terrain height over dry columns (None if unknown)."""
    lo = hi = None
    wet_lo = wet_hi = None
    for record in records.values():
        surface = record.get("surface") or {}
        ys, _ = terrain_arrays(surface)
        water = surface.get("water") or []
        nwater = len(water)
        for index, y in enumerate(ys):
            if y is None:
                continue
            if wet_lo is None or y < wet_lo:
                wet_lo = y
            if wet_hi is None or y > wet_hi:
                wet_hi = y
            if index < nwater and water[index]:
                continue
            if lo is None or y < lo:
                lo = y
            if hi is None or y > hi:
                hi = y
    if lo is None:
        lo, hi = wet_lo, wet_hi
    return None if lo is None else hi - lo


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
    relief = _terrain_relief(records)
    return {
        "min_y": min_y,
        "max_y": max_y,
        "avg_y": round(sum(heights) / len(heights), 1),
        "relief": relief if relief is not None else max_y - min_y,
    }


_OFFSETS_X = [i % 16 for i in range(256)]
_OFFSETS_Z = [i // 16 for i in range(256)]


@lru_cache(maxsize=1 << 17)
def _direction_cached(dx, dz):
    return _direction(dx, dz)


def _terrain_summary(records, x, z):
    """Summarise leaf-free dry ground: peak, low point, and per-direction peaks.

    Uses ``terrain_y`` (canopy-free). Chunks are visited nearest-first and
    pruned by their max/min so most columns are never touched.
    """
    highest = None  # (y, dx, dz, material)
    lowest = None
    best_dir = {}  # direction -> (y, dx, dz)

    prepared = []
    for (cx, cz), record in records.items():
        surface = record.get("surface") or {}
        ys, mats = terrain_arrays(surface)
        if not ys:
            continue
        water = surface.get("water") or []
        has_water = any(water)
        known = [
            ys[i] for i in range(min(len(ys), 256))
            if ys[i] is not None and not (has_water and i < len(water) and water[i])
        ]
        if not known:
            continue
        bx = cx * 16 - x
        bz = cz * 16 - z
        near = (bx + 7.5) ** 2 + (bz + 7.5) ** 2
        prepared.append((near, bx, bz, max(known), min(known), ys, mats, water if has_water else None))
    prepared.sort(key=lambda item: item[0])

    n_dirs = 8
    for _, bx, bz, cmax, cmin, ys, mats, water in prepared:
        need_high = highest is None or cmax > highest[0]
        need_low = lowest is None or cmin < lowest[0]
        thr = None
        if len(best_dir) >= n_dirs:
            thr = min(v[0] for v in best_dir.values())
        need_dir = thr is None or cmax > thr
        if not (need_high or need_low or need_dir):
            continue
        for index in range(len(ys)):
            y = ys[index]
            if y is None:
                continue
            if water is not None and index < len(water) and water[index]:
                continue
            if highest is not None and lowest is not None and best_dir:
                # fast reject: cannot improve anything unless above the
                # weakest direction peak or beyond the extrema
                if y <= highest[0] and y >= lowest[0]:
                    if thr is not None and y <= thr:
                        continue
            dx = bx + _OFFSETS_X[index]
            dz = bz + _OFFSETS_Z[index]
            if highest is None or y > highest[0]:
                highest = (y, dx, dz, mats[index] if index < len(mats) else None)
            if lowest is None or y < lowest[0]:
                lowest = (y, dx, dz, mats[index] if index < len(mats) else None)
            direction = _direction_cached(dx, dz)
            previous = best_dir.get(direction)
            if previous is None or y > previous[0]:
                best_dir[direction] = (y, dx, dz)
            if thr is not None and len(best_dir) >= n_dirs:
                thr = min(v[0] for v in best_dir.values())

    def entry(item):
        y, dx, dz, material = item
        return {
            "x": x + dx,
            "z": z + dz,
            "y": int(y),
            "dx": dx,
            "dz": dz,
            "distance": round(math.hypot(dx, dz), 1),
            "direction": _direction_cached(dx, dz),
            "material": material,
        }

    by_direction = {
        direction: {
            "y": int(y),
            "distance": round(math.hypot(dx, dz), 1),
            "direction": direction,
        }
        for direction, (y, dx, dz) in best_dir.items()
    }
    return {
        "highest": entry(highest) if highest else None,
        "lowest": entry(lowest) if lowest else None,
        "highest_by_direction": by_direction,
        "relief": _terrain_relief(records),
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


def accumulate_blocks(composition, top_blocks, engineered_blocks):
    """Add one chunk's block lists into ``top_blocks`` / ``engineered_blocks``.

    Air and unclassified blocks are dropped from ``top_blocks``. Engineered
    blocks come from the per-chunk ``engineered_blocks`` list when present
    (not subject to the top-N cap) and from ``top_blocks`` for old rows.
    """
    def pairs(items):
        for entry in items or []:
            try:
                yield entry[0], int(entry[1])
            except (IndexError, TypeError, ValueError):
                continue

    for name, count in pairs(composition.get("top_blocks")):
        try:
            kind = mc_blocks.classify_block(name)
        except Exception:
            kind = "unknown"
        if kind in ("air", "unknown"):
            continue
        top_blocks[name] += count
        if kind == "engineered" and "engineered_blocks" not in composition:
            engineered_blocks[name] += count
    for name, count in pairs(composition.get("engineered_blocks")):
        engineered_blocks[name] += count
        # make sure building materials are visible in top_blocks totals too
        # only if the chunk's own top-N missed them (not double counted)
        if name not in {e[0] for e in composition.get("top_blocks") or []}:
            top_blocks[name] += count


def _composition_summary(records):
    totals = Counter()
    top_blocks = Counter()
    engineered_blocks = Counter()
    biomes = Counter()

    for record in records.values():
        composition = record.get("composition") or {}
        counts = composition.get("counts") or {}
        for key in ("natural", "engineered", "liquid", "air", "unknown"):
            totals[key] += int(counts.get(key, 0) or 0)

        accumulate_blocks(composition, top_blocks, engineered_blocks)

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
        "engineered_blocks": [[name, count] for name, count in engineered_blocks.most_common(5)],
        "artificiality_score": round(engineered / noise, 3) if noise else 0.0,
    }
    biome = biomes.most_common(1)[0][0] if biomes else None
    return composition, biome
