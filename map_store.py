"""Tiered terrain map store.

Hot   - chunks in the most recent perception window (in memory, window-driven)
Warm  - recently queried/loaded chunks outside the window, LRU (in memory)
Cold  - every chunk ever loaded or updated, persisted to SQLite

The hot and warm tiers are pure caches; correctness falls back to SQLite,
then to the base-state loader reading the world save.

Tier rules:
  * ``touch_window`` makes exactly the requested window hot and demotes any
    hot chunk that left the window into warm.
  * Detailed queries (``get_chunk``/``get_or_load``) promote to warm only,
    unless the chunk is already hot.
  * Diffs update a resident chunk in place (hot stays hot, warm stays warm);
    a non-resident chunk is loaded and stored in warm. Diffs never promote.
  * ``put_chunk`` (preload) stores in warm, never hot.

Trust model (the save is the base, diffs/snapshots are overlays):
  * Every record that came from the save carries ``save_ts``: the chunk's
    region-header timestamp at load time. ``updated_at`` is only "last time this
    row was persisted" and ``loaded_at`` is "when the base was read".
  * On every read the loader's current header timestamp is compared with the
    record's ``save_ts``. If the save is newer the record is rebuilt from the
    save (which already contains older changes) and only the diffs/snapshots
    *received after that save time* are replayed from a small bounded
    in-memory log (:class:`_DiffLog`).
  * The cold SQLite tier is stamped with a schema version and a ``world_id``
    (resolved region dir + seed); a mismatch discards the cold rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

import mc_blocks
import terrain_rules

log = logging.getLogger("topography.store")

MAX_PERCEPTION_CHUNKS = 1024

# v2: chunks.save_ts / loaded_at, meta table, surface.terrain_y/terrain_materials.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    surface_json TEXT NOT NULL,
    composition_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    save_ts INTEGER,
    loaded_at REAL,
    PRIMARY KEY (cx, cz)
);
"""
META_SCHEMA = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"

SURFACE_ARRAYS = (
    "y", "materials", "ground_y", "ground_materials", "water",
    "terrain_y", "terrain_materials",
)
_WATER_TOPS = {
    "minecraft:water", "minecraft:bubble_column", "minecraft:kelp",
    "minecraft:kelp_plant", "minecraft:seagrass", "minecraft:tall_seagrass",
}
OVERWORLD = "minecraft:overworld"


def normalize_dimension(value) -> str:
    """Normalise ``overworld`` / ``minecraft:overworld`` / ``the_nether`` ... ."""
    if value is None:
        return OVERWORLD
    raw = str(value).strip().lower()
    if not raw:
        return OVERWORLD
    if ":" not in raw:
        raw = "minecraft:" + raw
    if raw in ("minecraft:the_overworld", "minecraft:world"):
        raw = OVERWORLD
    if raw == "minecraft:nether":
        raw = "minecraft:the_nether"
    if raw == "minecraft:end":
        raw = "minecraft:the_end"
    return raw


def is_overworld(value) -> bool:
    return normalize_dimension(value) == OVERWORLD


class _DiffLog:
    """Bounded per-chunk log of overlays with their receive times."""

    def __init__(self, per_chunk: int = 1024, max_chunks: int = 4096):
        self.per_chunk = per_chunk
        self.max_chunks = max_chunks
        self._logs: OrderedDict[tuple[int, int], deque] = OrderedDict()

    def add(self, key, received_at: float, kind: str, payload: dict):
        entries = self._logs.get(key)
        if entries is None:
            entries = self._logs[key] = deque(maxlen=self.per_chunk)
        else:
            self._logs.move_to_end(key)
        entries.append((received_at, kind, payload))
        while len(self._logs) > self.max_chunks:
            self._logs.popitem(last=False)

    def since(self, key, ts: float):
        return [e for e in self._logs.get(key, ()) if e[0] > ts]


