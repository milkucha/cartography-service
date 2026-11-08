"""``GET /survey``: architectural digest of a bounding box.

Buildings come from the cached surface data (``settlements.detect_buildings``), then the
REAL block sections of the chunks in the bbox are read through the loader and every block
above the local ground level inside the building footprints is bucketed by role (walls, roof,
glass, doors, fences, lighting, other_built); path-like surface blocks outside buildings give
the ``paths`` role. Landmarks: tallest, largest, bell/beacon/lectern/enchanting_table/
brewing_stand, well-like structures and bridge-like strips.

LIMITATION: palette/role counts and special-block landmarks are read from the SAVE FILE, not
from live diffs. Blocks placed or broken since the chunk was last written by the server are
invisible to the palette (the building geometry, which uses the store, may already show them).
Special blocks are searched only inside building footprints grown by 2 blocks.

Appearance tags (``tag``) are short reusable words (tower, building, well, bridge) and the
``note`` fields describe form/material plainly; nothing here names a place or a style.
"""

from __future__ import annotations

from collections import Counter

import mc_blocks
import palette
import settlements
import store_view

MAX_SIDE = 160
MAX_CHUNKS = 400
ROLES = ("walls", "roof", "glass", "doors", "fences", "lighting", "paths", "other_built")
SPECIAL = {
    "minecraft:bell": "bell", "minecraft:beacon": "beacon", "minecraft:lectern": "lectern",
    "minecraft:enchanting_table": "enchanting_table", "minecraft:brewing_stand": "brewing_stand",
}
PATH_MATERIALS = {
    "minecraft:dirt_path", "minecraft:gravel", "minecraft:cobblestone",
    "minecraft:mossy_cobblestone", "minecraft:stone_bricks",
}
LIGHT_EXACT = {
    "glowstone", "sea_lantern", "campfire", "soul_campfire", "shroomlight", "redstone_lamp",
    "jack_o_lantern", "end_rod", "lantern", "soul_lantern",
}
WALL_EXACT = {"cobblestone", "mossy_cobblestone", "bricks", "quartz_block", "hay_block"}
WALL_PREFIX = ("polished_", "smooth_", "cut_", "chiseled_", "stripped_")

_role_memo: dict = {}


def role_of(name):
    """Role of a block name, or None when it is not counted. 'roofcand' = stairs/slab."""
    if name in _role_memo:
        return _role_memo[name]
    n = name.replace("minecraft:", "")
    role = None
    if n.endswith("torch") or n.endswith("lantern") or n in LIGHT_EXACT or n.endswith("candle"):
        role = "lighting"
    elif n.endswith(("_door", "_trapdoor", "_fence_gate")):
        role = "doors"
    elif n.endswith("glass") or n.endswith("glass_pane"):
        role = "glass"
    elif n.endswith(("_fence", "_wall")) or n in ("iron_bars", "chain"):
        role = "fences"
    elif n.endswith(("_stairs", "_slab")):
        role = "roofcand"
    elif (
        n.endswith(("_planks", "_log", "_wood", "_bricks", "_concrete", "_terracotta", "_wool"))
        or "sandstone" in n
        or "prismarine" in n
        or n in WALL_EXACT
        or n.startswith(WALL_PREFIX)
    ):
        role = "walls"
    else:
        try:
            if mc_blocks.classify_block(name) == "engineered":
                role = "other_built"
        except Exception:
            role = None
    _role_memo[name] = role
    return role


def _label(name):
    return name.replace("minecraft:", "").replace("_", " ")


def _top(counter, n=5):
    return [[k, v] for k, v in counter.most_common(n)]


def _validate(x1, z1, x2, z2):
    x1, x2 = sorted((int(x1), int(x2)))
    z1, z2 = sorted((int(z1), int(z2)))
    if x2 - x1 + 1 > MAX_SIDE or z2 - z1 + 1 > MAX_SIDE:
        raise ValueError(f"Survey area too large; max {MAX_SIDE}x{MAX_SIDE} blocks")
    cx1, cx2, cz1, cz2 = x1 >> 4, x2 >> 4, z1 >> 4, z2 >> 4
    if (cx2 - cx1 + 1) * (cz2 - cz1 + 1) > MAX_CHUNKS:
        raise ValueError(f"Survey area too large; max {MAX_CHUNKS} chunks")
    return x1, z1, x2, z2, (cx1, cz1, cx2, cz2)


