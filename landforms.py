"""Named landform classification (cliff, hill, mountain, ridge, plateau, valley, plain, lake,
river, ocean) from the stored per-column surface data.

Heights are the *natural* ground ``terrain_y`` (``ground_y`` for old rows without it, exactly as
``perception.terrain_arrays``), so roofs, trees and decks never read as terrain. Water comes from
the ``water`` flags. Only chunks the store (or a gated ``StoreView``) hands over are used:

  * every landform is built from known cells only; nothing is inferred across a missing chunk,
  * steepness is measured inside a cell or between two *known* neighbours, so the edge of the
    known data is never a cliff and never a shoreline,
  * landforms touching unknown cells carry ``"partial": true`` (their extent is a lower bound).

Pipeline (all pure Python, cached per window like ``settlements``):

  1. per-chunk features: 16 cells of 4x4 blocks (mean/min/max of dry terrain, water share),
     cached by (record, store generation),
  2. water components on the 4-block grid -> lake / river / ocean,
  3. steep cells (4-block grid) -> thin steep bands -> cliffs (with the downhill facing),
  4. 8-block cells + a smooth "local base" (lower quartile of the surroundings, ~160 blocks)
     -> raised masses -> plateau / ridge / mountain / hill by shape and prominence,
  5. valley floors (higher ground on both sides along some axis), plains (low relief, low
     prominence, large).

Results are ranked by salience over distance, capped per kind, and described relative to the
query point (nearest relevant point, direction, distance).
"""

from __future__ import annotations

import math
from collections import OrderedDict

import perception
import store_view

# ---------------------------------------------------------------- thresholds
# Grids: C4 = 4-block cells (chunk features, water, cliffs); S8 = 8-block cells (relief forms).
# Minecraft context: sea level 63, plains ~64-72, hills +10..+35, mountains peak y>=100 and rise
# 30+ above their surroundings, cliffs are near-vertical steps of 8+ blocks.
CLIFF_CELL_RANGE = 6      # >= this much dry-height spread inside a 4x4 cell = steep cell
CLIFF_EDGE_STEP = 8       # >= this mean-height step between adjacent 4-cells = steep edge (slope >= 2)
CLIFF_MIN_HEIGHT = 8      # total y span of a cliff band
CLIFF_MIN_CELLS = 3       # ... and at least this many steep cells (>= ~12 blocks long)
CLIFF_MIN_LENGTH = 12     # blocks along the band
CLIFF_MAX_INTERIOR = 0.35  # share of band cells with all 8 neighbours steep; thicker = mountain flank
CLIFF_FACING_MIN_SLOPE = 0.3  # blocks per 4-cell below which no facing is reported

HILL_PROM = 10            # rise above the local base that makes a cell "raised"
HILL_MIN_CELLS = 6        # 8x8 cells (~20x20 blocks) so small bumps are ignored
MOUNTAIN_PROM = 32        # peak prominence for a mountain ...
MOUNTAIN_MIN_Y = 96       # ... and absolute peak height
RIDGE_MIN_LENGTH = 48     # blocks
RIDGE_MIN_ASPECT = 2.5    # length / mean width
RIDGE_MAX_WIDTH = 48
PLATEAU_FLAT_BAND = 3     # flat top = cells within this of the 75th percentile height
PLATEAU_FLAT_RANGE = 4    # ... with at most this internal relief
PLATEAU_MIN_FLAT = 10     # flat 8x8 cells (>= ~640 blocks^2)
PLATEAU_MIN_FLAT_FRAC = 0.5
PLATEAU_EDGE_DROP = 6     # a rim pair is steep if the drop to the outside cell is at least this
PLATEAU_MIN_STEEP = 4
PLATEAU_MIN_STEEP_FRAC = 0.5

VALLEY_DEPTH = 12         # both flanks at least this much higher than the floor ...
VALLEY_REACH = 6          # ... within this many 8-cells (48 blocks)
VALLEY_MIN_CELLS = 8
VALLEY_MIN_LENGTH = 40

PLAIN_MAX_PROM = 6
PLAIN_MAX_RELIEF = 5      # max-min over the 3x3 neighbourhood of 8-cells
PLAIN_MIN_CELLS = 64      # ~64x64 blocks

