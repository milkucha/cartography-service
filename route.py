"""``GET /route``: coarse long-range route over the chunks one agent has SEEN.

Plans on a grid of 4x4-block cells summarised from stored columns, through a gated
``StoreView`` (unseen chunks are not traversable knowledge and are never read). Where the way
or the target is unseen the route runs as far as is known and says so (``partial``); it never
implies anything about what lies beyond.

Costs (per block, multiplied): water ``WATER_COST``, steep ground adds ``ROUGH_COST`` per block
of relief and ``CLIMB_COST`` per block of height step, buildings ``BUILT_COST``; bridges
``BRIDGE_COST`` and paths ``PATH_COST`` are cheaper than open ground. Natural ground ``terrain_y``
is used for terrain, the deck height ``y`` for bridge decks.

Bridges and paths are detected directly from the column data (:func:`Grid.detect_chunk`): a bridge
is a straight, narrow, connected strip of elevated built blocks with walkable land at BOTH ends
(a pier into the water has none). Waypoints are placed at bends, shore edges, bridge ends and deck,
crests and path entries, with seeded jitter among columns that are dry and not on a cliff edge.
Stdlib only. Work is bounded by MAX_NODES, TIME_LIMIT_S, MAX_CHUNKS and MAX_DISTANCE.
"""

from __future__ import annotations

import heapq
import math
import random
import time
import zlib
from statistics import median

import perception
import terrain_queries

CELL = 4
MAX_DISTANCE = 1536          # straight-line start->target; farther targets are clamped (reason "distance")
SEARCH_PAD = 96              # blocks of search area around the start/target box
MAX_NODES = 40000            # A* expansions
TIME_LIMIT_S = 1.5
MAX_CHUNKS = 3000            # distinct chunks examined
MAX_WAYPOINTS = 64
MAX_LEG = 48                 # longest gap between waypoints (grows for very long routes)
MIN_KNOWN_COLUMNS = 8        # of 16 columns for a cell to count as known

# costs
WATER_COST = 5.0
ROUGH_COST = 0.25            # extra multiplier per block of relief above 2 inside a cell
CLIMB_COST = 1.5             # per block of height step above 1 between neighbouring cells
BUILT_COST = 2.5
BRIDGE_COST = 0.6
PATH_COST = 0.55
SHORE_COST = 1.3
CLIFF_RELIEF = 7             # in-cell relief at or above this: not walkable
MAX_STEP = 8                 # height step between neighbouring cells at or above this: not walkable
HEURISTIC_SCALE = 0.55       # cheapest per-block cost (path); keeps the heuristic admissible
HEURISTIC_WEIGHT = 1.5       # weighted A*: bounded work, near-optimal routes

# bridge detection
BRIDGE_MIN_LENGTH = 5
BRIDGE_MAX_WIDTH = 6
BRIDGE_MAX_COMPONENT = 600
BRIDGE_END_DY = 2            # |land ground - deck| allowed at each end
BRIDGE_FILL = 0.6
BRIDGE_DECK_STEP = 12
BRIDGE_SNAP = 4

# waypoints
JITTER_RADIUS = 2.5          # columns from the cell centre a waypoint may be moved
CREST_RISE = 6
PATH_BLOCKS = frozenset({
    "minecraft:dirt_path", "minecraft:cobblestone", "minecraft:mossy_cobblestone",
    "minecraft:stone_bricks",
})

_NEIGHBOURS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


