"""Settlement detection from cached surface data (no section reads).

Chunk records carry ``terrain_y``, the height of the natural ground under each column
(roofs, floors and decks are skipped by ``terrain_rules``). A column is a *structure* column when

  * its surface material is a built material (planks, stairs, slabs, bricks, wool, concrete,
    glass, cobblestone, logs ...; natural ground/leaves/vegetation never qualify), and
  * its surface sits at least 2 blocks above its own ``terrain_y`` (building height =
    ``y - terrain_y``). Only rows without ``terrain_y`` (legacy) fall back to a local
    baseline (an erosion = minimum of the surrounding ground over about +-10 blocks).

Structure columns are grouped into buildings (connected components, 8-neighbour with a small
gap tolerance); trees (small log-only components), thin strips and sparse scatter are
dropped. Buildings within ~LINK_GAP blocks of each other form a settlement (union-find).
Rails, torches and paths never create a settlement on their own: only roofed structures do,
and the per-chunk strong indicators used as supporting evidence are "domestic" ones only
(beds, bells, chests, furnaces, crafting tables ...), never rails/torches/ladders.

Low confidence means "weak signal", not "confirmed small or absent": sprawling or scattered
builds are under-counted by density methods, so fragmented clusters carry an evidence note.

Results are memoised by (window chunk set, store generation).
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, OrderedDict

import store_view

GAP = 2              # building columns closer than GAP+1 blocks (Chebyshev) are linked
LINK_GAP = 32        # bbox gap (blocks) under which buildings belong to one settlement
MIN_AREA = 6
MIN_SIDE = 3
MIN_FILL = 0.15
MIN_HEIGHT = 2
BASE_CELL_RADIUS = 2  # erosion radius in 4x4-block cells (~ +-10 blocks)
MAX_SETTLEMENTS = 8
MAX_RADIUS = 256
MIN_RADIUS = 16
BIG = 10 ** 6

STRONG_SUFFIX = (
    "_planks", "_stairs", "_slab", "_wool", "_concrete", "_glazed_terracotta", "_bricks",
    "_stained_glass",
)
STRONG_EXACT = {
    "minecraft:cobblestone", "minecraft:mossy_cobblestone", "minecraft:bricks",
    "minecraft:glass", "minecraft:tinted_glass", "minecraft:hay_block", "minecraft:bookshelf",
    "minecraft:quartz_block", "minecraft:smooth_stone", "minecraft:iron_block",
    "minecraft:prismarine", "minecraft:purpur_block", "minecraft:honeycomb_block",
}
STRONG_PREFIX = ("minecraft:polished_", "minecraft:cut_", "minecraft:smooth_", "minecraft:chiseled_",
                 "minecraft:stripped_")
WEAK_EXACT = {"minecraft:sandstone", "minecraft:red_sandstone"}

DOMESTIC_SUFFIX = ("_bed",)
DOMESTIC_EXACT = {
    "minecraft:bell": "bell", "minecraft:chest": "chest", "minecraft:trapped_chest": "chest",
    "minecraft:barrel": "barrel", "minecraft:furnace": "furnace",
    "minecraft:blast_furnace": "furnace", "minecraft:smoker": "furnace",
    "minecraft:crafting_table": "crafting table", "minecraft:lectern": "lectern",
    "minecraft:loom": "loom", "minecraft:composter": "composter",
    "minecraft:cartography_table": "workstation", "minecraft:fletching_table": "workstation",
    "minecraft:smithing_table": "workstation", "minecraft:grindstone": "workstation",
    "minecraft:stonecutter": "workstation", "minecraft:brewing_stand": "brewing stand",
    "minecraft:jukebox": "jukebox",
}

_kind_memo: dict = {}


def material_kind(name):
    """0 = not built, 1 = built, 2 = weak (maybe natural), 3 = wood (log)."""
    if not name:
        return 0
    k = _kind_memo.get(name)
    if k is not None:
        return k
    if name.endswith(("_log", "_wood")):
        k = 3
        if name.startswith("minecraft:stripped_"):
            k = 1
    elif name in WEAK_EXACT:
        k = 2
    elif (
        name in STRONG_EXACT
        or name.endswith(STRONG_SUFFIX)
        or name.startswith(STRONG_PREFIX)
    ):
        k = 1
    else:
        k = 0
    if "stained_glass_pane" in name or name.endswith("_brick_wall"):
        k = 0
    _kind_memo[name] = k
    return k


# --------------------------------------------------------------- chunk features
_feat_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_FEAT_LIMIT = 6000


def _terrain(surface):
    """Per-column natural ground height (``ground_y`` for rows without terrain_y)."""
    ty = surface.get("terrain_y")
    return ty if ty is not None else surface["ground_y"]


def _features(rec, gen):
    key = (rec["cx"], rec["cz"], rec.get("rev"))
    hit = _feat_cache.get(key)
    if hit is not None and hit[0] is rec and hit[1] == gen:
        return hit[2]
    surf = rec["surface"]
    mats = surf["materials"]
    ys = surf["y"]
    tys = surf.get("terrain_y") or [None] * 256
    uniq = set(mats)
    built = {m for m in uniq if m and material_kind(m)}
    cx0, cz0 = rec["cx"] * 16, rec["cz"] * 16
    cand = []
    if built:
        for i, m in enumerate(mats):
            if m in built:
                y = ys[i]
                if y is None:
                    continue
                cand.append((cx0 + (i & 15), cz0 + (i >> 4), y, m, tys[i]))
    paths = []
    if "minecraft:dirt_path" in uniq:
        paths = [(cx0 + (i & 15), cz0 + (i >> 4)) for i, m in enumerate(mats)
                 if m == "minecraft:dirt_path"]
    feat = {"cand": cand, "paths": paths, "cells": None, "rec": rec}
    _feat_cache[key] = (rec, gen, feat)
    _feat_cache.move_to_end(key)
    while len(_feat_cache) > _FEAT_LIMIT:
        _feat_cache.popitem(last=False)
    return feat


def _cells(feat):
    """16 cell minima (4x4 blocks each) of ground level; water counts at its surface."""
    cells = feat["cells"]
    if cells is not None:
        return cells
    surf = feat["rec"]["surface"]
    ys, water, tys = surf["y"], surf["water"], _terrain(surf)
    g = []
    for i in range(256):
        v = ys[i] if water[i] else tys[i]
        g.append(BIG if v is None else v)
    cells = []
    for cz4 in range(4):
        for cx4 in range(4):
            base = cz4 * 64 + cx4 * 4
            cells.append(min(min(g[base + r * 16: base + r * 16 + 4]) for r in range(4)))
    feat["cells"] = cells
    return cells


# ------------------------------------------------------------------- detection
def _components(mask, gap):
    offs = [(dx, dz) for dx in range(-gap, gap + 1) for dz in range(-gap, gap + 1)
            if (dx, dz) != (0, 0)]
    seen = set()
    comps = []
    for start in mask:
        if start in seen:
            continue
        seen.add(start)
        stack = [start]
        comp = []
        while stack:
            p = stack.pop()
            comp.append(p)
            px, pz = p
            for dx, dz in offs:
                q = (px + dx, pz + dz)
                if q in mask and q not in seen:
                    seen.add(q)
                    stack.append(q)
        comps.append(comp)
    return comps


def detect_buildings(records, gen=0):
    """Return buildings detected in ``records`` ({(cx, cz): chunk record})."""
    feats = {k: _features(r, gen) for k, r in records.items()}
    cell_memo = {}

    def cell_val(ci, cj):
        f = feats.get((ci >> 2, cj >> 2))
        if f is None:
            return BIG
        return _cells(f)[(cj & 3) * 4 + (ci & 3)]

    def baseline(ci, cj):
        v = cell_memo.get((ci, cj))
        if v is None:
            v = min(
                cell_val(ci + a, cj + b)
                for a in range(-BASE_CELL_RADIUS, BASE_CELL_RADIUS + 1)
                for b in range(-BASE_CELL_RADIUS, BASE_CELL_RADIUS + 1)
            )
            cell_memo[(ci, cj)] = v
        return v

    cand_cols = {(x, z) for f in feats.values() for x, z, _y, _m, _t in f["cand"]}

    def water_level(x, z):
        """Surface y if (x, z) is a loaded water column, else None."""
        f = feats.get((x >> 4, z >> 4))
        if f is None:
            return None
        surf = f["rec"]["surface"]
        i = (x & 15) + (z & 15) * 16
        return surf["y"][i] if surf["water"][i] else None

    def deck_level(x, z):
        """Water surface under a built column that spans water (pier/bridge deck).

        Terrain under such a column is the water bed, which would read as a very tall
        building; the deck stands on the water, so measure from its surface instead.
        """
        for dx, dz in ((1, 0), (0, 1)):
            levels = []
            for sgn in (-1, 1):
                lv = None
                for k in range(1, 4):
                    q = (x + sgn * dx * k, z + sgn * dz * k)
                    if q in cand_cols:
                        continue
                    lv = water_level(*q)
                    break
                levels.append(lv)
            if levels[0] is not None and levels[1] is not None:
                return max(levels)
        return None

    mask = {}
    for f in feats.values():
        for x, z, y, m, ty in f["cand"]:
            base = ty if ty is not None else baseline(x >> 2, z >> 2)
            if base >= BIG:
                continue
            if ty is not None and y - ty >= MIN_HEIGHT:
                wl = deck_level(x, z)
                if wl is not None and wl > base:
                    base = wl
            h = y - base
            if h >= MIN_HEIGHT:
                mask[(x, z)] = (h, m, base)

    buildings = []
    for comp in _components(mask, GAP):
        area = len(comp)
        if area < MIN_AREA:
            continue
        xs = [p[0] for p in comp]
        zs = [p[1] for p in comp]
        x1, x2, z1, z2 = min(xs), max(xs), min(zs), max(zs)
        bw, bd = x2 - x1 + 1, z2 - z1 + 1
        if min(bw, bd) < MIN_SIDE or area < MIN_FILL * bw * bd:
            continue
        mats = Counter(mask[p][1] for p in comp)
        kinds = Counter()
        for m, n in mats.items():
            kinds[material_kind(m)] += n
        if kinds[1] == 0 and kinds[3] and area <= 12:
            continue  # trunk-like: tiny log-only component
        if kinds[1] == 0 and kinds[3] and kinds[3] == area and area <= 16:
            continue
        if kinds[1] == 0:
            continue  # only weak (sandstone) / wood evidence: could be rock or trees
        top = max(comp, key=lambda p: mask[p][0])
        bases = sorted(mask[p][2] for p in comp)
        buildings.append({
            "bbox": {"x1": x1, "z1": z1, "x2": x2, "z2": z2},
            "footprint_area": area,
            "height": mask[top][0],
            "roof_material": mats.most_common(1)[0][0],
            "materials": [m for m, _ in mats.most_common(3)],
            "center": ((x1 + x2) / 2.0, (z1 + z2) / 2.0),
            "tallest": {"x": top[0], "z": top[1], "height": mask[top][0]},
            "base_y": bases[len(bases) // 2],
            "_cols": {p: mask[p][2] for p in comp},
        })
    buildings.sort(key=lambda b: (b["bbox"]["x1"], b["bbox"]["z1"]))
    return buildings, feats


def _bbox_gap(a, b):
    dx = max(a["x1"] - b["x2"], b["x1"] - a["x2"], 0)
    dz = max(a["z1"] - b["z2"], b["z1"] - a["z2"], 0)
    return max(dx, dz)


def cluster_buildings(buildings, link=LINK_GAP):
    n = len(buildings)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(n), key=lambda i: buildings[i]["bbox"]["x1"])
    for ai, i in enumerate(order):
        bi = buildings[i]["bbox"]
        for j in order[ai + 1:]:
            bj = buildings[j]["bbox"]
            if bj["x1"] - bi["x2"] > link:
                break
            if _bbox_gap(bi, bj) <= link:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(buildings[i])
    return list(groups.values())


def settlement_id(cx, cz):
    gx, gz = int(round(cx / 32.0)), int(round(cz / 32.0))
    return "s:" + hashlib.sha1(f"{gx},{gz}".encode()).hexdigest()[:10]


def _domestic(feats, bbox, margin=8):
    """Counts of domestic indicators in chunks overlapping the bbox, rails excluded."""
    cx1, cx2 = (bbox["x1"] - margin) >> 4, (bbox["x2"] + margin) >> 4
    cz1, cz2 = (bbox["z1"] - margin) >> 4, (bbox["z2"] + margin) >> 4
    counts = Counter()
    paths = 0
    px1, px2, pz1, pz2 = bbox["x1"] - 16, bbox["x2"] + 16, bbox["z1"] - 16, bbox["z2"] + 16
    for cx in range(cx1, cx2 + 1):
        for cz in range(cz1, cz2 + 1):
            f = feats.get((cx, cz))
            if f is None:
                continue
            sc = (f["rec"].get("composition") or {}).get("strong_counts") or {}
            for name, n in sc.items():
                if name.endswith(DOMESTIC_SUFFIX):
                    counts["bed"] += n
                elif name in DOMESTIC_EXACT:
                    counts[DOMESTIC_EXACT[name]] += n
            paths += sum(1 for x, z in f["paths"] if px1 <= x <= px2 and pz1 <= z <= pz2)
    return counts, paths


def _build_settlements(records, gen):
    buildings, feats = detect_buildings(records, gen)
    if records:
        wx1 = min(k[0] for k in records) * 16
        wx2 = max(k[0] for k in records) * 16 + 15
        wz1 = min(k[1] for k in records) * 16
        wz2 = max(k[1] for k in records) * 16 + 15
    out = []
    for group in cluster_buildings(buildings):
        b = len(group)
        x1 = min(g["bbox"]["x1"] for g in group)
        x2 = max(g["bbox"]["x2"] for g in group)
        z1 = min(g["bbox"]["z1"] for g in group)
        z2 = max(g["bbox"]["z2"] for g in group)
        bbox = {"x1": x1, "z1": z1, "x2": x2, "z2": z2}
        total = sum(g["footprint_area"] for g in group)
        cx = sum(g["center"][0] * g["footprint_area"] for g in group) / total
        cz = sum(g["center"][1] * g["footprint_area"] for g in group) / total
        dom, paths = _domestic(feats, bbox)
        dom_types = len(dom)
        dom_total = sum(dom.values())
        if b >= 3 or (b == 2 and dom_types >= 2):
            kind = "settlement"
            conf = 0.4 + 0.04 * min(b, 10) + (0.1 if dom_types >= 2 else 0) + (0.1 if paths >= 10 else 0)
        else:
            kind = "outpost"
            conf = 0.2 + 0.1 * (b - 1) + (0.15 if dom_types >= 1 else 0) + (0.05 if paths >= 10 else 0)
        evidence = [f"{b} roofed structure{'s' if b != 1 else ''}"]
        for label, n in dom.most_common(4):
            evidence.append(f"{n} {label}{'s' if n != 1 and not label.endswith('s') else ''}")
        if paths:
            evidence.append(f"{paths} path columns")
        area = (x2 - x1 + 1) * (z2 - z1 + 1)
        if b >= 3 and area and total / area < 0.04:
            evidence.append("fragmented layout: spread-out buildings, count may be an undercount")
        elif b < 3:
            evidence.append("weak signal: low confidence does not mean small or absent")
        if x1 - 8 <= wx1 or z1 - 8 <= wz1 or x2 + 8 >= wx2 or z2 + 8 >= wz2:
            evidence.append("touches scanned window edge: extent may be larger")
        tallest = max(group, key=lambda g: g["height"])["tallest"]
        out.append({
            "id": settlement_id(cx, cz),
            "kind": kind,
            "center": {"x": int(round(cx)), "z": int(round(cz))},
            "bbox": bbox,
            "building_count": b,
            "built_columns": total,
            "area": area,
            "tallest": dict(tallest),
            "confidence": round(min(conf, 0.95), 2),
            "evidence": evidence,
        })
    return out


_memo: "OrderedDict[tuple, list]" = OrderedDict()


def compass(dx, dz):
    """8-way direction with north = -z."""
    if dx == 0 and dz == 0:
        return "here"
    ang = math.degrees(math.atan2(dx, -dz)) % 360
    names = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
    return names[int((ang + 22.5) // 45) % 8]


def find_settlements(store, x, z, radius=96):
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
    gen = int(getattr(store, "generation", 0) or 0)
    key = (tuple((k, id(r), r.get("rev")) for k, r in records.items()), gen, id(getattr(store, "_store", store)))
    found = _memo.get(key)
    if found is None:
        found = _build_settlements(records, gen)
        _memo[key] = found
        while len(_memo) > 16:
            _memo.popitem(last=False)
    else:
        _memo.move_to_end(key)
    core = {(cx, cz) for cx in range(cx1, cx2 + 1) for cz in range(cz1, cz2 + 1)}
    unexplored = len(store_view.unexplored_keys(store) & core)

    result = []
    for s in found:
        c = s["center"]
        dx, dz = c["x"] - x, c["z"] - z
        b = s["bbox"]
        item = dict(s)
        item["distance"] = round(math.hypot(dx, dz), 1)
        item["direction"] = compass(dx, dz)
        item["inside"] = (b["x1"] - 16 <= x <= b["x2"] + 16 and b["z1"] - 16 <= z <= b["z2"] + 16)
        result.append(item)
    result.sort(key=lambda s: s["distance"])
    return {
        "center": {"x": x, "z": z},
        "radius": radius,
        "settlements": result[:MAX_SETTLEMENTS],
        "missing_chunks": missing,
        "unexplored_chunks": unexplored,
    }