LAKE_MIN_AREA = 192       # blocks^2
RIVER_MIN_AREA = 96
RIVER_MIN_LENGTH = 24
RIVER_MAX_WIDTH = 14
RIVER_MIN_ASPECT = 3.0
OCEAN_MIN_AREA = 30000

BASE_BLOCK_MIN = 4        # known 8-cells (of 16) for a 32-block block to have a base value
BASE_NEIGHBOURS_MIN = 6   # known blocks (of 25) for the local base to exist

MAX_RADIUS = 256
MIN_RADIUS = 16
DEFAULT_RADIUS = 128
MAX_RESULTS = 12
PERCEPTION_LIMIT = 8
PER_KIND_CAP = {"cliff": 3}
DEFAULT_PER_KIND = 2

KINDS = ("cliff", "hill", "mountain", "ridge", "plateau", "valley", "plain", "lake", "river",
         "ocean")

_NB8 = [(dx, dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1) if (dx, dz) != (0, 0)]
_NB4 = [(1, 0), (-1, 0), (0, 1), (0, -1)]


# ------------------------------------------------------------ chunk features
_feat_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_FEAT_LIMIT = 6000
_memo: "OrderedDict[tuple, list]" = OrderedDict()


def _chunk_cells(rec, gen):
    """16 cells (row-major 4x4 of 4-block cells): ``(mean|None, lo, hi, wet, water_y)``."""
    key = (rec["cx"], rec["cz"])
    hit = _feat_cache.get(key)
    if hit is not None and hit[0] is rec and hit[1] == gen:
        return hit[2]
    surf = rec.get("surface") or {}
    ty, _ = perception.terrain_arrays(surf)
    water = surf.get("water") or []
    sy = surf.get("y") or []
    n_ty, n_w, n_sy = len(ty), len(water), len(sy)
    cells = []
    for cz4 in range(4):
        for cx4 in range(4):
            total = count = wet = 0
            lo = hi = None
            wy = None
            for r in range(4):
                base = (cz4 * 4 + r) * 16 + cx4 * 4
                for c in range(4):
                    i = base + c
                    if i < n_w and water[i]:
                        wet += 1
                        if wy is None and i < n_sy and sy[i] is not None:
                            wy = sy[i]
                        continue
                    y = ty[i] if i < n_ty else None
                    if y is None:
                        continue
                    total += y
                    count += 1
                    if lo is None or y < lo:
                        lo = y
                    if hi is None or y > hi:
                        hi = y
            cells.append((total / count if count else None, lo, hi, wet >= 8, wy))
    _feat_cache[key] = (rec, gen, cells)
    _feat_cache.move_to_end(key)
    while len(_feat_cache) > _FEAT_LIMIT:
        _feat_cache.popitem(last=False)
    return cells


# ------------------------------------------------------------------- helpers
def compass(dx, dz):
    """8-way direction with north = -z (same convention as ``settlements.compass``)."""
    if dx == 0 and dz == 0:
        return "here"
    ang = math.degrees(math.atan2(dx, -dz)) % 360
    names = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    return names[int((ang + 22.5) // 45) % 8]


def _components(keys, neighbours):
    keys = set(keys)
    seen = set()
    out = []
    for start in keys:
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        comp = []
        while stack:
            p = stack.pop()
            comp.append(p)
            for dx, dz in neighbours:
                q = (p[0] + dx, p[1] + dz)
                if q in keys and q not in seen:
                    seen.add(q)
                    stack.append(q)
        out.append(comp)
    return out


def _shape(pts, cell):
    """(length, width, mean_x, mean_z) of a cell cloud; width = area / length."""
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    mz = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    szz = sum((p[1] - mz) ** 2 for p in pts)
    sxz = sum((p[0] - mx) * (p[1] - mz) for p in pts)
    theta = 0.5 * math.atan2(2 * sxz, sxx - szz)
    ux, uz = math.cos(theta), math.sin(theta)
    proj = [(p[0] - mx) * ux + (p[1] - mz) * uz for p in pts]
    length = (max(proj) - min(proj)) * cell + cell
    area = n * cell * cell
    return length, area / length, mx, mz


def _partial(keys, known):
    """True if any cell has a neighbour that is not in ``known`` (data edge / unseen chunk)."""
    for (a, b) in keys:
        for dx, dz in _NB8:
            if (a + dx, b + dz) not in known:
                return True
    return False


def _percentile(sorted_vals, q):
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * q))]


