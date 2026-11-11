"""Long-range overview of everything one agent has seen.

The perception window covers about 128 blocks each way. ``GET /overview`` answers "what is out
there" over a much larger radius at a flat cost: it reads only the small per-chunk summaries
written by :class:`map_store.MapStore` at flush time (never chunk rows), through the same gated,
last-seen view the other queries use, and reports coarse cells by compass sector and distance ring
plus a few named features (high ground, water bodies, built areas).

Stdlib only. Nothing here loads, decodes, promotes or touches a chunk.
"""

from __future__ import annotations

import json
import math
import threading
from collections import Counter, OrderedDict, deque
from statistics import median

import perception
import terrain_queries

MIN_RADIUS = 256
MAX_RADIUS = 4096
DEFAULT_RADIUS = 2048

NEAR = 128                       # nearer than this is the perception window's job
RINGS = (128, 512, 1024, 2048, 4096)
SECTORS = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
WATER_CHUNK = 128                # water columns for a chunk to count as water
WATER_MIN_CHUNKS = 4             # smallest water body reported
BUILT_CHUNK = 8                  # built columns for a chunk to count as built
HIGH_MIN_RISE = 8                # a high point must stand this far above the median ground
FOREST_SHARE = 0.3
MAX_FEATURES = 8
MAX_BYTES = 6000
CACHE_SIZE = 32

_ROCK = frozenset({
    "stone", "deepslate", "andesite", "diorite", "granite", "tuff", "calcite",
    "basalt", "blackstone", "gravel", "cobblestone",
})
_GRASS = frozenset({
    "grass_block", "dirt", "coarse_dirt", "podzol", "mycelium", "rooted_dirt",
    "moss_block", "mud",
})

_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


def land_class(name):
    """Coarse land class of a surface material name (no ``minecraft:`` prefix)."""
    if not name:
        return "other"
    raw = str(name).split(":", 1)[-1]
    if "sand" in raw:
        return "sand"
    if "snow" in raw or "ice" in raw:
        return "snow"
    if "terracotta" in raw:
        return "badlands"
    if raw in _ROCK:
        return "rock"
    if raw in _GRASS:
        return "grass"
    return "other"


def summarize(surface):
    """The small per-chunk summary stored at flush time (``chunk_summary`` row values)."""
    surface = surface or {}
    ys = surface.get("y") or []
    water = surface.get("water") or []
    materials = surface.get("materials") or []
    ty, tm = perception.terrain_arrays(surface)

    known = water_count = trees = built = 0
    heights = []
    classes = Counter()
    for i, y in enumerate(ys):
        if y is None:
            continue
        known += 1
        is_water = i < len(water) and bool(water[i])
        if is_water:
            water_count += 1
            continue
        material = materials[i] if i < len(materials) else None
        natural = ty[i] if i < len(ty) else None
        if natural is not None:
            heights.append(natural)
            if material and str(material).split(":", 1)[-1].endswith(("_leaves", "_log")):
                trees += 1
            if y - natural >= 2 and terrain_queries._is_engineered(material):
                built += 1
        if i < len(tm):
            classes[land_class(tm[i])] += 1
    return {
        "known": known,
        "water": water_count,
        "h_min": min(heights) if heights else None,
        "h_max": max(heights) if heights else None,
        "h_sum": sum(heights) if heights else None,
        "trees": trees,
        "built": built,
        "cls": classes.most_common(1)[0][0] if classes else None,
    }


def summary_row(row):
    """``(rev, values)`` row from ``MapStore.summary_rows`` -> dict."""
    if isinstance(row, dict):
        return row
    keys = ("known", "water", "h_min", "h_max", "h_sum", "trees", "built", "cls")
    return dict(zip(keys, row))


def _bearing(dx, dz):
    return math.degrees(math.atan2(dx, -dz))


def _sector(dx, dz):
    return int(round(_bearing(dx, dz) / 45.0)) % 8


def _chunk_centre(cx, cz):
    return cx * 16 + 8, cz * 16 + 8


def _ring_index(distance):
    for index, upper in enumerate(RINGS[1:]):
        if distance < upper:
            return index
    return len(RINGS) - 2


def _cache_get(key):
    with _cache_lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
        return value


def _cache_put(key, value):
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def build(store, x: int, z: int, radius: int = DEFAULT_RADIUS):
    """Overview of the summaries ``store`` exposes within ``radius`` blocks of ``(x, z)``."""
    x, z, radius = int(x), int(z), int(radius)
    if radius < MIN_RADIUS or radius > MAX_RADIUS:
        raise ValueError(f"Parameter 'radius' must be between {MIN_RADIUS} and {MAX_RADIUS}")
    key = (getattr(store, "agent", "") or "", int(getattr(store, "generation", 0) or 0),
           x >> 6, z >> 6, radius)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _compute(store, x, z, radius)
    _cache_put(key, result)
    return result


