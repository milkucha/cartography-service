"""Planning queries built on leaf-free terrain: flat building areas and bridge spans.

Both queries read ``terrain_y`` (falling back to ``ground_y`` for old cached
rows) and the surface ``water`` flag. Stdlib only.
"""

from __future__ import annotations

import math
from collections import deque
from functools import lru_cache

import mc_blocks
import perception

MAX_FLAT_RADIUS = 128
MAX_FOOTPRINT = 64
MAX_FLAT_RESULTS = 5
WATER_TOLERANCE = 0.02
# Surface this far above terrain (and dry) means a canopy/structure is in the way.
OBSTRUCTION_HEIGHT = 2

MAX_BRIDGE_WALK = 128
LONG_SPAN = 64
SHORE_DIFF_CAUTION = 3
SHORE_SEARCH_BACK = 16

DIRECTIONS = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}


@lru_cache(maxsize=512)
def _is_engineered(name):
    if not name:
        return False
    try:
        return mc_blocks.classify_block(name) == "engineered"
    except Exception:
        return False


class _ChunkCache:
    """Per-query chunk lookup so each chunk is fetched from the store once."""

    def __init__(self, store):
        self.store = store
        self.chunks = {}
        self.missing = set()

    def get(self, cx, cz):
        key = (cx, cz)
        if key in self.chunks:
            return self.chunks[key]
        if key in self.missing:
            return None
        data = self.store.get_or_load(cx, cz)
        if data is None:
            self.missing.add(key)
        else:
            self.chunks[key] = data
        return data

    def column(self, x, z):
        """Return a column dict or None when the chunk is unknown."""
        data = self.get(x >> 4, z >> 4)
        if data is None:
            return None
        surface = data["surface"]
        idx = (x & 15) + (z & 15) * 16
        ty, tm = perception.terrain_arrays(surface)
        return {
            "y": surface["y"][idx],
            "material": surface["materials"][idx],
            "terrain_y": ty[idx] if idx < len(ty) else None,
            "terrain_material": tm[idx] if idx < len(tm) else None,
            "water": bool(surface["water"][idx]),
        }


# ------------------------------------------------------------------ flat areas
def _sliding(values, k):
    """Sliding-window (min, max) lists of length ``len(values) - k + 1``."""
    mins, maxs = [], []
    dmin, dmax = deque(), deque()
    for i, v in enumerate(values):
        while dmin and values[dmin[-1]] >= v:
            dmin.pop()
        dmin.append(i)
        while dmax and values[dmax[-1]] <= v:
            dmax.pop()
        dmax.append(i)
        if dmin[0] <= i - k:
            dmin.popleft()
        if dmax[0] <= i - k:
            dmax.popleft()
        if i >= k - 1:
            mins.append(values[dmin[0]])
            maxs.append(values[dmax[0]])
    return mins, maxs


def _summed_area(grid):
    """(rows+1) x (cols+1) inclusive prefix sums of a 0/1 grid."""
    rows = len(grid)
    cols = len(grid[0]) if rows else 0
    sat = [[0] * (cols + 1) for _ in range(rows + 1)]
    for r in range(rows):
        run = 0
        src = grid[r]
        above = sat[r]
        cur = sat[r + 1]
        for c in range(cols):
            run += src[c]
            cur[c + 1] = above[c + 1] + run
    return sat


def _rect_sum(sat, r, c, h, w):
    return sat[r + h][c + w] - sat[r][c + w] - sat[r + h][c] + sat[r][c]