class MapStore:
    def __init__(self, db_path: str, loader, hot_limit: int = 2048, warm_limit: int = 2048):
        self.loader = loader
        self.hot_limit = hot_limit
        self.warm_limit = warm_limit
        self._lock = threading.RLock()
        self._hot: OrderedDict[tuple[int, int], dict] = OrderedDict()
        self._warm: OrderedDict[tuple[int, int], dict] = OrderedDict()
        self._dirty: dict[tuple[int, int], dict] = {}
        self._difflog = _DiffLog()
        self._clock = time.time
        self._generation = 0
        self._stop = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self.discarded_cold = 0
        self.reloaded_stale = 0

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.world_id = self._loader_world_id()
        self._open_schema()

    def _loader_world_id(self):
        fn = getattr(self.loader, "world_id", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not derive world id")
            return None

    def _open_schema(self):
        conn = self._conn
        conn.execute(META_SCHEMA)
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        has_chunks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
        ).fetchone()
        reason = None
        if has_chunks:
            try:
                version = int(meta.get("schema_version") or 0)
            except ValueError:
                version = 0
            stored_world = meta.get("world_id")
            if version < SCHEMA_VERSION:
                reason = f"schema version {version} < {SCHEMA_VERSION}"
            elif self.world_id and stored_world and stored_world != self.world_id:
                reason = f"world changed ({stored_world} -> {self.world_id})"
        if reason:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            self.discarded_cold = row[0] if row else 0
            conn.execute("DROP TABLE chunks")
            log.warning("Discarding %s cold chunk rows: %s", self.discarded_cold, reason)
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        if self.world_id:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('world_id', ?)",
                (self.world_id,),
            )
        conn.commit()

    @property
    def generation(self) -> int:
        """Monotonic counter incremented whenever a diff/snapshot is applied."""
        with self._lock:
            return self._generation

    # ------------------------------------------------------------------ tiers
    def _remember_hot(self, key: tuple[int, int], data: dict):
        """Mark ``key`` hot. Hot and warm are kept disjoint."""
        self._warm.pop(key, None)
        self._hot[key] = data
        self._hot.move_to_end(key)
        while len(self._hot) > self.hot_limit:
            old_key, old_data = self._hot.popitem(last=False)
            self._remember_warm(old_key, old_data)

    def _remember_warm(self, key: tuple[int, int], data: dict):
        self._warm[key] = data
        self._warm.move_to_end(key)
        while len(self._warm) > self.warm_limit:
            self._warm.popitem(last=False)

    def _read_cold(self, cx: int, cz: int):
        row = self._conn.execute(
            "SELECT surface_json, composition_json, updated_at, save_ts, loaded_at "
            "FROM chunks WHERE cx=? AND cz=?",
            (cx, cz),
        ).fetchone()
        if row is None:
            return None
        return {
            "cx": cx,
            "cz": cz,
            "surface": json.loads(row[0]),
            "composition": json.loads(row[1]),
            "updated_at": row[2],
            "save_ts": row[3],
            "loaded_at": row[4],
            "source": "cold",
        }

    # ------------------------------------------------------------ revalidation
    def _revalidate(self, key, data):
        """Rebuild ``data`` from the save when the save is newer than the record.

        Returns the (possibly new) record. The replacement keeps its tier and
        is marked dirty; overlays received after the save time are replayed.
        """
        ts_fn = getattr(self.loader, "chunk_timestamp", None)
        if not callable(ts_fn) or data is None:
            return data
        try:
            current = ts_fn(key[0], key[1])
        except Exception:  # pragma: no cover - defensive
            log.exception("chunk_timestamp failed for %s", key)
            return data
        if current is None:
            return data
        recorded = data.get("save_ts")
        if recorded is not None and current <= recorded:
            return data
        # recorded is None: a diff/snapshot-only record that the save now covers.
        fresh = self.loader.load_chunk(key[0], key[1])
        if fresh is None:
            return data
        fresh["source"] = "base"
        fresh["loaded_at"] = self._clock()
        with self._lock:
            since = fresh.get("save_ts") or current
            for _t, kind, payload in self._difflog.since(key, since):
                if kind == "diff":
                    self._apply_delta_to(fresh, payload)
                else:
                    self._apply_snapshot_to(fresh, payload)
            self.reloaded_stale += 1
            self._install(key, fresh)
            self._dirty[key] = fresh
        log.info("Chunk %s reloaded: save is newer (%s > %s)", key, current, recorded)
        return fresh

    def _install(self, key, data):
        """Replace the resident record keeping its tier (new records go warm)."""
        if key in self._hot:
            self._hot[key] = data
        elif key in self._warm:
            self._warm[key] = data
            self._warm.move_to_end(key)
        else:
            self._remember_warm(key, data)

    def _get_data(self, cx: int, cz: int, include_save: bool = True):
        """Read hot -> warm -> cold (validated against the save) -> save."""
        key = (int(cx), int(cz))
        with self._lock:
            data = self._hot.get(key)
            if data is None:
                data = self._warm.get(key)
            if data is None:
                data = self._read_cold(key[0], key[1])
        if data is not None:
            return self._revalidate(key, data)
        if include_save and self.loader is not None:
            return self.loader.load_chunk(key[0], key[1])
        return None

    def get_chunk(self, cx: int, cz: int):
        """Detailed read path: promote to warm, never to hot."""
        key = (int(cx), int(cz))
        with self._lock:
            data = self._hot.get(key)
            if data is None:
                data = self._warm.get(key)
                if data is not None:
                    self._warm.move_to_end(key)
            if data is None:
                data = self._read_cold(key[0], key[1])
                if data is not None:
                    self._remember_warm(key, data)
        if data is None:
            return None
        return self._revalidate(key, data)

    def get_or_load(self, cx: int, cz: int):
        """Detailed read path: promote to warm, loading the save on a miss."""
        key = (int(cx), int(cz))
        data = self.get_chunk(key[0], key[1])
        if data is not None:
            return data
        if self.loader is None:
            return None
        data = self.loader.load_chunk(key[0], key[1])
        if data is None:
            return None
        with self._lock:
            data["source"] = "base"
            data["loaded_at"] = self._clock()
            self._remember_warm(key, data)
            self._dirty[key] = data
        return data

    def touch_window(self, center_cx: int, center_cz: int, size_chunks: int):
        """Load a square window and make exactly those chunks hot.

        Returns ``(records, missing)`` where ``records`` is row-major
        (north-to-south, then west-to-east). Chunks outside the new window
        that were hot are demoted into warm.
        """
        size = int(size_chunks)
        if size < 1:
            raise ValueError("size_chunks must be positive")
        if size * size > MAX_PERCEPTION_CHUNKS:
            raise ValueError(
                f"Window too large ({size}x{size} chunks); "
                f"max {MAX_PERCEPTION_CHUNKS} chunks"
            )
        ccx, ccz = int(center_cx), int(center_cz)
        half = size // 2
        cx1 = ccx - half
        cx2 = cx1 + size - 1
        cz1 = ccz - half
        cz2 = cz1 + size - 1

        keys = [
            (cx, cz)
            for cz in range(cz1, cz2 + 1)
            for cx in range(cx1, cx2 + 1)
        ]
        loaded: dict[tuple[int, int], dict] = {}
        missing = 0
        for key in keys:
            data = self._get_data(key[0], key[1])
            if data is None:
                missing += 1
            else:
                loaded[key] = data

        window_keys = set(keys)
        with self._lock:
            for key in list(self._hot.keys()):
                if key not in window_keys:
                    data = self._hot.pop(key)
                    self._remember_warm(key, data)
            for key in keys:
                data = loaded.get(key)
                if data is None:
                    continue
                self._remember_hot(key, data)

        records = [loaded[key] for key in keys if key in loaded]
        return records, missing

    def get_window(self, center_cx: int, center_cz: int, radius_chunks: int):
        """Load and return chunks within a square chunk window."""
        records = []
        missing = 0
        radius = max(0, int(radius_chunks))
        for cx in range(center_cx - radius, center_cx + radius + 1):
            for cz in range(center_cz - radius, center_cz + radius + 1):
                data = self.get_or_load(cx, cz)
                if data is None:
                    missing += 1
                else:
                    records.append(data)
        return records, missing

    def get_chunk_at(self, x: int, z: int):
        return self.get_chunk(int(x) // 16, int(z) // 16)

    # --------------------------------------------------------------- mutation
    def put_chunk(self, data: dict):
        """Preload path: store in warm + dirty. Never introduces a hot chunk."""
        key = (int(data["cx"]), int(data["cz"]))
        with self._lock:
            if key in self._hot:
                self._hot[key] = data
            else:
                self._remember_warm(key, data)
            self._dirty[key] = data

    @staticmethod
    def _blank_chunk(cx: int, cz: int) -> dict:
        return {
            "cx": cx,
            "cz": cz,
            "surface": {
                "y": [None] * 256,
                "materials": [None] * 256,
                "ground_y": [None] * 256,
                "ground_materials": [None] * 256,
                "water": [False] * 256,
                "terrain_y": [None] * 256,
                "terrain_materials": [None] * 256,
            },
            "composition": {
                "counts": {"natural": 0, "engineered": 0, "unknown": 0, "liquid": 0, "air": 0},
                "top_blocks": [],
                "strong_indicators": [],
                "strong_counts": {},
                "strong_indicator_count": 0,
                "dominant_biome": None,
                "artificiality_score": 0.0,
                "surface_min_y": None,
            },
            "save_ts": None,
            "source": "diff",
        }

    def _acquire(self, key):
        """Return the record to mutate: resident, cold, base or blank."""
        with self._lock:
            data = self._hot.get(key) or self._warm.get(key)
            if data is None:
                data = self._read_cold(key[0], key[1])
        if data is not None:
            data = self._revalidate(key, data)
        elif self.loader is not None:
            data = self.loader.load_chunk(key[0], key[1])
            if data is not None:
                data["source"] = "base"
                data["loaded_at"] = self._clock()
        if data is None:
            data = self._blank_chunk(key[0], key[1])
        return data

    def _commit(self, key, data, log_entry=None):
        """Install a mutated record, mark dirty and bump the generation."""
        if log_entry is not None:
            self._difflog.add(key, self._clock(), *log_entry)
        self._install(key, data)
        self._dirty[key] = data
        self._generation += 1

    def apply_diff(self, delta: dict):
        """Apply one block change. Returns False if the delta is unusable."""
        try:
            x = int(delta["x"])
            y = int(delta["y"])
            z = int(delta["z"])
            action = str(delta.get("action", "")).lower()
            block_name = delta.get("block_name") or delta.get("new_top_block")
        except (KeyError, TypeError, ValueError):
            return False
        if action not in ("place", "break", "chunk_load", "chunk_unload"):
            return False
        if not is_overworld(delta.get("dimension")):
            return True  # accepted but ignored: the service is overworld-only
        if action in ("chunk_load", "chunk_unload"):
            return True
        if action == "place" and block_name is None:
            return False
        # Block id unchanged (state-only change): nothing about the map moves.
        if action == "place" and delta.get("old_block") == block_name:
            return True
        if action == "break" and delta.get("block_name") in mc_blocks.AIR:
            return True

        key = (x // 16, z // 16)
        data = self._acquire(key)
        with self._lock:
            # A concurrent writer may have made the chunk resident meanwhile.
            data = self._hot.get(key) or self._warm.get(key) or data
            clean = {k: v for k, v in delta.items() if k != "dimension"}
            self._apply_delta_to(data, clean)
            data["source"] = "diff"
            self._commit(key, data, ("diff", clean))
        return True

    # ------------------------------------------------------- delta application
    @staticmethod
    def _ensure_terrain(surface: dict):
        for name in ("terrain_y", "terrain_materials"):
            if name not in surface or len(surface[name]) != 256:
                surface[name] = [None] * 256

    def _apply_delta_to(self, data: dict, delta: dict):
        """Mutate ``data`` with one place/break delta (no tier/log handling)."""
        x, y, z = int(delta["x"]), int(delta["y"]), int(delta["z"])
        action = str(delta.get("action", "")).lower()
        block_name = delta.get("block_name") or delta.get("new_top_block")
        index = (x % 16) + (z % 16) * 16
        surface = data["surface"]
        self._ensure_terrain(surface)
        if action == "place":
            self._apply_place(data, surface, index, y, block_name, delta.get("old_block"))
        elif action == "break":
            self._apply_break(data, surface, index, y, delta, block_name)

    def _apply_place(self, data, surface, index, y, block_name, old_block):
        top = surface["y"][index]
        if block_name not in mc_blocks.AIR and (top is None or y >= top):
            surface["y"][index] = y
            surface["materials"][index] = block_name
            surface["water"][index] = block_name in _WATER_TOPS
        ground = surface["ground_y"][index]
        if block_name not in mc_blocks.AIR and block_name not in _NON_SOLID:
            if ground is None or y >= ground:
                surface["ground_y"][index] = y
                surface["ground_materials"][index] = block_name
        terrain = surface["terrain_y"][index]
        if terrain_rules.is_ground(block_name) and (terrain is None or y >= terrain):
            surface["terrain_y"][index] = y
            surface["terrain_materials"][index] = block_name
        self._adjust_composition(data, block_name, +1)
        if old_block:
            self._adjust_composition(data, old_block, -1)

    def _apply_break(self, data, surface, index, y, delta, broken_block):
        top = surface["y"][index]
        old_material = surface["materials"][index]
        trusted = delta.get("surface_y") is not None
        nt_y, nt_b = delta.get("new_top_y"), delta.get("new_top_block")
        ng_y, ng_b = delta.get("new_ground_y"), delta.get("new_ground_block")
        has_terrain_hint = "new_terrain_y" in delta

        # Surface/ground: only information about the TRUE column surface may
        # change them. A plugin-supplied ``surface_y`` is exactly that; legacy
        # deltas are honoured only when the break was at/above the recorded top.
        if trusted:
            sy = int(delta["surface_y"])
            surface["y"][index] = sy
            surface["materials"][index] = nt_b
            surface["water"][index] = nt_b in _WATER_TOPS
            self._set_ground_after_break(surface, index, sy, nt_b, ng_y, ng_b)
        elif top is None or y >= top:
            if nt_y is not None and nt_b is not None:
                nt_y = int(nt_y)
                surface["y"][index] = nt_y
                surface["materials"][index] = nt_b
                surface["water"][index] = nt_b in _WATER_TOPS
                self._set_ground_after_break(surface, index, nt_y, nt_b, ng_y, ng_b)
            elif top is not None and y == top:
                surface["y"][index] = y - 1
                surface["materials"][index] = None
                surface["water"][index] = False
        if (
            not trusted
            and not (top is None or y >= top)
            and surface["ground_y"][index] == y
        ):
            # Underground break of what we recorded as ground: it is gone,
            # but we do not know what is below.
            surface["ground_y"][index] = y - 1
            surface["ground_materials"][index] = None
        elif (
            not trusted
            and (top is None or y >= top)
            and surface["ground_y"][index] == y
            and ng_y is None
        ):
            surface["ground_y"][index] = y - 1
            surface["ground_materials"][index] = None

        # Terrain (topmost is_ground block).
        terrain = surface["terrain_y"][index]
        if has_terrain_hint:
            t_y = delta.get("new_terrain_y")
            if trusted or terrain is None or y >= terrain:
                if t_y is None:
                    surface["terrain_y"][index] = None
                    surface["terrain_materials"][index] = None
                else:
                    surface["terrain_y"][index] = int(t_y)
                    surface["terrain_materials"][index] = delta.get("new_terrain_block")
        elif terrain is not None and y == terrain:
            fallback = None
            if (
                ng_y is not None
                and int(ng_y) < y
                and terrain_rules.is_ground(ng_b)
            ):
                fallback = (int(ng_y), ng_b)
            if fallback:
                surface["terrain_y"][index], surface["terrain_materials"][index] = fallback
            else:
                surface["terrain_y"][index] = None
                surface["terrain_materials"][index] = None

        broken = broken_block if delta.get("block_name") else old_material
        if broken:
            self._adjust_composition(data, broken, -1)
        new_block = delta.get("new_block")
        self._adjust_composition(data, new_block or "minecraft:air", +1)

    @staticmethod
    def _set_ground_after_break(surface, index, top_y, top_block, ng_y, ng_b):
        if ng_y is not None and ng_b is not None:
            surface["ground_y"][index] = int(ng_y)
            surface["ground_materials"][index] = ng_b
        elif top_block is not None and top_block not in _NON_SOLID:
            surface["ground_y"][index] = top_y
            surface["ground_materials"][index] = top_block

    def _adjust_composition(self, data: dict, block_name: str, amount: int):
        if not block_name:
            return
        comp = data.setdefault("composition", {})
        counts = comp.setdefault("counts", {})
        cls = mc_blocks.classify_block(block_name)
        counts[cls] = max(0, counts.get(cls, 0) + amount)
        if not mc_blocks.is_strong_indicator(block_name):
            return
        indicators = comp.setdefault("strong_indicators", [])
        strong = comp.get("strong_counts")
        if strong is None:
            # Records without per-block counts: assume one of each known type.
            strong = comp["strong_counts"] = {name: 1 for name in indicators}
        strong[block_name] = max(0, strong.get(block_name, 0) + amount)
        if strong[block_name] > 0:
            if block_name not in indicators:
                indicators.append(block_name)
        else:
            strong.pop(block_name, None)
            if block_name in indicators:
                indicators.remove(block_name)
        comp["strong_indicator_count"] = sum(strong.values())

    # --------------------------------------------------------------- snapshots
    def apply_snapshots(self, snapshots):
        """Apply plugin snapshots; returns ``(accepted, rejected, ignored)``."""
        accepted = rejected = ignored = 0
        for snap in snapshots:
            result = self.apply_snapshot(snap)
            if result is True:
                accepted += 1
            elif result == "ignored":
                ignored += 1
            else:
                rejected += 1
        return accepted, rejected, ignored

    @staticmethod
    def _validate_snapshot(snap):
        if not isinstance(snap, dict):
            return None
        try:
            cx, cz = int(snap["cx"]), int(snap["cz"])
        except (KeyError, TypeError, ValueError):
            return None
        surface = snap.get("surface")
        if not isinstance(surface, dict):
            return None
        clean = {}
        for name in ("y", "materials", "ground_y", "ground_materials", "water"):
            arr = surface.get(name)
            if not isinstance(arr, list) or len(arr) != 256:
                return None
            clean[name] = list(arr)
        clean["water"] = [bool(v) for v in clean["water"]]
        for name in ("terrain_y", "terrain_materials"):
            arr = surface.get(name)
            if arr is None:
                clean[name] = [None] * 256
            elif isinstance(arr, list) and len(arr) == 256:
                clean[name] = list(arr)
            else:
                return None
        counts = snap.get("counts")
        if counts is not None and not isinstance(counts, dict):
            return None
        return {"cx": cx, "cz": cz, "surface": clean, "counts": counts}

    def apply_snapshot(self, snap):
        """Replace one chunk's surface. True / "ignored" / False."""
        if isinstance(snap, dict) and not is_overworld(snap.get("dimension")):
            return "ignored"
        clean = self._validate_snapshot(snap)
        if clean is None:
            return False
        key = (clean["cx"], clean["cz"])
        data = self._acquire(key)
        with self._lock:
            data = self._hot.get(key) or self._warm.get(key) or data
            self._apply_snapshot_to(data, clean)
            data["source"] = "snapshot"
            self._commit(key, data, ("snap", clean))
        return True

    @staticmethod
    def _apply_snapshot_to(data: dict, clean: dict):
        data["surface"] = {name: list(arr) for name, arr in clean["surface"].items()}
        counts = clean.get("counts")
        if counts:
            comp = data.setdefault("composition", {})
            merged = comp.setdefault("counts", {})
            for name, value in counts.items():
                try:
                    merged[str(name)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue

    # --------------------------------------------------------------- cold tier
    def flush(self, force: bool = False):
        with self._lock:
            if not self._dirty:
                return 0
            rows = []
            now = time.time()
            for (cx, cz), data in self._dirty.items():
                data["updated_at"] = now
                rows.append(
                    (
                        cx,
                        cz,
                        json.dumps(data.get("surface") or {}),
                        json.dumps(data.get("composition") or {}),
                        now,
                        data.get("save_ts"),
                        data.get("loaded_at"),
                    )
                )
            self._dirty.clear()
            self._conn.executemany(
                "INSERT INTO chunks (cx, cz, surface_json, composition_json, updated_at, "
                "save_ts, loaded_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cx, cz) DO UPDATE SET "
                "surface_json=excluded.surface_json, "
                "composition_json=excluded.composition_json, "
                "updated_at=excluded.updated_at, "
                "save_ts=excluded.save_ts, "
                "loaded_at=excluded.loaded_at",
                rows,
            )
            self._conn.commit()
            return len(rows)

    def start_flush_thread(self, interval: float = 2.0):
        def loop():
            while not self._stop.wait(interval):
                with self._lock:
                    dirty = len(self._dirty)
                if dirty > 0:
                    self.flush()

        self._flush_thread = threading.Thread(target=loop, name="map-store-flush", daemon=True)
        self._flush_thread.start()

    def stop(self):
        self._stop.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=5)
        self.flush(force=True)
        with self._lock:
            self._conn.close()

    def stats(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            cold = row[0] if row else 0
            return {
                "hot_chunks": len(self._hot),
                "warm_chunks": len(self._warm),
                "cold_chunks": cold,
                "dirty_chunks": len(self._dirty),
                "generation": self._generation,
                "world_id": self.world_id,
                "discarded_cold": self.discarded_cold,
                "reloaded_stale": self.reloaded_stale,
            }


_NON_SOLID = {
    "minecraft:air", "minecraft:cave_air", "minecraft:void_air",
    "minecraft:water", "minecraft:lava", "minecraft:bubble_column",
    "minecraft:kelp", "minecraft:kelp_plant", "minecraft:seagrass",
    "minecraft:tall_seagrass", "minecraft:short_grass", "minecraft:grass",
    "minecraft:tall_grass", "minecraft:fern", "minecraft:large_fern",
    "minecraft:dead_bush", "minecraft:sugar_cane", "minecraft:bamboo",
    "minecraft:vines", "minecraft:vine", "minecraft:glow_lichen",
}