def _box(key, size):
    x0, z0 = key[0] * size, key[1] * size
    return (x0, z0, x0 + size - 1, z0 + size - 1)


def _entry(kind, salience, pts8, cell8, x, z, y_min, y_max, length, width, area, partial,
           **extra):
    """Landform record in world coordinates; ``boxes`` are 8-block cell boxes for nearest."""
    item = {
        "kind": kind,
        "x": int(x), "z": int(z),
        "y_min": int(y_min), "y_max": int(y_max),
        "size": {"length": int(round(length)), "width": int(round(width)),
                 "area": int(round(area))},
        "partial": bool(partial),
        "salience": round(float(salience), 1),
    }
    item.update(extra)
    keys = sorted({k for k in pts8})
    return {"item": item, "cells8": keys}


def _centre_of(points, size):
    """Cell (key) closest to the centroid; returns its centre block (x, z)."""
    mx = sum(p[0] for p in points) / len(points)
    mz = sum(p[1] for p in points) / len(points)
    best = min(points, key=lambda p: (p[0] - mx) ** 2 + (p[1] - mz) ** 2)
    return best[0] * size + size // 2, best[1] * size + size // 2


# ------------------------------------------------------------------ analysis
def analyze(records, gen=0):
    """Return landforms (world coordinates, not yet relative to a query point)."""
    c4 = {}
    for (cx, cz), rec in records.items():
        cells = _chunk_cells(rec, gen)
        for idx, cell in enumerate(cells):
            c4[(cx * 4 + (idx & 3), cz * 4 + (idx >> 2))] = cell
    if not c4:
        return []
    out = []
    out.extend(_water_forms(c4))
    out.extend(_cliffs(c4))
    s8 = _super_cells(c4)
    out.extend(_relief_forms(s8))
    return out