def flat_areas(store, center_x, center_z, radius=64, width=9, depth=9, max_delta=1):
    """Find axis-aligned width x depth footprints whose terrain is nearly level.

    Returns up to five non-overlapping candidates ranked by (height delta,
    distance from the centre). Unknown columns, engineered ground, and (beyond
    2%) water or obstructed columns disqualify a footprint.
    """
    center_x, center_z = int(center_x), int(center_z)
    radius = int(radius)
    width, depth, max_delta = int(width), int(depth), int(max_delta)
    if radius < 1 or radius > MAX_FLAT_RADIUS:
        raise ValueError(f"Parameter 'radius' must be between 1 and {MAX_FLAT_RADIUS}")
    for name, value in (("width", width), ("depth", depth)):
        if value < 1 or value > MAX_FOOTPRINT:
            raise ValueError(f"Parameter '{name}' must be between 1 and {MAX_FOOTPRINT}")
    if max_delta < 0 or max_delta > 64:
        raise ValueError("Parameter 'max_delta' must be between 0 and 64")

    x1, x2 = center_x - radius, center_x + radius
    z1, z2 = center_z - radius, center_z + radius
    cols = x2 - x1 + 1
    rows = z2 - z1 + 1
    cache = _ChunkCache(store)

    height = [[0] * cols for _ in range(rows)]
    hard = [[0] * cols for _ in range(rows)]   # unknown or engineered ground
    wet = [[0] * cols for _ in range(rows)]    # surface water
    blocked = [[0] * cols for _ in range(rows)]  # canopy / structure above ground
    material = [[None] * cols for _ in range(rows)]
    missing = 0
    for cx in range(x1 >> 4, (x2 >> 4) + 1):
        for cz in range(z1 >> 4, (z2 >> 4) + 1):
            data = cache.get(cx, cz)
            if data is None:
                missing += 1
                for z in range(max(z1, cz * 16), min(z2, cz * 16 + 15) + 1):
                    hrow = hard[z - z1]
                    for x in range(max(x1, cx * 16), min(x2, cx * 16 + 15) + 1):
                        hrow[x - x1] = 1
                continue
            surface = data["surface"]
            ty, tm = perception.terrain_arrays(surface)
            sy = surface["y"]
            water = surface["water"]
            for z in range(max(z1, cz * 16), min(z2, cz * 16 + 15) + 1):
                r = z - z1
                for x in range(max(x1, cx * 16), min(x2, cx * 16 + 15) + 1):
                    idx = (x & 15) + (z & 15) * 16
                    c = x - x1
                    t = ty[idx] if idx < len(ty) else None
                    if t is None:
                        hard[r][c] = 1
                        continue
                    height[r][c] = t
                    name = tm[idx] if idx < len(tm) else None
                    material[r][c] = name
                    if _is_engineered(name):
                        hard[r][c] = 1
                    if water[idx]:
                        wet[r][c] = 1
                    elif sy[idx] is not None and sy[idx] > t + OBSTRUCTION_HEIGHT:
                        blocked[r][c] = 1

    sat_hard = _summed_area(hard)
    sat_wet = _summed_area(wet)
    sat_blocked = _summed_area(blocked)

    orientations = [(width, depth)] if width == depth else [(width, depth), (depth, width)]
    candidates = []
    r2 = radius * radius
    for w, d in orientations:
        if w > cols or d > rows:
            continue
        allowed = int(WATER_TOLERANCE * w * d)
        # Row pass: window of w along x for every row.
        row_min, row_max = [], []
        for r in range(rows):
            lo, hi = _sliding(height[r], w)
            row_min.append(lo)
            row_max.append(hi)
        out_cols = cols - w + 1
        # Column pass: window of d along z for every column of the row results.
        for c in range(out_cols):
            lo_col, _ = _sliding([row_min[r][c] for r in range(rows)], d)
            _, hi_col = _sliding([row_max[r][c] for r in range(rows)], d)
            for r in range(rows - d + 1):
                delta = hi_col[r] - lo_col[r]
                if delta > max_delta:
                    continue
                fx = x1 + c + (w - 1) / 2.0
                fz = z1 + r + (d - 1) / 2.0
                dist2 = (fx - center_x) ** 2 + (fz - center_z) ** 2
                if dist2 > r2:
                    continue
                if _rect_sum(sat_hard, r, c, d, w):
                    continue
                n_wet = _rect_sum(sat_wet, r, c, d, w)
                if n_wet > allowed:
                    continue
                if _rect_sum(sat_blocked, r, c, d, w) > allowed:
                    continue
                candidates.append((delta, dist2, r, c, w, d, n_wet, lo_col[r], hi_col[r]))

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    chosen = []
    for item in candidates:
        _, _, r, c, w, d, *_ = item
        overlap = False
        for other in chosen:
            _, _, r0, c0, w0, d0, *_ = other
            if c < c0 + w0 and c0 < c + w and r < r0 + d0 and r0 < r + d:
                overlap = True
                break
        if not overlap:
            chosen.append(item)
            if len(chosen) >= MAX_FLAT_RESULTS:
                break

    results = []
    for delta, dist2, r, c, w, d, n_wet, y_min, y_max in chosen:
        counts = {}
        for rr in range(r, r + d):
            for name in material[rr][c:c + w]:
                counts[name] = counts.get(name, 0) + 1
        floor = max(counts, key=counts.get) if counts else None
        fx = x1 + c + (w - 1) / 2.0
        fz = z1 + r + (d - 1) / 2.0
        center_cell_x = x1 + c + (w - 1) // 2
        center_cell_z = z1 + r + (d - 1) // 2
        results.append(
            {
                "x1": x1 + c,
                "z1": z1 + r,
                "x2": x1 + c + w - 1,
                "z2": z1 + r + d - 1,
                "y_min": y_min,
                "y_max": y_max,
                "center_y": height[center_cell_z - z1][center_cell_x - x1],
                "distance": round(math.sqrt(dist2), 1),
                "direction": perception._direction(round(fx - center_x), round(fz - center_z)),
                "water_columns": n_wet,
                "floor_material": floor,
            }
        )

    return {
        "center": {"x": center_x, "z": center_z},
        "radius": radius,
        "width": width,
        "depth": depth,
        "max_delta": max_delta,
        "areas": results,
        "missing_chunks": missing,
    }