def _scan_sections(store, records, buildings, in_bbox_chunks):
    """Count blocks by role inside building footprints from the real sections."""
    loader = getattr(store, "loader", None)
    roles = {r: Counter() for r in ROLES}
    per_building = [Counter() for _ in buildings]
    specials = []
    if loader is None or not hasattr(loader, "read_chunk_root") or not getattr(loader, "available", False):
        return roles, per_building, specials, False

    col_info = {}  # (x, z) -> (building index, base)
    for bi, b in enumerate(buildings):
        for p, base in b["_cols"].items():
            col_info[p] = (bi, base)
    by_chunk = {}
    for p in col_info:
        by_chunk.setdefault((p[0] >> 4, p[1] >> 4), []).append(p)
    # special-block search area: footprints grown by 2
    near_by_chunk = {}
    for p in col_info:
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                q = (p[0] + dx, p[1] + dz)
                near_by_chunk.setdefault((q[0] >> 4, q[1] >> 4), set()).add(q)

    for key in sorted(set(by_chunk) | set(near_by_chunk)):
        if key not in in_bbox_chunks:
            continue
        rec = records.get(key)
        if rec is None or rec.get("frozen"):
            continue   # the save holds the chunk as it is now, not as the agent last saw it
        root = loader.read_chunk_root(key[0], key[1])
        if not root:
            continue
        cols = by_chunk.get(key, [])
        near = near_by_chunk.get(key, set())
        surf_y = rec["surface"]["y"]
        bases = [col_info[p][1] for p in cols] or [0]
        ymin = min(bases)
        ymax = max((surf_y[(p[0] & 15) + (p[1] & 15) * 16] or 0 for p in near), default=0)
        for section in root.get("sections") or []:
            if not isinstance(section, dict) or "Y" not in section:
                continue
            sy = section["Y"] * 16
            if sy + 15 <= ymin or sy > ymax:
                continue
            bs = section.get("block_states") or {}
            pal = [e.get("Name") for e in (bs.get("palette") or [])]
            data = bs.get("data")
            if len(pal) < 2 or not data:
                continue
            bits = palette.bits_needed(len(pal), minimum=4)
            if bits > 16:
                continue
            idx = palette.unpack_indices(data, bits, 4096)
            if len(idx) < 4096:
                continue
            prole = [role_of(n) if n else None for n in pal]
            pspecial = [SPECIAL.get(n) for n in pal]
            if any(prole):
                for (x, z) in cols:
                    li = (x & 15) + (z & 15) * 16
                    bi, base = col_info[(x, z)]
                    top = surf_y[li] or 0
                    for ly in range(16):
                        y = sy + ly
                        if y <= base:
                            continue
                        pi = idx[ly * 256 + li]
                        role = prole[pi]
                        if role is None:
                            continue
                        name = pal[pi]
                        if role == "roofcand":
                            role = "roof" if y >= top - 1 else "other_built"
                        roles[role][name] += 1
                        if role == "walls":
                            per_building[bi][name] += 1
            if any(pspecial):
                for (x, z) in near:
                    li = (x & 15) + (z & 15) * 16
                    for ly in range(16):
                        sp = pspecial[idx[ly * 256 + li]]
                        if sp:
                            specials.append((sp, x, sy + ly, z))
    return roles, per_building, specials, True


def _find_wells(records, feats):
    wells = []
    for key, rec in records.items():
        f = feats.get(key)
        if f is None or not f["cand"]:
            continue
        surf = rec["surface"]
        water, mats, ys = surf["water"], surf["materials"], surf["y"]
        for i in range(256):
            if not water[i]:
                continue
            x, z = key[0] * 16 + (i & 15), key[1] * 16 + (i >> 4)
            ok = True
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx == 0 and dz == 0:
                        continue
                    nx, nz = x + dx, z + dz
                    nrec = records.get((nx >> 4, nz >> 4))
                    if nrec is None:
                        ok = False
                        break
                    ni = (nx & 15) + (nz & 15) * 16
                    m = nrec["surface"]["materials"][ni]
                    if nrec["surface"]["water"][ni] or not m or not (
                        settlements.material_kind(m) or m.endswith(("_fence", "_wall"))
                    ):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                wells.append((x, ys[i], z))
    return wells