class Grid:
    """Lazy, per-request cell/bridge model over a gated view."""

    def __init__(self, view, max_chunks=MAX_CHUNKS):
        self.view = view
        self.max_chunks = max_chunks
        self._recs = {}
        self._cells = {}
        self._detected = set()
        self._deck_seen = set()
        self.bridge_of = {}
        self.bridges = {}
        self.limit_hit = False

    # ------------------------------------------------------------ columns
    def chunk(self, cx, cz):
        key = (cx, cz)
        if key in self._recs:
            return self._recs[key]
        if len(self._recs) >= self.max_chunks:
            self.limit_hit = True
            return None
        data = self.view.get_or_load(cx, cz)
        entry = None
        if data is not None:
            s = data["surface"]
            ty, tm = perception.terrain_arrays(s)
            entry = (s["y"], s["materials"], ty, tm, s["water"])
        self._recs[key] = entry
        return entry

    def known_chunk(self, cx, cz):
        return self.chunk(cx, cz) is not None

    def col(self, x, z):
        e = self.chunk(x >> 4, z >> 4)
        if e is None:
            return None
        i = (x & 15) + (z & 15) * 16
        y = e[0][i]
        if y is None:
            return None
        ty = e[2][i] if i < len(e[2]) else None
        return (y, e[1][i], ty, e[3][i] if i < len(e[3]) else None, bool(e[4][i]))

    def is_deck_candidate(self, c):
        y, mat, ty, _tm, water = c
        return (not water and ty is not None and y - ty >= 2 and bool(mat)
                and terrain_queries._is_engineered(mat))

    # ------------------------------------------------------------ bridges
    def detect_chunk(self, cx, cz):
        if (cx, cz) in self._detected:
            return
        self._detected.add((cx, cz))
        if not self.known_chunk(cx, cz):
            return
        for lz in range(16):
            for lx in range(16):
                p = (cx * 16 + lx, cz * 16 + lz)
                if p in self._deck_seen:
                    continue
                c = self.col(*p)
                if c is None or not self.is_deck_candidate(c):
                    continue
                self._flood(p)

    def _flood(self, start):
        comp = {}
        stack = [start]
        self._deck_seen.add(start)
        unknown = False
        while stack:
            p = stack.pop()
            c = self.col(*p)
            comp[p] = c[0]
            if len(comp) > BRIDGE_MAX_COMPONENT:
                unknown = True  # too big to be a bridge: leave the rest unexplored
                break
            for dx, dz in _NEIGHBOURS[:4]:
                q = (p[0] + dx, p[1] + dz)
                if q in self._deck_seen:
                    continue
                n = self.col(*q)
                if n is None:
                    unknown = unknown or not self.known_chunk(q[0] >> 4, q[1] >> 4)
                    continue
                if self.is_deck_candidate(n) and abs(n[0] - c[0]) <= 1:
                    self._deck_seen.add(q)
                    stack.append(q)
        if not unknown:
            self._classify(comp)

    def _land_end(self, p, deck_y, comp):
        """Walkable natural ground at ``p`` close to the deck height (a bridge end), else False."""
        if p in comp:
            return False
        c = self.col(*p)
        if c is None or c[4] or c[2] is None:
            return False
        if "lava" in (c[1] or ""):
            return False
        if self.is_deck_candidate(c):
            return False
        return abs(c[2] - deck_y) <= BRIDGE_END_DY

    def _classify(self, comp):
        xs = [p[0] for p in comp]
        zs = [p[1] for p in comp]
        x1, x2, z1, z2 = min(xs), max(xs), min(zs), max(zs)
        w, d = x2 - x1 + 1, z2 - z1 + 1
        length, width = max(w, d), min(w, d)
        if length < BRIDGE_MIN_LENGTH or width > BRIDGE_MAX_WIDTH:
            return
        if len(comp) < BRIDGE_FILL * w * d:
            return
        axis = "x" if w >= d else "z"
        lo, hi = (x1, x2) if axis == "x" else (z1, z2)
        line = []
        for a in range(lo, hi + 1):
            cross = [p for p in comp if (p[0] if axis == "x" else p[1]) == a]
            if not cross:
                return  # gap along the axis: not one straight strip
            other = sorted((p[1] if axis == "x" else p[0]) for p in cross)
            b = other[len(other) // 2]
            y = int(median(comp[p] for p in cross))
            line.append((a, b, y) if axis == "x" else (b, a, y))
        # (x, z, y) centreline
        ends_ok = []
        for end in (lo, hi):
            sign = -1 if end == lo else 1
            row = [p for p in comp if (p[0] if axis == "x" else p[1]) == end]
            ok = False
            for p in row:
                q = (p[0] + sign, p[1]) if axis == "x" else (p[0], p[1] + sign)
                if self._land_end(q, comp[p], comp):
                    ok = True
                    break
            ends_ok.append(ok)
        if not all(ends_ok):
            return  # a pier or roof: needs walkable land at both ends
        bid = f"b{len(self.bridges) + 1}"
        self.bridges[bid] = {
            "id": bid, "axis": axis, "centreline": line, "width": width, "length": length,
            "deck_y": int(median(comp.values())),
        }
        for p in comp:
            self.bridge_of[p] = bid

    # -------------------------------------------------------------- cells
    def cell(self, ci, cj):
        key = (ci, cj)
        if key in self._cells:
            return self._cells[key]
        cx, cz = ci >> 2, cj >> 2
        if not self.known_chunk(cx, cz):
            self._cells[key] = None
            return None
        self.detect_chunk(cx, cz)
        x0, z0 = ci * CELL, cj * CELL
        known = water = lava = built = pathc = 0
        land, wlev, deck, bids = [], [], [], set()
        for z in range(z0, z0 + CELL):
            for x in range(x0, x0 + CELL):
                c = self.col(x, z)
                if c is None:
                    continue
                known += 1
                y, mat, ty, tmat, is_water = c
                bid = self.bridge_of.get((x, z))
                if bid:
                    deck.append(y)
                    bids.add(bid)
                elif is_water:
                    water += 1
                    wlev.append(y)
                    if "lava" in (mat or ""):
                        lava += 1
                elif ty is not None:
                    land.append(ty)
                    if y - ty >= 2 and mat and terrain_queries._is_engineered(mat):
                        built += 1
                    elif (tmat in PATH_BLOCKS) or (y == ty and mat in PATH_BLOCKS):
                        pathc += 1
        if known < MIN_KNOWN_COLUMNS:
            self._cells[key] = None
            return None
        cell = {"known": known, "bids": bids}
        if len(deck) >= 4:
            cell.update(kind="bridge", h=int(median(deck)), relief=max(deck) - min(deck),
                        mult=BRIDGE_COST, bid=sorted(bids)[0])
        elif lava:
            cell.update(kind="lava", h=int(median(wlev)), relief=0, mult=None)
        elif water * 2 >= known:
            cell.update(kind="water", h=int(median(wlev)), relief=0, mult=WATER_COST)
        elif not land:
            self._cells[key] = None
            return None
        else:
            relief = max(land) - min(land)
            h = int(median(land))
            mult = 1.0 + ROUGH_COST * max(0, relief - 2)
            kind = "land"
            if relief >= CLIFF_RELIEF:
                kind = "cliff"
                mult = None
            else:
                if pathc * 2 >= len(land):
                    kind, mult = "path", PATH_COST
                elif built * 4 >= len(land):
                    mult *= BUILT_COST
                if water:
                    mult *= SHORE_COST
            cell.update(kind=kind, h=h, relief=relief, mult=mult, shore=bool(water))
        self._cells[key] = cell
        return cell


def _octile(a, b):
    dx, dz = abs(a[0] - b[0]), abs(a[1] - b[1])
    return CELL * (max(dx, dz) + (math.sqrt(2) - 1) * min(dx, dz))


def _edge_cost(c1, c2, diagonal):
    if c1["mult"] is None or c2["mult"] is None:
        return None
    k1, k2 = c1["kind"], c2["kind"]
    if (k1 == "bridge") != (k2 == "bridge") and "water" in (k1, k2):
        return None  # a deck is entered from land or deck, never from the water beside it
    dh = abs(c1["h"] - c2["h"])
    if dh >= MAX_STEP:
        return None
    dist = CELL * (math.sqrt(2) if diagonal else 1)
    return dist * (c1["mult"] + c2["mult"]) / 2 + max(0, dh - 1) * CLIMB_COST


def _search(grid, start, goal, bounds, now, deadline, max_nodes):
    """Weighted A* over known cells. Returns (path|None, best_path, info)."""
    ci0, cj0, ci1, cj1 = bounds
    g = {start: 0.0}
    parent = {}
    heap = [(HEURISTIC_WEIGHT * HEURISTIC_SCALE * _octile(start, goal), 0.0, 0, start)]
    closed = set()
    counter = 0
    best, best_h = start, _octile(start, goal)
    frontier = set()
    reason = None
    nodes = 0
    while heap:
        _f, _h, _t, cur = heapq.heappop(heap)
        if cur in closed:
            continue
        closed.add(cur)
        nodes += 1
        if cur == goal:
            return _rebuild(parent, cur), cur, {"nodes": nodes, "reason": None, "frontier": frontier}
        h = _octile(cur, goal)
        if h < best_h:
            best, best_h = cur, h
        if nodes >= max_nodes:
            reason = "limit"
            break
        if nodes % 128 == 0 and now() > deadline:
            reason = "limit"
            break
        if grid.limit_hit:
            reason = "limit"
            break
        c1 = grid.cell(*cur)
        for di, dj in _NEIGHBOURS:
            nb = (cur[0] + di, cur[1] + dj)
            if not (ci0 <= nb[0] <= ci1 and cj0 <= nb[1] <= cj1) or nb in closed:
                continue
            c2 = grid.cell(*nb)
            if c2 is None:
                if not grid.known_chunk(nb[0] >> 2, nb[1] >> 2):
                    frontier.add(cur)
                continue
            diagonal = di != 0 and dj != 0
            if nb == goal and c2["kind"] != "lava":
                # a goal in water/cliff/built cells is reachable: we report it, the bot decides
                c2 = dict(c2, mult=c2["mult"] if c2["mult"] is not None else WATER_COST)
            if diagonal:
                o1, o2 = grid.cell(cur[0] + di, cur[1]), grid.cell(cur[0], cur[1] + dj)
                if not any(o is not None and o["mult"] is not None for o in (o1, o2)):
                    continue
            cost = _edge_cost(c1, c2, diagonal)
            if cost is None:
                continue
            ng = g[cur] + cost
            if ng < g.get(nb, math.inf):
                g[nb] = ng
                parent[nb] = cur
                counter += 1
                nh = _octile(nb, goal)
                heapq.heappush(heap, (ng + HEURISTIC_WEIGHT * HEURISTIC_SCALE * nh, nh, counter, nb))
    return None, best, {"nodes": nodes, "reason": reason, "frontier": frontier, "parent": parent}


def _rebuild(parent, cur):
    path = [cur]
    while cur in parent:
        cur = parent[cur]
        path.append(cur)
    path.reverse()
    return path


def _line_cells(a, b):
    n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    if n == 0:
        return [a]
    return [(a[0] + round((b[0] - a[0]) * i / n), a[1] + round((b[1] - a[1]) * i / n)) for i in range(n + 1)]


def _perp(p, a, b):
    dx, dz = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dz)
    if n == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    return abs(dx * (a[1] - p[1]) - (a[0] - p[0]) * dz) / n