# ----------------------------------------------------------------- bridge span
def _walk_points(start_x, start_z, direction, target):
    if target is not None:
        tx, tz = target
        dx, dz = abs(tx - start_x), abs(tz - start_z)
        n = max(dx, dz)
        if n == 0:
            return [(start_x, start_z)]
        return [
            (start_x + round((tx - start_x) * i / n), start_z + round((tz - start_z) * i / n))
            for i in range(n + 1)
        ]
    sx, sz = DIRECTIONS[direction]
    return [(start_x + sx * i, start_z + sz * i) for i in range(MAX_BRIDGE_WALK + 1)]


def bridge_span(store, x, z, direction=None, target=None):
    """Scan from (x, z) toward water and describe the crossing to the far shore.

    ``direction`` is north|south|east|west; ``target`` is an ``(x2, z2)`` pair
    (walks the straight line toward it). Shore heights are leaf-free terrain.
    """
    x, z = int(x), int(z)
    if target is None and direction not in DIRECTIONS:
        raise ValueError("Parameter 'dir' must be north, south, east or west")
    cache = _ChunkCache(store)
    points = _walk_points(x, z, direction, target)

    cautions = []
    start = cache.column(*points[0])
    water_cols = []  # first (main) crossing
    near = None
    far = None
    runs = []  # all water runs seen: lengths
    current_run = 0
    in_main = False
    main_done = False
    missing_on_line = 0
    lava = False

    def shore(px, pz, col):
        ty = col["terrain_y"] if col["terrain_y"] is not None else col["y"]
        return {"x": px, "z": pz, "y": ty, "material": col["terrain_material"] or col["material"]}

    if start is not None and start["water"]:
        cautions.append("start column is water; stand on the near shore")

    for px, pz in points:
        col = cache.column(px, pz)
        if col is None or col["y"] is None:
            if not main_done:
                missing_on_line += 1
            if current_run:
                runs.append(current_run)
                current_run = 0
            continue
        is_water = col["water"]
        if is_water:
            current_run += 1
            if "lava" in (col["material"] or ""):
                lava = True
            if not main_done:
                in_main = True
                bed = col["terrain_y"]
                water_cols.append(
                    {
                        "x": px,
                        "z": pz,
                        "y": col["y"],
                        "bed_y": bed,
                        "depth": (col["y"] - bed) if bed is not None else None,
                        "material": col["material"],
                    }
                )
        else:
            if current_run:
                runs.append(current_run)
                current_run = 0
            if in_main and not main_done:
                far = shore(px, pz, col)
                main_done = True
            elif not in_main:
                near = shore(px, pz, col)
    if current_run:
        runs.append(current_run)

    if not water_cols:
        cautions.append("no water found along this line")
    elif far is None:
        cautions.append("no far shore found within range")
    if near is None and water_cols:
        cautions.append("no near shore found (start is not on land)")
    if missing_on_line:
        cautions.append(f"{missing_on_line} columns along the line are not loaded")
    if lava:
        cautions.append("lava in the crossing")

    span = len(water_cols)
    if span > LONG_SPAN:
        cautions.append(f"long span ({span} blocks)")
    if near and far and abs(near["y"] - far["y"]) > SHORE_DIFF_CAUTION:
        cautions.append(f"shore heights differ by {abs(near['y'] - far['y'])} blocks")

    water_y = max((c["y"] for c in water_cols), default=None)
    depths = [c["depth"] for c in water_cols if c["depth"] is not None]
    beds = [c["bed_y"] for c in water_cols if c["bed_y"] is not None]
    deck_y = None
    if water_y is not None:
        deck_y = max([water_y + 2] + [s["y"] for s in (near, far) if s and s["y"] is not None])

    return {
        "start": {"x": x, "z": z},
        "dir": direction,
        "target": {"x": target[0], "z": target[1]} if target else None,
        "near_shore": near,
        "far_shore": far,
        "span": span,
        "water_y": water_y,
        "water": water_cols[:MAX_BRIDGE_WALK],
        "depth": {
            "min": min(depths) if depths else None,
            "max": max(depths) if depths else None,
            "avg": round(sum(depths) / len(depths), 1) if depths else None,
        },
        "bed_y": {"min": min(beds) if beds else None, "max": max(beds) if beds else None},
        "longest_water_run": max(runs) if runs else 0,
        "water_runs": len(runs),
        "deck_y": deck_y,
        "caution": cautions,
        "missing_chunks": len(cache.missing) if not main_done else 0,
    }