def _find_bridges(records, feats, mask_cols):
    cand = {}
    for key, f in feats.items():
        for x, z, y, m, _t in f["cand"]:
            if (x, z) not in mask_cols:
                cand[(x, z)] = y

    def is_water(x, z):
        rec = records.get((x >> 4, z >> 4))
        return bool(rec and rec["surface"]["water"][(x & 15) + (z & 15) * 16])

    def side(x, z, dx, dz):
        # first non-built column within 3 blocks must be water (bridges are <= 5 wide)
        for k in range(1, 4):
            q = (x + dx * k, z + dz * k)
            if q in cand:
                continue
            return is_water(*q)
        return False

    over = {p: y for p, y in cand.items()
            if (side(p[0], p[1], -1, 0) and side(p[0], p[1], 1, 0))
            or (side(p[0], p[1], 0, -1) and side(p[0], p[1], 0, 1))}
    bridges = []
    for comp in settlements._components(over, 1):
        xs = [p[0] for p in comp]
        zs = [p[1] for p in comp]
        long_side = max(max(xs) - min(xs), max(zs) - min(zs)) + 1
        short_side = min(max(xs) - min(xs), max(zs) - min(zs)) + 1
        if len(comp) >= 8 and long_side >= 8 and short_side <= 5:
            mid = comp[len(comp) // 2]
            bridges.append((mid[0], over[mid], mid[1], long_side, short_side))
    return bridges


def survey(store, x1, z1, x2, z2):
    x1, z1, x2, z2, (cx1, cz1, cx2, cz2) = _validate(x1, z1, x2, z2)
    core = {(cx, cz) for cx in range(cx1, cx2 + 1) for cz in range(cz1, cz2 + 1)}
    records = {}
    missing = 0
    for cx in range(cx1 - 1, cx2 + 2):
        for cz in range(cz1 - 1, cz2 + 2):
            rec = store.get_or_load(cx, cz)
            if rec is None:
                if (cx, cz) in core:
                    missing += 1
            else:
                records[(cx, cz)] = rec
    gen = int(getattr(store, "generation", 0) or 0)
    all_buildings, feats = settlements.detect_buildings(records, gen)
    buildings = [b for b in all_buildings
                 if x1 <= b["center"][0] <= x2 and z1 <= b["center"][1] <= z2]
    buildings.sort(key=lambda b: -b["footprint_area"])
    sampled = sum(1 for k in records if k in core)
    unexplored = len(store_view.unexplored_keys(store) & core)

    roles, per_building, specials, read_ok = _scan_sections(store, records, buildings, core)

    # paths: surface blocks outside buildings, near buildings (dirt_path anywhere)
    mask_cols = set()
    for b in buildings:
        mask_cols.update(b["_cols"])
    near = [(b["bbox"]["x1"] - 16, b["bbox"]["z1"] - 16, b["bbox"]["x2"] + 16, b["bbox"]["z2"] + 16)
            for b in buildings]
    for key in core:
        rec = records.get(key)
        if rec is None:
            continue
        mats = rec["surface"]["materials"]
        uniq = set(mats) & PATH_MATERIALS | {m for m in set(mats) if m and m.endswith("_planks")}
        if not uniq:
            continue
        for i, m in enumerate(mats):
            if m not in uniq:
                continue
            x, z = key[0] * 16 + (i & 15), key[1] * 16 + (i >> 4)
            if not (x1 <= x <= x2 and z1 <= z <= z2) or (x, z) in mask_cols:
                continue
            if m == "minecraft:dirt_path" or any(a <= x <= c and b_ <= z <= d for a, b_, c, d in near):
                roles["paths"][m] += 1

    palette_out = {r: _top(roles[r]) for r in ROLES}

    out_buildings = []
    for b, wc in list(zip(buildings, per_building))[:12]:
        out_buildings.append({
            "bbox": dict(b["bbox"]),
            "footprint_area": b["footprint_area"],
            "height": b["height"],
            "roof_material": b["roof_material"],
            "wall_materials": [n for n, _ in wc.most_common(2)],
        })

    n = len(buildings)
    areas = [b["footprint_area"] for b in buildings]
    heights = [b["height"] for b in buildings]
    spacing = None
    if n >= 2:
        cs = [b["center"] for b in buildings]
        nn = []
        for i, a in enumerate(cs):
            nn.append(min(((a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2) ** 0.5
                          for j, c in enumerate(cs) if j != i))
        spacing = round(sum(nn) / n, 1)
    dims = {
        "footprint_avg": round(sum(areas) / n) if n else 0,
        "footprint_max": max(areas) if n else 0,
        "height_avg": round(sum(heights) / n, 1) if n else 0,
        "height_max": max(heights) if n else 0,
        "spacing_avg": spacing,
    }

    landmarks = []
    tallest = max(buildings, key=lambda b: b["height"]) if n else None
    largest = buildings[0] if n else None
    if tallest:
        t = tallest["tallest"]
        compact = tallest["footprint_area"] <= 80 and tallest["height"] >= 10
        landmarks.append({
            "kind": "tallest", "tag": "tower" if compact else "building",
            "x": t["x"], "y": tallest["base_y"] + t["height"], "z": t["z"],
            "note": f"{t['height']} high, {_label(tallest['roof_material'])} top",
        })
    if largest and (not tallest or largest is not tallest):
        c = largest["center"]
        landmarks.append({
            "kind": "largest", "tag": "building", "x": int(c[0]), "y": largest["base_y"], "z": int(c[1]),
            "note": f"{largest['footprint_area']} columns, {_label(largest['roof_material'])} top",
        })
    seen_special = set()
    for kind, x, y, z in sorted(specials, key=lambda s: (s[0], s[1], s[3], s[2])):
        if kind in seen_special:
            continue
        seen_special.add(kind)
        landmarks.append({"kind": kind, "tag": kind, "x": x, "y": y, "z": z, "note": _label(kind)})
    for x, y, z in _find_wells(records, feats):
        if x1 <= x <= x2 and z1 <= z <= z2:
            landmarks.append({"kind": "well", "tag": "well", "x": x, "y": y, "z": z,
                              "note": "water pool ringed by built blocks"})
            break
    bridges = [br for br in _find_bridges(records, feats, mask_cols)
               if x1 <= br[0] <= x2 and z1 <= br[2] <= z2]
    if bridges:
        x, y, z, ln, wd = max(bridges, key=lambda b: b[3])
        landmarks.append({"kind": "bridge", "tag": "bridge", "x": x, "y": y, "z": z,
                          "note": f"narrow built strip over water, about {ln}x{wd}"})
    landmarks = landmarks[:8]

    summary = _summary(n, roles, buildings, dims, tallest, read_ok)
    return {
        "bbox": {"x1": x1, "z1": z1, "x2": x2, "z2": z2},
        "building_count": n,
        "buildings": out_buildings,
        "palette": palette_out,
        "dimensions": dims,
        "landmarks": landmarks,
        "summary": summary,
        "sampled_chunks": sampled,
        "missing_chunks": missing,
        "unexplored_chunks": unexplored,
    }


def _summary(n, roles, buildings, dims, tallest, read_ok):
    if n == 0:
        return "no buildings detected"
    parts = [f"{n} building{'s' if n != 1 else ''}"]
    walls = [_label(k) for k, _ in roles["walls"].most_common(2)]
    roof = roles["roof"].most_common(1)
    if walls and roof:
        r = _label(roof[0][0])
        r = r[:-1] if r.endswith("stairs") else r
        parts.append(f"{'/'.join(walls)} walls, {r} roofs")
    elif walls:
        parts.append(f"{'/'.join(walls)} walls")
    else:
        top = Counter(b["roof_material"] for b in buildings).most_common(1)[0][0]
        parts.append(f"{_label(top)} tops")
    w = round(sum(b["bbox"]["x2"] - b["bbox"]["x1"] + 1 for b in buildings) / n)
    d = round(sum(b["bbox"]["z2"] - b["bbox"]["z1"] + 1 for b in buildings) / n)
    parts.append(f"avg {w}x{d}, up to {dims['height_max']} high")
    light = [_label(k) for k, _ in roles["lighting"].most_common(2)]
    if light:
        parts.append("+".join(light))
    paths = [_label(k) for k, _ in roles["paths"].most_common(1)]
    if paths:
        label = paths[0][:-5] if paths[0].endswith(" path") else paths[0]
        parts.append(f"{label} paths")
    if tallest:
        t = tallest["tallest"]
        tag = "tower" if tallest["footprint_area"] <= 80 and tallest["height"] >= 10 else "building"
        parts.append(f"tallest {tag} at ({t['x']},{t['z']})")
    text = "; ".join(parts)
    return text[:400]