def _simplify(run, ok_cell, eps=1.0):
    """Indices (into ``run``) of the points to keep: bends, plus splits where a straight line is not clear."""
    keep = {0, len(run) - 1}

    def rec(i, j):
        if j - i < 2:
            return
        far, dist = i, -1.0
        for k in range(i + 1, j):
            d = _perp(run[k], run[i], run[j])
            if d > dist:
                far, dist = k, d
        clear = all(ok_cell(c) for c in _line_cells(run[i], run[j]))
        if dist > eps or not clear:
            keep.add(far)
            rec(i, far)
            rec(far, j)

    rec(0, len(run) - 1)
    return sorted(keep)


class _Planner:
    def __init__(self, grid, rng):
        self.grid = grid
        self.rng = rng

    def cell_of(self, x, z):
        return (x // CELL, z // CELL)

    def safe(self, x, z, h, want_shore=False):
        g = self.grid
        c = g.col(x, z)
        if c is None or c[4] or c[2] is None or (x, z) in g.bridge_of:
            return False
        if abs(c[2] - h) > 2 or "lava" in (c[1] or ""):
            return False
        if c[0] - c[2] >= 2 and c[1] and terrain_queries._is_engineered(c[1]):
            return False  # standing on a roof column
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = g.col(x + dx, z + dz)
            if n is None:
                return False
            if not n[4] and n[2] is not None and abs(n[2] - c[2]) > 3:
                return False  # cliff edge
            if n[4] and "lava" in (n[1] or ""):
                return False
        return True

    def pick(self, cell, kind):
        """A safe column in ``cell`` near its centre (seeded jitter); None if the cell has none."""
        info = self.grid.cell(*cell)
        if info is None:
            return None
        x0, z0 = cell[0] * CELL, cell[1] * CELL
        cx, cz = x0 + (CELL - 1) / 2, z0 + (CELL - 1) / 2
        cands = []
        for z in range(z0 - 1, z0 + CELL + 1):
            for x in range(x0 - 1, x0 + CELL + 1):
                d = math.hypot(x - cx, z - cz)
                if d <= JITTER_RADIUS + 0.5 and self.safe(x, z, info["h"]):
                    near_water = False
                    if kind == "shore":
                        near_water = any((self.grid.col(x + dx, z + dz) or (0, 0, 0, 0, False))[4]
                                         for dx, dz in _NEIGHBOURS)
                    cands.append((x, z, d, near_water))
        if not cands:
            return None
        if kind == "shore":
            wet = [c for c in cands if c[3]]
            cands = wet or cands
        within = [c for c in cands if c[2] <= JITTER_RADIUS]
        pool = within or sorted(cands, key=lambda c: c[2])[:1]
        x, z = self.rng.choice(pool)[:2]
        return x, z, self.grid.col(x, z)[2]


NOTES = {
    "shore": "water's edge",
    "path": "joins a path",
    "bridge_start": "bridge starts",
    "bridge_end": "bridge ends",
    "bridge_deck": "on the bridge",
    "frontier": "edge of explored ground",
}


def _note_for(kind, y):
    return f"high point (y={y})" if kind == "crest" else NOTES.get(kind)


def route(view, x, z, tx, tz, rng=None, now=time.monotonic, max_nodes=MAX_NODES,
          time_limit=TIME_LIMIT_S, max_distance=MAX_DISTANCE, max_chunks=MAX_CHUNKS):
    """Plan a route from block (x, z) to (tx, tz) over chunks visible through ``view``."""
    t0 = now()
    x, z, tx, tz = int(x), int(z), int(tx), int(tz)
    rng = rng or random.Random(zlib.crc32(f"{getattr(view, 'agent', '')}:{x}:{z}:{tx}:{tz}".encode()))
    grid = Grid(view, max_chunks)
    out = {"status": "blocked", "reason": None, "start": {"x": x, "z": z}, "waypoints": [], "bridges": [],
           "detours": [], "nodes": 0}
    dist = math.hypot(tx - x, tz - z)
    clamped = False
    gx, gz = tx, tz
    if dist > max_distance:
        k = max_distance / dist
        gx, gz = x + round((tx - x) * k), z + round((tz - z) * k)
        clamped = True
    out["target"] = {"x": tx, "z": tz, "y": None, "known": False}

    def finish():
        out["unexplored_chunks"] = len(getattr(view, "unexplored", ()) or ())
        out["ms"] = round((now() - t0) * 1000)
        return out

    start_cell, goal_cell = (x // CELL, z // CELL), (gx // CELL, gz // CELL)
    sc = grid.cell(*start_cell)
    tcol = grid.col(tx, tz)
    if tcol is not None:
        out["target"].update(known=True, y=_ground_y(grid, tx, tz))
    if sc is None:
        out["reason"] = "start_unexplored"
        out["note"] = "the chunk you stand in has not been reported as seen yet"
        return finish()
    out["start"]["y"] = sc["h"]
    if start_cell == goal_cell and not clamped:
        out.update(status="complete", remaining=0, distance=round(dist))
        if out["target"]["known"]:
            out["waypoints"] = [{"x": tx, "y": out["target"]["y"], "z": tz, "kind": "target"}]
        return finish()

    pad = SEARCH_PAD // CELL
    bounds = (min(start_cell[0], goal_cell[0]) - pad, min(start_cell[1], goal_cell[1]) - pad,
              max(start_cell[0], goal_cell[0]) + pad, max(start_cell[1], goal_cell[1]) + pad)
    path, best, info = _search(grid, start_cell, goal_cell, bounds, now, t0 + time_limit, max_nodes)
    out["nodes"] = info["nodes"]
    complete = path is not None
    if not complete:
        path = _rebuild(info["parent"], best) if best != start_cell else [start_cell]
        if len(path) < 2:
            out["reason"] = "unexplored" if (info["frontier"] or not grid.known_chunk(goal_cell[0] >> 2, goal_cell[1] >> 2)) \
                else ("limit" if info["reason"] else "blocked")
            out["remaining"] = round(dist)
            return finish()
    planner = _Planner(grid, rng)
    if clamped:
        complete = False
    wps = _waypoints(planner, path, complete, (tx, tz), out["target"])
    used = {w["bridge"] for w in wps if w.get("bridge")}
    out["bridges"] = [_bridge_out(grid.bridges[b]) for b in sorted(used)]
    out["waypoints"] = _thin(wps)
    out["detours"] = _detours(grid, start_cell, goal_cell, path) if complete or path else []
    last = out["waypoints"][-1] if out["waypoints"] else {"x": x, "z": z}
    out["distance"] = round(_path_length((x, z), out["waypoints"]))
    out["remaining"] = 0 if complete else round(math.hypot(tx - last["x"], tz - last["z"]))
    if complete:
        out["status"] = "complete"
    else:
        out["status"] = "partial"
        if clamped and info["reason"] is None:
            out["reason"] = "distance"
        elif info["reason"]:
            out["reason"] = "limit"
        elif info["frontier"] or not out["target"]["known"]:
            out["reason"] = "unexplored"
        else:
            out["reason"] = "blocked"
    return finish()


def _ground_y(grid, x, z):
    bid = grid.bridge_of.get((x, z))
    c = grid.col(x, z)
    if c is None:
        return None
    if bid:
        return c[0]
    if c[4]:
        return c[0]  # water surface
    return c[2] if c[2] is not None else c[0]


def _bridge_out(b):
    a, z = b["centreline"][0], b["centreline"][-1]
    return {"id": b["id"], "a": {"x": a[0], "y": a[2], "z": a[1]}, "b": {"x": z[0], "y": z[2], "z": z[1]},
            "length": b["length"], "width": b["width"], "deck_y": b["deck_y"]}


def _path_length(origin, wps):
    total, prev = 0.0, origin
    for w in wps:
        total += math.hypot(w["x"] - prev[0], w["z"] - prev[1])
        prev = (w["x"], w["z"])
    return total


def _thin(wps):
    if len(wps) <= MAX_WAYPOINTS:
        return wps
    drop = len(wps) - MAX_WAYPOINTS
    keep = []
    for w in wps:
        if drop > 0 and w["kind"] in ("leg", "bend") and not w.get("bridge"):
            drop -= 1
            continue
        keep.append(w)
    return keep[:MAX_WAYPOINTS - 1] + keep[-1:] if len(keep) > MAX_WAYPOINTS else keep


def _class(info):
    if info["kind"] == "bridge":
        return ("bridge", info["bid"])
    return ("water",) if info["kind"] == "water" else ("land",)


def _waypoints(planner, path, complete, target, target_out):
    grid = planner.grid
    infos = [grid.cell(*c) for c in path]
    runs = []  # [class, first index, last index]
    for i, inf in enumerate(infos):
        cl = _class(inf)
        if runs and runs[-1][0] == cl:
            runs[-1][2] = i
        else:
            runs.append([cl, i, i])
    wps = []

    def add(kind, cell, note=None, **extra):
        col = planner.pick(cell, kind)
        if col is None:
            return None
        w = {"x": col[0], "y": col[2], "z": col[1], "kind": kind}
        note = note or _note_for(kind, col[2])
        if note:
            w["note"] = note
        w.update(extra)
        if wps and (wps[-1]["x"], wps[-1]["z"]) == (w["x"], w["z"]):
            return None
        wps.append(w)
        return w

    for ri, (cl, a, b) in enumerate(runs):
        cells = path[a:b + 1]
        if cl[0] == "bridge":
            _bridge_waypoints(grid.bridges[cl[1]], cells, wps)
            continue
        if cl[0] == "water":
            continue  # nothing stands in water: the shore waypoints on either side bracket the swim
        ok = lambda c: (grid.cell(*c) or {"kind": "water"})["kind"] in ("land", "path")
        crest = _crests(cells, grid)
        marks = {0, len(cells) - 1} | set(_simplify(cells, ok)) | crest
        for k in sorted(marks):
            inf = grid.cell(*cells[k])
            first, last = k == 0, k == len(cells) - 1
            if first:
                if ri > 0 and runs[ri - 1][0][0] == "water":
                    add("shore", cells[0], note="back on dry land")
                if not last:
                    continue
                if ri == 0 or runs[ri - 1][0][0] != "water":
                    continue
            kind = "crest" if k in crest else "bend"
            if inf["kind"] == "path" and (k == 0 or grid.cell(*cells[k - 1])["kind"] != "path"):
                kind = "path"
            extra = {}
            if last:
                if ri < len(runs) - 1:
                    if runs[ri + 1][0][0] == "water":
                        kind = "shore"
                        swim = (runs[ri + 1][2] - runs[ri + 1][1] + 1) * CELL
                        extra = {"note": f"water ahead, about {swim} blocks to cross", "swim": swim}
                else:
                    kind = "target" if complete else "frontier"
            w = add(kind, cells[k], **extra)
            if w is None and last and ri == len(runs) - 1 and not complete:
                for back in range(k - 1, max(-1, k - 4), -1):  # nearest standable cell before the edge
                    if add("frontier", cells[back]):
                        break
    if complete and target_out["y"] is not None:
        if wps and wps[-1]["kind"] == "target":
            wps.pop()
        wps.append({"x": target[0], "y": target_out["y"], "z": target[1], "kind": "target"})
    return _spacing(wps, path, planner)


def _crests(cells, grid):
    out = set()
    hs = [grid.cell(*c)["h"] for c in cells]
    for k in range(1, len(cells) - 1):
        lo = max(0, k - 5)
        hi = min(len(cells), k + 6)
        if hs[k] == max(hs[lo:hi]) and hs[k] - min(hs[lo:k + 1]) >= CREST_RISE and hs[k] - min(hs[k:hi]) >= CREST_RISE:
            out.add(k)
    return out


def _bridge_waypoints(br, cells, wps):
    line = br["centreline"]
    def nearest(cell):
        cx, cz = cell[0] * CELL + CELL / 2, cell[1] * CELL + CELL / 2
        return min(range(len(line)), key=lambda i: (line[i][0] - cx) ** 2 + (line[i][1] - cz) ** 2)
    i0, i1 = nearest(cells[0]), nearest(cells[-1])
    forward = i0 <= i1
    n = len(line)
    snap0 = i0 <= BRIDGE_SNAP if forward else i0 >= n - 1 - BRIDGE_SNAP
    snap1 = i1 >= n - 1 - BRIDGE_SNAP if forward else i1 <= BRIDGE_SNAP
    start_i = (0 if forward else n - 1) if snap0 else i0
    end_i = (n - 1 if forward else 0) if snap1 else i1
    step = 1 if end_i >= start_i else -1
    idxs = list(range(start_i, end_i + step, step))
    for pos, i in enumerate(idxs):
        x, z, y = line[i]
        kind = None
        if pos == 0:
            kind = "bridge_start" if snap0 else "bridge_deck"
        elif pos == len(idxs) - 1:
            kind = "bridge_end" if snap1 else "bridge_deck"
        elif pos % BRIDGE_DECK_STEP == 0:
            kind = "bridge_deck"
        if kind:
            w = {"x": x, "y": y, "z": z, "kind": kind, "bridge": br["id"]}
            note = _note_for(kind, y)
            if note:
                w["note"] = note
            wps.append(w)


def _spacing(wps, path, planner):
    """Insert ``leg`` waypoints so no gap exceeds ``MAX_LEG`` (never inside a bridge)."""
    if not wps:
        return wps
    origin = (path[0][0] * CELL + CELL // 2, path[0][1] * CELL + CELL // 2)
    max_leg = max(MAX_LEG, math.ceil(_path_length(origin, wps) / 40))
    out = []
    prev = {"x": origin[0], "z": origin[1]}
    for w in wps:
        gap = math.hypot(w["x"] - prev["x"], w["z"] - prev["z"])
        inside_bridge = w.get("bridge") and out and out[-1].get("bridge") == w["bridge"]
        if gap > max_leg and not inside_bridge:
            n = int(gap // max_leg)
            for k in range(1, n + 1):
                f = k / (n + 1)
                px, pz = prev["x"] + (w["x"] - prev["x"]) * f, prev["z"] + (w["z"] - prev["z"]) * f
                cell = (int(px) // CELL, int(pz) // CELL)
                inf = planner.grid.cell(*cell)
                if inf is None or inf["kind"] not in ("land", "path"):
                    continue
                col = planner.pick(cell, "leg")
                if col is not None:
                    out.append({"x": col[0], "y": col[2], "z": col[1], "kind": "leg"})
        out.append(w)
        prev = w
    return out


def _detours(grid, start_cell, goal_cell, path):
    """What the straight line would have crossed that the route goes around."""
    path_set = set(path)
    line = _line_cells(start_cell, path[-1] if path else goal_cell)
    water = cliff = 0
    for c in line:
        inf = grid.cell(*c)
        if inf is None or c in path_set:
            continue
        if inf["kind"] == "water":
            water += 1
        elif inf["kind"] in ("cliff", "lava"):
            cliff += 1
    out = []
    if water >= 2 and not any((grid.cell(*c) or {}).get("kind") == "water" for c in path):
        out.append({"around": "water", "cells": water})
    if cliff >= 2:
        out.append({"around": "cliff", "cells": cliff})
    return out