def _compute(store, x, z, radius):
    ccx, ccz = x >> 4, z >> 4
    span = radius // 16 + 2
    rows = store.summaries(ccx - span, ccz - span, ccx + span, ccz + span)
    all_keys = set(rows)
    filtered = {}
    for key, summary in rows.items():
        centre_x, centre_z = _chunk_centre(*key)
        if math.hypot(centre_x - x, centre_z - z) <= radius:
            filtered[key] = summary_row(summary)

    seen_method = getattr(store, "seen_chunks", None)
    if callable(seen_method):
        seen_count = seen_method(ccx - span, ccz - span, ccx + span, ccz + span)
    else:
        seen_count = len(filtered)

    cells = _cells(filtered, x, z, radius)
    features = _features(filtered, all_keys, x, z)
    result = {
        "center": {"x": x, "z": z},
        "radius": radius,
        "seen_chunks": seen_count,
        "summarised_chunks": len(filtered),
        "cells": cells,
        "features": features,
        "window_version": int(getattr(store, "generation", 0) or 0),
    }
    return _fit(result)


def _cells(filtered, x, z, radius):
    stats = {}
    for (cx, cz), summary in filtered.items():
        centre_x, centre_z = _chunk_centre(cx, cz)
        dx, dz = centre_x - x, centre_z - z
        distance = math.hypot(dx, dz)
        if distance < NEAR:
            continue
        ring = _ring_index(distance)
        cell = stats.get((ring, _sector(dx, dz)))
        if cell is None:
            cell = stats[(ring, _sector(dx, dz))] = {
                "chunks": 0, "known": 0, "water": 0, "trees": 0, "dry": 0,
                "low": None, "high": None, "built": 0, "classes": Counter(),
            }
        cell["chunks"] += 1
        cell["known"] += summary["known"]
        cell["water"] += summary["water"]
        cell["trees"] += summary["trees"]
        dry = summary["known"] - summary["water"]
        cell["dry"] += dry
        if summary["h_min"] is not None:
            cell["low"] = summary["h_min"] if cell["low"] is None else min(cell["low"], summary["h_min"])
            cell["high"] = summary["h_max"] if cell["high"] is None else max(cell["high"], summary["h_max"])
        if summary["built"] >= BUILT_CHUNK:
            cell["built"] += 1
        if summary["cls"]:
            cell["classes"][summary["cls"]] += dry

    out = []
    for (ring, sector), cell in sorted(stats.items()):
        upper = min(RINGS[ring + 1], radius)
        lower = min(RINGS[ring], radius)
        area = math.pi * (upper ** 2 - lower ** 2) / 8 / 256
        coverage = min(1.0, cell["chunks"] / area) if area > 0 else 0.0
        water = cell["water"] / cell["known"] if cell["known"] else 0.0
        if water >= 0.6:
            land = "water"
        elif cell["dry"] and cell["trees"] / cell["dry"] >= FOREST_SHARE:
            land = "forest"
        elif cell["classes"]:
            land = cell["classes"].most_common(1)[0][0]
        else:
            land = None
        item = {
            "dir": SECTORS[sector],
            "from": lower,
            "to": upper,
            "chunks": cell["chunks"],
            "coverage": round(coverage, 2),
            "partial": coverage < 0.5,
            "land": land,
            "water": round(water, 2),
            "built": cell["built"],
        }
        if cell["low"] is not None:
            item["y"] = [cell["low"], cell["high"]]
        out.append(item)
    return out


def _features(filtered, all_keys, x, z):
    features = []
    features.extend(_high_ground(filtered, x, z))
    features.extend(_water_bodies(filtered, all_keys, x, z))
    features.extend(_built_areas(filtered, x, z))
    return features[:MAX_FEATURES]


def _feature(kind, chunk, x, z, **extra):
    centre_x, centre_z = _chunk_centre(*chunk)
    dx, dz = centre_x - x, centre_z - z
    item = {
        "kind": kind,
        "x": centre_x,
        "z": centre_z,
        "distance": round(math.hypot(dx, dz)),
        "direction": SECTORS[_sector(dx, dz)],
    }
    item.update(extra)
    return item