# ---- water
def _water_forms(c4):
    wet = {k for k, v in c4.items() if v[3]}
    out = []
    for comp in _components(wet, _NB8):
        n = len(comp)
        area = n * 16
        if area < RIVER_MIN_AREA:
            continue
        length, width, _, _ = _shape(comp, 4)
        ys = sorted(c4[k][4] for k in comp if c4[k][4] is not None)
        wy = ys[len(ys) // 2] if ys else 62
        is_river = (RIVER_MIN_LENGTH <= length and width <= RIVER_MAX_WIDTH
                    and length / width >= RIVER_MIN_ASPECT)
        if is_river:
            kind, sal = "river", 10 + length * 0.3
        elif area >= OCEAN_MIN_AREA:
            kind, sal = "ocean", 60
        elif area >= LAKE_MIN_AREA:
            kind, sal = "lake", math.sqrt(area) * 0.6
        else:
            continue
        x, z = _centre_of(comp, 4)
        # prefer an actual water cell for the representative point
        out.append(_entry(kind, sal, {(k[0] >> 1, k[1] >> 1) for k in comp}, 8, x, z, wy, wy,
                          length, width, area, _partial(comp, c4)))
    return out


# ---- cliffs
def _cliffs(c4):
    steep = set()
    for k, (mean, lo, hi, wet, _) in c4.items():
        if mean is None:
            continue
        if hi - lo >= CLIFF_CELL_RANGE:
            steep.add(k)
        for dx, dz in ((1, 0), (0, 1)):
            q = c4.get((k[0] + dx, k[1] + dz))
            if q is not None and q[0] is not None and abs(q[0] - mean) >= CLIFF_EDGE_STEP:
                steep.add(k)
                steep.add((k[0] + dx, k[1] + dz))
    out = []
    for comp in _components(steep, _NB8):
        if len(comp) < CLIFF_MIN_CELLS:
            continue
        lo = min(c4[k][1] for k in comp)
        hi = max(c4[k][2] for k in comp)
        if hi - lo < CLIFF_MIN_HEIGHT:
            continue
        length, width, _, _ = _shape(comp, 4)
        if length < CLIFF_MIN_LENGTH:
            continue
        cs = set(comp)
        interior = sum(1 for (a, b) in comp if all((a + dx, b + dz) in cs for dx, dz in _NB8))
        if interior / len(comp) > CLIFF_MAX_INTERIOR:
            continue
        facing = _facing(comp, c4)
        x, z = _centre_of(comp, 4)
        extra = {"facing": facing} if facing else {}
        out.append(_entry("cliff", (hi - lo) + min(length, 120) * 0.25,
                          {(k[0] >> 1, k[1] >> 1) for k in comp}, 8, x, z, lo, hi,
                          length, width, len(comp) * 16, _partial(comp, c4), **extra))
    return out


def _facing(comp, c4):
    """Compass direction the cliff drops towards (least-squares slope over band + neighbours)."""
    cells = set(comp)
    for (a, b) in comp:
        for dx, dz in _NB4:
            cells.add((a + dx, b + dz))
    pts = [(k[0], k[1], c4[k][0]) for k in cells if k in c4 and c4[k][0] is not None]
    if len(pts) < 4:
        return None
    mi = sum(p[0] for p in pts) / len(pts)
    mj = sum(p[1] for p in pts) / len(pts)
    mh = sum(p[2] for p in pts) / len(pts)
    sii = sum((p[0] - mi) ** 2 for p in pts)
    sjj = sum((p[1] - mj) ** 2 for p in pts)
    gx = sum((p[0] - mi) * (p[2] - mh) for p in pts) / sii if sii else 0.0
    gz = sum((p[1] - mj) * (p[2] - mh) for p in pts) / sjj if sjj else 0.0
    if math.hypot(gx, gz) < CLIFF_FACING_MIN_SLOPE:
        return None
    return compass(-gx, -gz)


# ---- relief forms on the 8-block grid
def _super_cells(c4):
    """{(I, J): (mean, lo, hi) | None(water)}. A chunk holds whole 8-cells, so absent = unknown."""
    groups = {}
    for (i, j), v in c4.items():
        groups.setdefault((i >> 1, j >> 1), []).append(v)
    s8 = {}
    for key, kids in groups.items():
        dry = [v for v in kids if v[0] is not None]
        wet = sum(1 for v in kids if v[3])
        if wet >= 2 or not dry:
            s8[key] = None
        else:
            s8[key] = (sum(v[0] for v in dry) / len(dry), min(v[1] for v in dry),
                       max(v[2] for v in dry))
    return s8


def _local_base(s8):
    """Smooth lower-quartile surroundings: ``{(I, J): base}`` for cells that have enough data."""
    blocks = {}
    for (i, j), v in s8.items():
        if v is not None:
            blocks.setdefault((i >> 2, j >> 2), []).append(v[0])
    bval = {}
    for key, vals in blocks.items():
        if len(vals) >= BASE_BLOCK_MIN:
            vals.sort()
            bval[key] = _percentile(vals, 0.25)
    bbase = {}
    for (bi, bj) in blocks:
        vals = [bval[(bi + a, bj + b)] for a in range(-2, 3) for b in range(-2, 3)
                if (bi + a, bj + b) in bval]
        if len(vals) >= BASE_NEIGHBOURS_MIN:
            vals.sort()
            bbase[(bi, bj)] = _percentile(vals, 0.25)
    base = {}
    for (i, j), v in s8.items():
        if v is None:
            continue
        u = (i + 0.5) / 4 - 0.5
        w = (j + 0.5) / 4 - 0.5
        u0, w0 = math.floor(u), math.floor(w)
        fu, fw = u - u0, w - w0
        acc = wsum = 0.0
        for a, b, wt in ((0, 0, (1 - fu) * (1 - fw)), (1, 0, fu * (1 - fw)),
                         (0, 1, (1 - fu) * fw), (1, 1, fu * fw)):
            val = bbase.get((u0 + a, w0 + b))
            if val is not None and wt > 0:
                acc += val * wt
                wsum += wt
        if wsum > 0:
            base[(i, j)] = acc / wsum
    return base


def _relief_forms(s8):
    base = _local_base(s8)
    prom = {k: s8[k][0] - b for k, b in base.items()}
    raised = {k for k, p in prom.items() if p >= HILL_PROM}
    out = []
    mass_cells = set()
    for comp in _components(raised, _NB4):
        if len(comp) < HILL_MIN_CELLS:
            continue
        mass_cells.update(comp)
        out.append(_mass(comp, s8, base, prom))
    out = [o for o in out if o]
    valley_cells, valleys = _valleys(s8)
    out.extend(valleys)
    out.extend(_plains(s8, prom, valley_cells))
    return out


def _mass(comp, s8, base, prom):
    cset = set(comp)
    peak_prom = max(prom[k] for k in comp)
    top = max(s8[k][2] for k in comp)
    bases = sorted(base[k] for k in comp)
    y_min = int(round(bases[len(bases) // 2]))
    length, width, _, _ = _shape(comp, 8)
    area = len(comp) * 64
    partial = _partial(comp, s8)
    peak_cell = max(comp, key=lambda k: (s8[k][0], s8[k][2]))
    px, pz = peak_cell[0] * 8 + 4, peak_cell[1] * 8 + 4

    # plateau: raised flat top with steep rims
    means = sorted(s8[k][0] for k in comp)
    t75 = _percentile(means, 0.75)
    flat = [k for k in comp
            if abs(s8[k][0] - t75) <= PLATEAU_FLAT_BAND and s8[k][2] - s8[k][1] <= PLATEAU_FLAT_RANGE]
    if len(flat) >= PLATEAU_MIN_FLAT and len(flat) / len(comp) >= PLATEAU_MIN_FLAT_FRAC:
        pairs = steep = 0
        for k in flat:
            for dx, dz in _NB4:
                q = (k[0] + dx, k[1] + dz)
                if q in cset or q not in s8 or s8[q] is None:
                    continue
                pairs += 1
                if s8[k][0] - s8[q][0] >= PLATEAU_EDGE_DROP:
                    steep += 1
        if steep >= PLATEAU_MIN_STEEP and pairs and steep / pairs >= PLATEAU_MIN_STEEP_FRAC:
            fx, fz = _centre_of(flat, 8)
            return _entry("plateau", peak_prom + 20, comp, 8, fx, fz, y_min,
                          max(s8[k][2] for k in flat), length, width, area, partial,
                          prominence=int(round(peak_prom)))

    common = dict(prominence=int(round(peak_prom)))
    if (length >= RIDGE_MIN_LENGTH and length / width >= RIDGE_MIN_ASPECT
            and width <= RIDGE_MAX_WIDTH):
        return _entry("ridge", peak_prom + length * 0.2 + 10, comp, 8, px, pz, y_min, top,
                      length, width, area, partial, **common)
    if peak_prom >= MOUNTAIN_PROM and top >= MOUNTAIN_MIN_Y:
        return _entry("mountain", peak_prom + 40, comp, 8, px, pz, y_min, top, length, width,
                      area, partial, **common)
    return _entry("hill", peak_prom, comp, 8, px, pz, y_min, top, length, width, area, partial,
                  **common)


def _valleys(s8):
    axes = ((1, 0), (0, 1), (1, 1), (1, -1))
    floor = {}
    for (i, j), v in s8.items():
        if v is None:
            continue
        best = 0.0
        for dx, dz in axes:
            sides = []
            for sgn in (1, -1):
                mx = None
                for k in range(1, VALLEY_REACH + 1):
                    q = (i + sgn * k * dx, j + sgn * k * dz)
                    if q not in s8:
                        break  # unknown ground: stop, never extrapolate
                    qv = s8[q]
                    if qv is not None and (mx is None or qv[0] > mx):
                        mx = qv[0]
                sides.append(mx)
            if sides[0] is not None and sides[1] is not None:
                depth = min(sides) - v[0]
                if depth > best:
                    best = depth
        if best >= VALLEY_DEPTH:
            floor[(i, j)] = best
    out = []
    for comp in _components(floor, _NB8):
        if len(comp) < VALLEY_MIN_CELLS:
            continue
        length, width, _, _ = _shape(comp, 8)
        if length < VALLEY_MIN_LENGTH:
            continue
        lows = [s8[k][1] for k in comp]
        depths = sorted(floor[k] for k in comp)
        depth = depths[len(depths) // 2]
        low_cell = min(comp, key=lambda k: s8[k][0])
        floor_y = min(lows)
        out.append(_entry("valley", depth + length * 0.1, comp, 8, low_cell[0] * 8 + 4,
                          low_cell[1] * 8 + 4, floor_y, floor_y + int(round(depth)),
                          length, width, len(comp) * 64, _partial(comp, s8),
                          depth=int(round(depth))))
    return set(floor), out


def _plains(s8, prom, valley_cells):
    cand = set()
    for k, p in prom.items():
        if p >= PLAIN_MAX_PROM or k in valley_cells:
            continue
        vals = [s8[(k[0] + a, k[1] + b)][0] for a in (-1, 0, 1) for b in (-1, 0, 1)
                if s8.get((k[0] + a, k[1] + b)) is not None]
        if max(vals) - min(vals) <= PLAIN_MAX_RELIEF:
            cand.add(k)
    out = []
    for comp in _components(cand, _NB4):
        if len(comp) < PLAIN_MIN_CELLS:
            continue
        length, width, _, _ = _shape(comp, 8)
        ys = [s8[k][0] for k in comp]
        x, z = _centre_of(comp, 8)
        out.append(_entry("plain", 8, comp, 8, x, z, int(math.floor(min(ys))),
                          int(math.ceil(max(ys))), length, width, len(comp) * 64,
                          _partial(comp, s8)))
    return out


# ---------------------------------------------------------------- relative view
def describe(found, x, z, limit=MAX_RESULTS, max_distance=None):
    """Rank landforms for a query point and add distance/direction to the nearest point."""
    scored = []
    for entry in found:
        best = None
        for key in entry["cells8"]:
            x0, z0, x1, z1 = _box(key, 8)
            nx = x0 if x < x0 else x1 if x > x1 else x
            nz = z0 if z < z0 else z1 if z > z1 else z
            d2 = (nx - x) ** 2 + (nz - z) ** 2
            if best is None or d2 < best[0]:
                best = (d2, nx, nz)
                if d2 == 0:
                    break
        dist = math.sqrt(best[0])
        if max_distance is not None and dist > max_distance:
            continue
        item = dict(entry["item"])
        dx, dz = best[1] - x, best[2] - z
        item["distance"] = round(dist, 1)
        item["direction"] = compass(dx, dz)
        item["nearest"] = {"x": best[1], "z": best[2]}
        item["inside"] = best[0] == 0
        scored.append((item["salience"] / (dist + 40.0), item))
    scored.sort(key=lambda s: -s[0])
    result = []
    counts = {}
    for _, item in scored:
        kind = item["kind"]
        cap = PER_KIND_CAP.get(kind, DEFAULT_PER_KIND)
        if counts.get(kind, 0) >= cap:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _cached_analysis(records, store):
    gen = int(getattr(store, "generation", 0) or 0)
    key = (tuple((k, id(r)) for k, r in records.items()), gen, id(getattr(store, "_store", store)))
    found = _memo.get(key)
    if found is None:
        found = analyze(records, gen)
        _memo[key] = found
        while len(_memo) > 16:
            _memo.popitem(last=False)
    else:
        _memo.move_to_end(key)
    return found


def for_window(records, store, x, z, limit=PERCEPTION_LIMIT):
    """Landforms for an already-loaded record window (``/perception``)."""
    return describe(_cached_analysis(records, store), int(x), int(z), limit)


def find_landforms(store, x, z, radius=DEFAULT_RADIUS):
    """``GET /landforms``: landforms within ``radius`` blocks of (x, z)."""
    radius = int(radius)
    if radius < MIN_RADIUS or radius > MAX_RADIUS:
        raise ValueError(f"Parameter 'radius' must be between {MIN_RADIUS} and {MAX_RADIUS}")
    cx1, cx2 = (x - radius) >> 4, (x + radius) >> 4
    cz1, cz2 = (z - radius) >> 4, (z + radius) >> 4
    records = {}
    missing = 0
    for cx in range(cx1, cx2 + 1):
        for cz in range(cz1, cz2 + 1):
            rec = store.get_or_load(cx, cz)
            if rec is None:
                missing += 1
            else:
                records[(cx, cz)] = rec
    core = {(cx, cz) for cx in range(cx1, cx2 + 1) for cz in range(cz1, cz2 + 1)}
    found = _cached_analysis(records, store) if records else []
    return {
        "center": {"x": x, "z": z},
        "radius": radius,
        "landforms": describe(found, x, z, MAX_RESULTS, max_distance=radius),
        "missing_chunks": missing,
        "unexplored_chunks": len(store_view.unexplored_keys(store) & core),
    }
