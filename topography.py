"""Surface-only topographic analysis over the tiered map store."""

from __future__ import annotations

import math
from collections import Counter

import mc_blocks

WATER_BLOCKS = {
    "minecraft:water",
    "minecraft:bubble_column",
    "minecraft:kelp",
    "minecraft:kelp_plant",
    "minecraft:seagrass",
    "minecraft:tall_seagrass",
} | {"minecraft:lava"}

NEIGHBORS_8 = [
    (-1, -1), (0, -1), (1, -1), (-1, 0),
    (1, 0), (-1, 1), (0, 1), (1, 1),
]
NEIGHBORS_4 = [(0, -1), (-1, 0), (1, 0), (0, 1)]


def column_index(x: int, z: int) -> int:
    return (x % 16) + (z % 16) * 16


def surface_column(store, x: int, z: int):
    data = store.get_or_load(x // 16, z // 16)
    if data is None:
        return None
    idx = column_index(x, z)
    surface = data["surface"]
    return {
        "x": x,
        "z": z,
        "cx": data["cx"],
        "cz": data["cz"],
        "y": surface["y"][idx],
        "material": surface["materials"][idx],
        "ground_y": surface["ground_y"][idx],
        "ground_material": surface["ground_materials"][idx],
        "water": surface["water"][idx],
    }


def _bresenham(x1: int, z1: int, x2: int, z2: int):
    dx = abs(x2 - x1)
    dz = abs(z2 - z1)
    sx = 1 if x1 < x2 else -1
    sz = 1 if z1 < z2 else -1
    err = dx - dz
    x, z = x1, z1
    while True:
        yield x, z
        if x == x2 and z == z2:
            break
        e2 = 2 * err
        if e2 > -dz:
            err -= dz
            x += sx
        if e2 < dx:
            err += dx
            z += sz


def _runs(points):
    """Collapse consecutive samples with the same surface into runs."""
    runs = []
    for point in points:
        if point["y"] is None:
            key = (None, None)
        else:
            key = (point["y"], point["material"])
        if runs and runs[-1]["_key"] == key:
            run = runs[-1]
            run["x2"] = point["x"]
            run["z2"] = point["z"]
            run["length"] += 1
        else:
            runs.append(
                {
                    "_key": key,
                    "x1": point["x"],
                    "z1": point["z"],
                    "x2": point["x"],
                    "z2": point["z"],
                    "y": point["y"],
                    "material": point["material"],
                    "length": 1,
                }
            )
    for run in runs:
        run.pop("_key", None)
    return runs


def heightmap_line(store, x1: int, z1: int, x2: int, z2: int):
    """Surface profile between two block coordinates (Bresenham sampling)."""
    points = []
    for x, z in _bresenham(x1, z1, x2, z2):
        point = surface_column(store, x, z)
        if point is None:
            points.append({"x": x, "z": z, "y": None, "material": None, "water": False})
        else:
            points.append(
                {
                    "x": x,
                    "z": z,
                    "y": point["y"],
                    "material": point["material"],
                    "ground_y": point["ground_y"],
                    "water": point["water"],
                }
            )

    transitions = []
    for prev, current in zip(points, points[1:]):
        if prev["y"] is None or current["y"] is None:
            continue
        if prev["water"] != current["water"]:
            transitions.append(
                {
                    "x": current["x"],
                    "z": current["z"],
                    "from": "water" if prev["water"] else "land",
                    "to": "water" if current["water"] else "land",
                }
            )

    known = [p["y"] for p in points if p["y"] is not None]
    return {
        "from": {"x": x1, "z": z1},
        "to": {"x": x2, "z": z2},
        "sample_count": len(points),
        "runs": _runs(points),
        "transitions": transitions,
        "min_y": min(known) if known else None,
        "max_y": max(known) if known else None,
        "points": points,
    }


def _window_grid(store, center_x: int, center_z: int, radius: int):
    """Return {(x,z): column-dict} for columns inside the radius."""
    radius = max(1, int(radius))
    x1, x2 = center_x - radius, center_x + radius
    z1, z2 = center_z - radius, center_z + radius
    cx1, cx2 = x1 // 16, x2 // 16
    cz1, cz2 = z1 // 16, z2 // 16

    chunks = {}
    missing = 0
    for cx in range(cx1, cx2 + 1):
        for cz in range(cz1, cz2 + 1):
            data = store.get_or_load(cx, cz)
            if data is None:
                missing += 1
            else:
                chunks[(cx, cz)] = data

    grid = {}
    r2 = radius * radius
    for x in range(x1, x2 + 1):
        for z in range(z1, z2 + 1):
            if (x - center_x) ** 2 + (z - center_z) ** 2 > r2:
                continue
            data = chunks.get((x // 16, z // 16))
            if data is None:
                continue
            idx = column_index(x, z)
            surface = data["surface"]
            grid[(x, z)] = {
                "y": surface["y"][idx],
                "material": surface["materials"][idx],
                "ground_y": surface["ground_y"][idx],
                "water": surface["water"][idx],
            }
    return grid, chunks, missing


def _find_water_component(mask, start, bounds):
    """Flood-fill the water component containing `start`. Returns a set."""
    if start not in mask or not mask[start]:
        return set()
    x1, z1, x2, z2 = bounds
    stack = [start]
    seen = {start}
    while stack:
        x, z = stack.pop()
        for dx, dz in NEIGHBORS_4:
            nb = (x + dx, z + dz)
            if nb in seen:
                continue
            if nb[0] < x1 or nb[0] > x2 or nb[1] < z1 or nb[1] > z2:
                continue
            if mask.get(nb):
                seen.add(nb)
                stack.append(nb)
    return seen


def _nearest_water_components(mask, center):
    """Pick the water component nearest (or containing) the center."""
    if mask.get(center):
        bounds = _mask_bounds(mask)
        return [_find_water_component(mask, center, bounds)]
    # Broad-phase: walk outwards in rings until a water cell is found.
    cx, cz = center
    for radius in range(1, 257):
        found = []
        for x in range(cx - radius, cx + radius + 1):
            for z in (cz - radius, cz + radius):
                if mask.get((x, z)):
                    found.append((x, z))
        for z in range(cz - radius + 1, cz + radius):
            for x in (cx - radius, cx + radius):
                if mask.get((x, z)):
                    found.append((x, z))
        if found:
            bounds = _mask_bounds(mask)
            components = []
            seen_starts = set()
            for cell in found:
                if cell in seen_starts:
                    continue
                component = _find_water_component(mask, cell, bounds)
                if component:
                    components.append(component)
                    seen_starts |= component
            return components
    return []


def _mask_bounds(mask):
    xs = [p[0] for p in mask]
    zs = [p[1] for p in mask]
    return min(xs), min(zs), max(xs), max(zs)


def _boundary_cells(component, mask):
    boundary = set()
    for cell in component:
        x, z = cell
        for dx, dz in NEIGHBORS_4:
            nb = (x + dx, z + dz)
            if not mask.get(nb):
                boundary.add(cell)
                break
    return boundary


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _order_boundary(boundary, reference):
    """Greedily walk 8-connected boundary cells into polylines."""
    remaining = set(boundary)
    segments = []
    while remaining:
        start = min(remaining, key=lambda p: (_dist2(p, reference), p[0], p[1]))
        remaining.remove(start)
        segment = [start]
        prev = None
        current = start
        while True:
            candidates = [
                (current[0] + dx, current[1] + dz)
                for dx, dz in NEIGHBORS_8
            ]
            candidates = [c for c in candidates if c in remaining]
            if not candidates:
                break
            if prev is None:
                nxt = min(candidates, key=lambda p: (_dist2(p, current), p[0], p[1]))
            else:
                direction = (current[0] - prev[0], current[1] - prev[1])

                def score(p):
                    move = (p[0] - current[0], p[1] - current[1])
                    return (
                        -(move[0] * direction[0] + move[1] * direction[1]),
                        _dist2(p, current),
                    )

                nxt = min(candidates, key=score)
            remaining.remove(nxt)
            segment.append(nxt)
            prev = current
            current = nxt
            if current == start:
                break
        segments.append(segment)
    return segments


def _perpendicular_distance(point, start, end):
    if start == end:
        return math.dist(point, start)
    x, z = point
    x1, z1 = start
    x2, z2 = end
    num = abs((z2 - z1) * x - (x2 - x1) * z + x2 * z1 - z2 * x1)
    den = math.hypot(z2 - z1, x2 - x1)
    return num / den if den else 0.0


def _rdp(points, epsilon):
    """Iterative Douglas-Peucker (recursion would blow up on long coasts)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        start, end = points[first], points[last]
        max_dist = -1.0
        index = first
        for i in range(first + 1, last):
            dist = _perpendicular_distance(points[i], start, end)
            if dist > max_dist:
                max_dist = dist
                index = i
        if max_dist > epsilon:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [point for point, kept in zip(points, keep) if kept]


def shoreline_polygon(store, center_x: int, center_z: int, radius: int):
    """Trace the nearest water body's land/water boundary as polylines."""
    grid, _, missing = _window_grid(store, center_x, center_z, radius)
    if not grid:
        return {
            "center": {"x": center_x, "z": center_z},
            "radius": radius,
            "segments": [],
            "point_count": 0,
            "water_at_center": False,
            "missing_chunks": missing,
        }

    mask = {coord: col["water"] for coord, col in grid.items()}
    center = (center_x, center_z)
    components = _nearest_water_components(mask, center)
    segments_out = []
    for component in components[:3]:
        boundary = _boundary_cells(component, mask)
        if not boundary:
            continue
        ordered = _order_boundary(boundary, center)
        for segment in sorted(ordered, key=len, reverse=True)[:1]:
            simplified = _rdp(segment, 1.5)
            segments_out.append([[int(x), int(z)] for x, z in simplified])

    segments_out.sort(
        key=lambda s: _dist2(_closest(s, center), center) if s else float("inf")
    )
    total = sum(len(s) for s in segments_out)
    return {
        "center": {"x": center_x, "z": center_z},
        "radius": radius,
        "water_at_center": bool(mask.get(center)),
        "segments": segments_out,
        "point_count": total,
        "missing_chunks": missing,
    }


def _closest(points, reference):
    return min(points, key=lambda p: _dist2(p, reference))


def area_summary(store, center_x: int, center_z: int, radius: int):
    grid, chunks, missing = _window_grid(store, center_x, center_z, radius)
    counts = Counter()
    top_blocks = Counter()
    engineered_blocks = Counter()
    indicators = Counter()
    biomes = Counter()
    artificiality = []
    heights = []
    water = 0
    highest_ground = None
    for data in chunks.values():
        comp = data.get("composition") or {}
        for key, value in (comp.get("counts") or {}).items():
            counts[key] += value
        for name, count in comp.get("top_blocks") or []:
            top_blocks[name] += count
            try:
                if mc_blocks.classify_block(name) == "engineered":
                    engineered_blocks[name] += count
            except Exception:
                pass
        for name in comp.get("strong_indicators") or []:
            indicators[name] += 1
        if comp.get("dominant_biome"):
            biomes[comp["dominant_biome"]] += 1
        artificiality.append(comp.get("artificiality_score") or 0.0)

    for (world_x, world_z), col in grid.items():
        if col["ground_y"] is not None:
            heights.append(col["ground_y"])
            if not col["water"] and (
                highest_ground is None or col["ground_y"] > highest_ground["y"]
            ):
                highest_ground = {"x": world_x, "z": world_z, "y": col["ground_y"]}
        if col["water"]:
            water += 1

    natural = counts.get("natural", 0)
    engineered = counts.get("engineered", 0)
    denom = natural + engineered
    return {
        "center": {"x": center_x, "z": center_z},
        "radius": radius,
        "columns": len(grid),
        "chunks": len(chunks),
        "missing_chunks": missing,
        "dominant_biome": biomes.most_common(1)[0][0] if biomes else None,
        "biomes": dict(biomes.most_common(6)),
        "composition": dict(counts),
        "artificiality_score": (engineered / denom) if denom else 0.0,
        "avg_chunk_artificiality": (
            sum(artificiality) / len(artificiality) if artificiality else 0.0
        ),
        "top_blocks": [[name, count] for name, count in top_blocks.most_common(10)],
        "engineered_blocks": [
            [name, count] for name, count in engineered_blocks.most_common(5)
        ],
        "highest_ground": highest_ground,
        "strong_indicators": dict(indicators.most_common(10)),
        "surface": {
            "min_y": min(heights) if heights else None,
            "max_y": max(heights) if heights else None,
            "avg_y": (sum(heights) / len(heights)) if heights else None,
            "water_fraction": (water / len(grid)) if grid else 0.0,
        },
    }


def recall(store, x: int, z: int):
    """Return the stored summary for the chunk containing (x, z)."""
    cx, cz = x // 16, z // 16
    data = store.get_or_load(cx, cz)
    if data is None:
        return None
    surface = data["surface"]
    heights = [y for y in surface["y"] if y is not None]
    water = sum(1 for flag in surface["water"] if flag)
    center_idx = column_index(x, z)
    comp = data.get("composition") or {}
    return {
        "chunk": {"cx": cx, "cz": cz},
        "chunk_bounds": {
            "x1": cx * 16, "z1": cz * 16,
            "x2": cx * 16 + 15, "z2": cz * 16 + 15,
        },
        "column": {
            "x": x,
            "z": z,
            "y": surface["y"][center_idx],
            "material": surface["materials"][center_idx],
            "ground_y": surface["ground_y"][center_idx],
            "water": surface["water"][center_idx],
        },
        "surface": {
            "min_y": min(heights) if heights else None,
            "max_y": max(heights) if heights else None,
            "avg_y": (sum(heights) / len(heights)) if heights else None,
            "water_columns": water,
        },
        "dominant_biome": comp.get("dominant_biome"),
        "composition": comp.get("counts") or {},
        "artificiality_score": comp.get("artificiality_score") or 0.0,
        "strong_indicators": comp.get("strong_indicators") or [],
        "top_blocks": comp.get("top_blocks") or [],
        "source": data.get("source"),
        "updated_at": data.get("updated_at"),
    }