def _high_ground(filtered, x, z):
    candidates = []
    averages = []
    for (cx, cz), summary in filtered.items():
        if summary["h_min"] is None or summary["h_max"] is None:
            continue
        dry = summary["known"] - summary["water"]
        if dry > 0 and summary["h_sum"] is not None:
            averages.append(summary["h_sum"] / dry)
        centre_x, centre_z = _chunk_centre(cx, cz)
        distance = math.hypot(centre_x - x, centre_z - z)
        if distance < NEAR:
            continue
        candidates.append(((-summary["h_max"], distance), (cx, cz), summary, distance))
    if not candidates or not averages:
        return []
    base = median(averages)
    candidates.sort(key=lambda item: item[0])
    out = []
    for _sort, chunk, summary, distance in candidates:
        if summary["h_max"] - base < HIGH_MIN_RISE:
            continue
        if out:
            first = out[0]
            if math.hypot(_chunk_centre(*chunk)[0] - first["x"],
                          _chunk_centre(*chunk)[1] - first["z"]) < 256:
                continue
        out.append(_feature("high_ground", chunk, x, z, y=int(summary["h_max"])))
        if len(out) == 2:
            break
    return out


def _groups(keys, connected8=False):
    keys = set(keys)
    steps = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    if connected8:
        steps += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    groups = []
    while keys:
        start = keys.pop()
        group = [start]
        queue = deque([start])
        while queue:
            cx, cz = queue.popleft()
            for dx, dz in steps:
                nb = (cx + dx, cz + dz)
                if nb in keys:
                    keys.remove(nb)
                    group.append(nb)
                    queue.append(nb)
        groups.append(group)
    return groups


def _water_bodies(filtered, all_keys, x, z):
    wet = {key for key, summary in filtered.items() if summary["water"] >= WATER_CHUNK}
    found = []
    for group in _groups(wet):
        if len(group) < WATER_MIN_CHUNKS:
            continue
        nearest = min(
            group,
            key=lambda key: math.hypot(_chunk_centre(*key)[0] - x, _chunk_centre(*key)[1] - z),
        )
        centre_x, centre_z = _chunk_centre(*nearest)
        distance = math.hypot(centre_x - x, centre_z - z)
        if distance < NEAR:
            continue
        mean_x = round(sum(_chunk_centre(*key)[0] for key in group) / len(group))
        mean_z = round(sum(_chunk_centre(*key)[1] for key in group) / len(group))
        open_group = any(
            (key[0] + dx, key[1] + dz) not in all_keys
            for key in group for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
        item = _feature("water", nearest, x, z)
        item.update({
            "centre": {"x": mean_x, "z": mean_z},
            "chunks": len(group),
            "open": open_group,
        })
        found.append((distance, item))
    found.sort(key=lambda item: item[0])
    return [item for _distance, item in found[:3]]


def _built_areas(filtered, x, z):
    built = {key for key, summary in filtered.items() if summary["built"] >= BUILT_CHUNK}
    found = []
    for group in _groups(built, connected8=True):
        mean_x = round(sum(_chunk_centre(*key)[0] for key in group) / len(group))
        mean_z = round(sum(_chunk_centre(*key)[1] for key in group) / len(group))
        distance = math.hypot(mean_x - x, mean_z - z)
        if distance < NEAR:
            continue
        found.append((distance, mean_x, mean_z, len(group)))
    found.sort(key=lambda item: item[0])
    out = []
    for distance, mean_x, mean_z, count in found[:3]:
        dx, dz = mean_x - x, mean_z - z
        out.append({
            "kind": "built",
            "x": mean_x,
            "z": mean_z,
            "distance": round(distance),
            "direction": SECTORS[_sector(dx, dz)],
            "chunks": count,
        })
    return out


def _fit(result):
    """Drop cells of the outermost ring until the encoded response fits."""
    trimmed = False
    while True:
        encoded = json.dumps(result, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= MAX_BYTES or not result["cells"]:
            break
        outermost = max(cell["to"] for cell in result["cells"])
        kept = [cell for cell in result["cells"] if cell["to"] != outermost]
        if len(kept) == len(result["cells"]):
            break
        result["cells"] = kept
        trimmed = True
    if trimmed:
        result["trimmed"] = True
    return result


def far_features(store, x, z, limit=3):
    """The first ``limit`` overview features, reduced for the perception prompt block."""
    data = build(store, x, z, DEFAULT_RADIUS)
    out = []
    for feature in data.get("features") or []:
        item = {
            "kind": feature["kind"],
            "direction": feature["direction"],
            "distance": feature["distance"],
        }
        if "y" in feature:
            item["y"] = feature["y"]
        out.append(item)
        if len(out) >= limit:
            break
    return out
