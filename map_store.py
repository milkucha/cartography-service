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

Last-seen map (per-agent frozen views, see ``store_view``):
  * Every stored chunk carries a ``rev``. A freshly registered base record is rev 1; any change
    to its surface data (diff, snapshot, reload from a newer save) takes a new, store-wide
    unique rev from a persisted counter. Rev 0 means "the store has no data for this chunk".
  * An agent that reports with the tracked protocol (``unloaded`` / ``replace`` on ``POST /seen``)
    has a *loaded* set (in memory only). Every seen chunk that is not loaded is *pinned* at the
    chunk's rev at the time it left the agent's loaded set (persisted in ``seen.frozen_rev``).
  * Before a chunk is changed, if some agent is pinned at its current rev, the old stored form is
    saved once (zlib JSON) in ``chunk_history``; a history row is deleted as soon as no pin
    references it. With no pins on a chunk the cost is one dict lookup.
  * A chunk pinned at rev 0 (seen while the store had no data) adopts the first data that arrives
    within ``FIRST_DATA_GRACE_S`` of the unload, or, for data read from a region file, whose on-disk
    timestamp is at or before ``frozen_at + FIRST_DATA_GRACE_S`` (whenever it is read). Later data is
    not attributed to that agent.
  * Rev-0 pins are resolved eagerly: each is queued in ``_unresolved`` and ``resolve_pending`` (driven
    by the prefetch worker, a small bounded batch per tick) reads the chunk from the save into the
    cold tier (never warm/hot), retrying with backoff until the grace period ends, since the server
    only writes new chunks at autosave/unload. Pins restored at startup are queued the same way, and a
    query for a queued chunk resolves it on the spot.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
import zlib
from collections import OrderedDict, deque
from pathlib import Path

import mc_blocks
import terrain_rules

log = logging.getLogger("topography.store")

MAX_PERCEPTION_CHUNKS = 1024

# v2: chunks.save_ts / loaded_at, meta table, surface.terrain_y/terrain_materials.
# v3: adds the per-agent ``seen`` ledger table. The chunks layout is unchanged from v2.
# v4: ``terrain_y`` now means natural ground only (roofs/floors/decks are skipped), so chunk
# rows written by v2/v3 hold wrong terrain and are dropped to be recomputed. Only the
# ``chunks`` table is discarded; ``seen`` and ``meta`` survive.
SCHEMA_VERSION = 4
CHUNKS_MIN_VERSION = 4
MAX_SEEN_PER_CALL = 5000

# Last-seen map tuning (see module docstring).
FIRST_DATA_GRACE_S = 600.0          # data for a chunk the store had nothing on, after an unload
LOADED_TTL_S = 120.0                # a tracked agent silent this long is treated as having unloaded all
HISTORY_MAX_BYTES = 64 * 1024 * 1024   # compressed bytes of chunk_history kept in total
HISTORY_MAX_PER_CHUNK = 8           # distinct pinned revisions kept per chunk
RESOLVE_BATCH = 32                  # rev-0 pins resolved per background step
RESOLVE_BUDGET_S = 0.25             # wall-clock cap of one background step
RESOLVE_RETRY_MIN_S = 5.0           # retry delay after a failed attempt, doubling ...
RESOLVE_RETRY_MAX_S = 60.0          # ... up to this
MAX_UNRESOLVED = 100_000            # queued rev-0 pins; beyond it only the data's own arrival adopts
FROZEN_CACHE_LIMIT = 1280          # decoded history records kept in memory (>= one 32x32 window)

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
SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    agent TEXT NOT NULL,
    dimension TEXT NOT NULL,
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (agent, dimension, cx, cz)
)
"""
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_history (
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (cx, cz, revision)
)
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
        self._seen_cache: dict[tuple[str, str], set] = {}
        # last-seen map state (see module docstring)
        self._pins: dict[tuple[int, int], dict[str, tuple[int, float]]] = {}
        self._agents: dict[str, dict] = {}
        self._unresolved: dict[tuple[int, int], list] = {}   # key -> [next_try, attempts]
        self._rev_counter = 1
        self._hist_index: OrderedDict = OrderedDict()   # (cx, cz, rev) -> compressed size
        self._hist_by_chunk: dict[tuple[int, int], set] = {}
        self._hist_bytes = 0
        self._frozen_cache: OrderedDict = OrderedDict()
        self.history_evicted = 0
        self.history_saved = 0
        self.history_lost_reads = 0

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.world_id = self._loader_world_id()
        self._open_schema()
        self._init_last_seen()

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
            if version < CHUNKS_MIN_VERSION:
                reason = f"schema version {version} < {CHUNKS_MIN_VERSION}"
            elif self.world_id and stored_world and stored_world != self.world_id:
                reason = f"world changed ({stored_world} -> {self.world_id})"
        if reason:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            self.discarded_cold = row[0] if row else 0
            conn.execute("DROP TABLE chunks")
            conn.execute("DROP TABLE IF EXISTS chunk_history")  # its revisions belonged to the dropped rows
            log.warning("Discarding %s cold chunk rows: %s", self.discarded_cold, reason)
        conn.execute(SCHEMA)
        conn.execute(SEEN_SCHEMA)
        conn.execute(HISTORY_SCHEMA)
        # Last-seen columns are always added by ALTER (new and old databases take the same path;
        # existing rows are kept).
        self._add_column("chunks", "rev", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("seen", "frozen_rev", "INTEGER")
        self._add_column("seen", "frozen_at", "REAL")
        if reason:
            conn.execute("UPDATE seen SET frozen_rev=NULL, frozen_at=NULL")
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

    def _add_column(self, table: str, name: str, decl: str):
        cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @property
    def generation(self) -> int:
        """Monotonic counter incremented whenever a diff/snapshot is applied."""
        with self._lock:
            return self._generation

    # ------------------------------------------------------------------ tiers
    def _remember_hot(self, key: tuple[int, int], data: dict):
        """Mark ``key`` hot. Hot and warm are kept disjoint."""
        self._warm.pop(key, None)
        self._ensure_rev(key, data)
        self._hot[key] = data
        self._hot.move_to_end(key)
        while len(self._hot) > self.hot_limit:
            old_key, old_data = self._hot.popitem(last=False)
            self._remember_warm(old_key, old_data)

    def _remember_warm(self, key: tuple[int, int], data: dict):
        self._ensure_rev(key, data)
        self._warm[key] = data
        self._warm.move_to_end(key)
        while len(self._warm) > self.warm_limit:
            self._warm.popitem(last=False)

    def _read_cold(self, cx: int, cz: int):
        row = self._conn.execute(
            "SELECT surface_json, composition_json, updated_at, save_ts, loaded_at, rev "
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
            "rev": row[5] or 1,
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
            self._preserve(key, data)   # an agent frozen on the old form keeps it
            fresh["rev"] = self._next_rev()
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
            existing = self._hot.get(key) or self._warm.get(key)
            if existing is not None and existing is not data:
                self._preserve(key, existing)
                data["rev"] = self._next_rev()
            if key in self._hot:
                self._ensure_rev(key, data)
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
        self._set_changed(key, data)
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
            self._preserve(key, data)
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
            if nt_b is None or nt_b in mc_blocks.AIR:
                # Empty column (plugin reports minY - 1 / air).
                surface["y"][index] = None
                surface["materials"][index] = None
                surface["water"][index] = False
                surface["ground_y"][index] = None
                surface["ground_materials"][index] = None
            else:
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

        # Terrain (topmost natural-ground block; non-ground placements/breaks never move it).
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
            self._preserve(key, data)
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

    # ------------------------------------------------------------ bot chunks
    @staticmethod
    def _canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def _same_bot_payload(cls, data, clean) -> bool:
        return (
            cls._canonical(data.get("surface")) == cls._canonical(clean["surface"])
            and cls._canonical(data.get("composition")) == cls._canonical(clean["composition"])
        )

    @staticmethod
    def _validate_bot_record(record):
        if not isinstance(record, dict):
            return None
        try:
            cx, cz = int(record["cx"]), int(record["cz"])
        except (KeyError, TypeError, ValueError):
            return None
        surface = record.get("surface")
        composition = record.get("composition")
        if not isinstance(surface, dict) or not isinstance(composition, dict):
            return None
        clean_surface = {}
        for name in (
            "y", "materials", "ground_y", "ground_materials", "water",
            "terrain_y", "terrain_materials",
        ):
            arr = surface.get(name)
            if not isinstance(arr, list) or len(arr) != 256:
                return None
            clean_surface[name] = list(arr)
        clean_surface["water"] = [bool(v) for v in clean_surface["water"]]
        return {
            "cx": cx, "cz": cz,
            "surface": clean_surface,
            "composition": composition,
        }

    def apply_bot_chunk(self, agent, dimension, record):
        """Replace one chunk from a bot-forwarded payload (``POST /chunk_raw``).

        Modelled on :meth:`apply_snapshot`: the chunk keeps its tier, per-agent
        frozen views are preserved, and the revision/generation only change when
        the payload differs from what is stored.  Unchanged re-forwards (two
        bots, or a chunk re-sent after underground changes) cost nothing.
        Returns ``True`` / ``"unchanged"`` / ``"ignored"`` / ``False``.
        """
        if not is_overworld(dimension):
            return "ignored"
        clean = self._validate_bot_record(record)
        if clean is None:
            return False
        key = (clean["cx"], clean["cz"])
        data = self._acquire(key)
        changed = False
        with self._lock:
            data = self._hot.get(key) or self._warm.get(key) or data
            changed = not self._same_bot_payload(data, clean)
            if changed:
                self._preserve(key, data)
                data["surface"] = {
                    name: list(arr) for name, arr in clean["surface"].items()
                }
                data["composition"] = copy.deepcopy(clean["composition"])
                data["source"] = "bot"
                self._commit(key, data, ("snap", {
                    "cx": key[0], "cz": key[1],
                    "surface": clean["surface"],
                    "counts": (clean["composition"] or {}).get("counts"),
                }))
        # Same store method POST /seen uses: the chunk is seen and loaded now.
        self.report_chunks(agent, [[key[0], key[1]]], None, dimension)
        return True if changed else "unchanged"

    # ------------------------------------------------------- last-seen map: revisions
    def _next_rev(self) -> int:
        self._rev_counter += 1
        return self._rev_counter

    def _ensure_rev(self, key, data):
        """Register a record that has no rev yet as the base state (rev 1)."""
        if data.get("rev"):
            return
        data["rev"] = 1
        if self._pins.get(key):
            self._adopt(key, 1, data.get("save_ts"))
            self._dirty.setdefault(key, data)   # a pinned chunk must outlive tier eviction

    def _set_changed(self, key, data):
        """Give ``data`` a new revision after its surface changed."""
        old = data.get("rev") or 0
        data["rev"] = self._next_rev()
        if old == 0 and self._pins.get(key):
            self._adopt(key, data["rev"])

    def _adopt(self, key, new_rev: int, save_ts=None):
        """First data for a chunk: pins at rev 0 follow it if it arrived inside the grace period or,
        for data read from the save, was written there at or before ``frozen_at + grace``."""
        pins = self._pins.get(key)
        if not pins:
            return
        now = self._clock()
        moved = []
        for agent, (rev, at) in list(pins.items()):
            if rev == 0 and (
                now - at <= FIRST_DATA_GRACE_S
                or (save_ts is not None and save_ts <= at + FIRST_DATA_GRACE_S)
            ):
                pins[agent] = (new_rev, at)
                moved.append((new_rev, agent, OVERWORLD, key[0], key[1]))
        if moved:
            self._conn.executemany(
                "UPDATE seen SET frozen_rev=? WHERE agent=? AND dimension=? AND cx=? AND cz=?", moved
            )
            self._conn.commit()
            self._generation += 1   # what those agents see of the chunk changes (missing -> data)

    def known_rev(self, key):
        """Current rev without loading anything; ``None`` when the store has no record."""
        with self._lock:
            data = self._hot.get(key) or self._warm.get(key) or self._dirty.get(key)
            if data is not None:
                return data.get("rev") or 1
            row = self._conn.execute(
                "SELECT rev FROM chunks WHERE cx=? AND cz=?", key
            ).fetchone()
            return (row[0] or 1) if row else None

    # ------------------------------------------------------- last-seen map: history
    def _preserve(self, key, data):
        """Copy-on-write: keep ``data``'s stored form if an agent is pinned at its rev."""
        pins = self._pins.get(key)
        if not pins or data is None:
            return
        rev = data.get("rev") or 1
        if not any(p[0] == rev for p in pins.values()):
            return
        have = self._hist_by_chunk.get(key)
        if have and rev in have:
            return
        payload = {
            "surface": data.get("surface"),
            "composition": data.get("composition"),
            "save_ts": data.get("save_ts"),
            "loaded_at": data.get("loaded_at"),
            "updated_at": data.get("updated_at"),
        }
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 3)
        self._conn.execute(
            "INSERT OR REPLACE INTO chunk_history (cx, cz, revision, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (key[0], key[1], rev, blob, self._clock()),
        )
        self._hist_index[(key[0], key[1], rev)] = len(blob)
        self._hist_by_chunk.setdefault(key, set()).add(rev)
        self._hist_bytes += len(blob)
        self.history_saved += 1
        self._enforce_history_caps(key, rev)   # commits

    def _drop_history(self, key, rev, commit=True):
        size = self._hist_index.pop((key[0], key[1], rev), None)
        if size is None:
            return
        self._hist_bytes -= size
        revs = self._hist_by_chunk.get(key)
        if revs is not None:
            revs.discard(rev)
            if not revs:
                del self._hist_by_chunk[key]
        self._frozen_cache.pop((key, rev), None)
        self._conn.execute(
            "DELETE FROM chunk_history WHERE cx=? AND cz=? AND revision=?", (key[0], key[1], rev)
        )
        if commit:
            self._conn.commit()

    def _prune_history(self, key):
        """Delete history rows of ``key`` that no pin references any more."""
        revs = self._hist_by_chunk.get(key)
        if not revs:
            return
        used = {p[0] for p in (self._pins.get(key) or {}).values()}
        for rev in [r for r in revs if r not in used]:
            self._drop_history(key, rev)

    def _enforce_history_caps(self, key, keep_rev):
        """Evict the oldest history rows beyond the caps; the row just saved is kept.

        An evicted revision is *lost*: agents still pinned at it see that chunk as missing (never
        as live data) until they load it again. ``history_evicted`` counts these.
        """
        revs = self._hist_by_chunk.get(key) or ()
        while len(revs) > HISTORY_MAX_PER_CHUNK:
            victim = min((r for r in revs if r != keep_rev), default=None)
            if victim is None:
                break
            self._drop_history(key, victim, commit=False)
            self.history_evicted += 1
        keep = (key[0], key[1], keep_rev)
        while self._hist_bytes > HISTORY_MAX_BYTES and len(self._hist_index) > 1:
            oldest = next(iter(self._hist_index))   # insertion order = oldest first
            if oldest == keep:
                self._hist_index.move_to_end(oldest)
                continue
            self._drop_history((oldest[0], oldest[1]), oldest[2], commit=False)
            self.history_evicted += 1
        self._conn.commit()

    def history_record(self, key, rev):
        """Decoded frozen record for ``(key, rev)`` or ``None`` when it was lost."""
        ck = (key, rev)
        with self._lock:
            hit = self._frozen_cache.get(ck)
            if hit is not None:
                self._frozen_cache.move_to_end(ck)
                return hit
            if rev not in (self._hist_by_chunk.get(key) or ()):
                self.history_lost_reads += 1
                return None
            row = self._conn.execute(
                "SELECT data FROM chunk_history WHERE cx=? AND cz=? AND revision=?",
                (key[0], key[1], rev),
            ).fetchone()
            if row is None:
                self.history_lost_reads += 1
                return None
            rec = json.loads(zlib.decompress(row[0]).decode("utf-8"))
            rec.update({"cx": key[0], "cz": key[1], "rev": rev, "frozen": True, "source": "frozen"})
            self._frozen_cache[ck] = rec
            while len(self._frozen_cache) > FROZEN_CACHE_LIMIT:
                self._frozen_cache.popitem(last=False)
            return rec

    # ------------------------------------------------------- last-seen map: agents
    def _init_last_seen(self):
        conn = self._conn
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        try:
            counter = int(meta.get("rev_counter") or 0)
        except ValueError:
            counter = 0
        for sql in (
            "SELECT MAX(rev) FROM chunks",
            "SELECT MAX(frozen_rev) FROM seen",
            "SELECT MAX(revision) FROM chunk_history",
        ):
            row = conn.execute(sql).fetchone()
            counter = max(counter, (row[0] or 0) if row else 0)
        self._rev_counter = max(1, counter)
        for hx, hz, hrev, size in conn.execute(
            "SELECT cx, cz, revision, LENGTH(data) FROM chunk_history ORDER BY created_at"
        ).fetchall():
            self._hist_index[(hx, hz, hrev)] = size
            self._hist_by_chunk.setdefault((hx, hz), set()).add(hrev)
            self._hist_bytes += size
        for name in meta:
            if name.startswith("tracked:"):
                agent = name[len("tracked:"):]
                self._agents[agent] = {"tracked": True, "loaded": set(), "last_report": 0.0}
                self._resolve_unknown_pins(agent)
                self._load_pins(agent)
        # history nobody references any more (an agent loaded the chunk, then the service stopped)
        for key in list(self._hist_by_chunk):
            self._prune_history(key)

    def _resolve_unknown_pins(self, agent):
        """Rows with no pin (the agent had them loaded when the service stopped, or they predate the
        last-seen map) become frozen at the chunk's current rev: nothing is loaded after a restart."""
        self.flush()   # the rows must carry the revs of the records in memory
        self._conn.execute(
            "UPDATE seen SET frozen_rev=COALESCE((SELECT c.rev FROM chunks c WHERE c.cx=seen.cx AND c.cz=seen.cz), 1), "
            "frozen_at=? WHERE agent=? AND dimension=? AND frozen_rev IS NULL",
            (self._clock(), agent, OVERWORLD),
        )
        self._conn.commit()

    def _load_pins(self, agent):
        rows = self._conn.execute(
            "SELECT cx, cz, frozen_rev, COALESCE(frozen_at, 0) FROM seen "
            "WHERE agent=? AND dimension=? AND frozen_rev IS NOT NULL",
            (agent, OVERWORLD),
        ).fetchall()
        for cx, cz, rev, at in rows:
            self._pins.setdefault((cx, cz), {})[agent] = (rev, at)
            if rev == 0:
                self._queue_unresolved((cx, cz), 0.0)

    def _agent(self, agent):
        st = self._agents.get(agent)
        if st is None:
            st = self._agents[agent] = {"tracked": False, "loaded": set(), "last_report": 0.0}
        return st

    def _become_tracked(self, agent, st):
        st["tracked"] = True
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')", ("tracked:" + agent,)
        )
        self._resolve_unknown_pins(agent)
        self._load_pins(agent)

    def pin_of(self, agent, key):
        pins = self._pins.get(key)
        return pins.get(agent) if pins else None

    def within_grace(self, pin) -> bool:
        return self._clock() - pin[1] <= FIRST_DATA_GRACE_S

    def loaded_snapshot(self, agent):
        """``(tracked, loaded_chunks)`` for the gated view of ``agent``."""
        with self._lock:
            st = self._agents.get(agent)
            if st is None or not st["tracked"]:
                return False, frozenset()
            return True, frozenset(st["loaded"])

    def resolve_after_live(self, agent, key, live):
        """What ``agent`` sees of a chunk it does not have loaded, given the live record."""
        if live is None:
            return None
        pin = self.pin_of(agent, key)
        if pin is None:
            return live
        if pin[0] == 0:
            return None
        if pin[0] >= (live.get("rev") or 1):
            return live
        return self.history_record(key, pin[0])

    def _pin_unloaded(self, agent, keys, now):
        """Freeze ``keys`` for ``agent`` at their current revision (existing pins keep theirs)."""
        rows = []
        for key in keys:
            pins = self._pins.setdefault(key, {})
            if agent in pins:
                continue
            rev = self.known_rev(key) or 0
            pins[agent] = (rev, now)
            if rev == 0:
                self._queue_unresolved(key, now)
            data = self._hot.get(key) or self._warm.get(key)
            if data is not None:
                self._dirty.setdefault(key, data)   # keep the pinned revision reachable
            rows.append((rev, now, now, agent, OVERWORLD, key[0], key[1]))
        if rows:
            self._conn.executemany(
                "UPDATE seen SET frozen_rev=?, frozen_at=?, last_seen=? "
                "WHERE agent=? AND dimension=? AND cx=? AND cz=?", rows
            )

    # ------------------------------------------------- last-seen map: eager rev-0 resolution
    def _queue_unresolved(self, key, due):
        if key not in self._unresolved and len(self._unresolved) < MAX_UNRESOLVED:
            self._unresolved[key] = [due, 0]

    def _zero_deadline(self, key):
        """Latest ``frozen_at + grace`` over the rev-0 pins of ``key`` (``None`` if there are none)."""
        pins = self._pins.get(key)
        ats = [at for rev, at in pins.values() if rev == 0] if pins else []
        return max(ats) + FIRST_DATA_GRACE_S if ats else None

    def _resolve_key(self, key, now) -> bool:
        """Try to give the rev-0 pins of ``key`` their data from the save. True when finished
        (resolved, or nothing more can change); False to retry later. Never promotes to warm/hot."""
        with self._lock:
            deadline = self._zero_deadline(key)
            if deadline is None or self.known_rev(key) is not None:
                return True
        loader = self.loader
        ts_fn = getattr(loader, "chunk_timestamp", None)
        if loader is None or not callable(ts_fn):
            return True
        try:
            ts = ts_fn(key[0], key[1])
            if ts is None:
                return now > deadline          # not written yet (new chunks are saved at autosave)
            if ts > deadline:
                return True                    # written after the grace period: not "as last seen"
            data = loader.load_chunk(key[0], key[1])
        except Exception:  # pragma: no cover - defensive
            log.exception("Resolving pinned chunk %s failed", key)
            return now > deadline
        if data is None:
            return now > deadline
        with self._lock:
            if self.known_rev(key) is not None or self._zero_deadline(key) is None:
                return True
            data["source"] = "base"
            data["loaded_at"] = self._clock()
            if not data.get("save_ts"):
                data["save_ts"] = ts
            self._ensure_rev(key, data)   # adopts the rev-0 pins by save time
            self._dirty[key] = data       # straight to the cold tier on the next flush
        return True

    def _finish_attempt(self, key, state, now, done):
        if done:
            self._unresolved.pop(key, None)
        else:
            state[1] += 1
            state[0] = now + min(RESOLVE_RETRY_MIN_S * 2 ** (state[1] - 1), RESOLVE_RETRY_MAX_S)

    def resolve_now(self, key) -> None:
        """Lazy path for a query: resolve a queued rev-0 pin right away when its retry is due."""
        state = self._unresolved.get(key)
        if state is None:
            return
        now = self._clock()
        if state[0] > now:
            return
        self._finish_attempt(key, state, now, self._resolve_key(key, now))

    def resolve_pending(self, limit: int = RESOLVE_BATCH, budget_s: float = RESOLVE_BUDGET_S, now=None) -> int:
        """One bounded background step; returns how many queued pins were still due afterwards."""
        now = self._clock() if now is None else now
        with self._lock:
            due = sorted((st[0], key) for key, st in self._unresolved.items() if st[0] <= now)
        started = time.monotonic()
        for n, (_t, key) in enumerate(due):
            if n >= limit or time.monotonic() - started > budget_s:
                return len(due) - n
            state = self._unresolved.get(key)
            if state is not None:
                self._finish_attempt(key, state, now, self._resolve_key(key, now))
        return 0

    def unresolved_count(self) -> int:
        return len(self._unresolved)

    def _unpin_loaded(self, agent, keys):
        for key in keys:
            pins = self._pins.get(key)
            if not pins or agent not in pins:
                continue
            rev = pins.pop(agent)[0]
            if not pins:
                del self._pins[key]
            cur = self.known_rev(key)
            if rev and cur is not None and rev < cur:
                self._generation += 1   # what this agent sees of the chunk changes (frozen -> live)
            self._prune_history(key)

    def sweep_loaded(self, now=None):
        """Treat tracked agents that stopped reporting as having unloaded everything."""
        now = self._clock() if now is None else now
        with self._lock:
            for agent, st in self._agents.items():
                if st["tracked"] and st["loaded"] and now - st["last_report"] > LOADED_TTL_S:
                    self._pin_unloaded(agent, sorted(st["loaded"]), now)
                    st["loaded"].clear()
                    self._conn.commit()

    def report_chunks(self, agent, chunks=(), unloaded=None, dimension=None, replace=False) -> dict:
        """Record which chunks ``agent`` has loaded (``chunks``) and unloaded (``unloaded``).

        Both count as seen. ``unloaded`` / ``replace`` switch the agent to the tracked protocol
        (frozen views); with ``replace`` the ``chunks`` list is the agent's complete loaded set.
        A chunk in both lists ends unloaded. Returns ``{"loaded": n, "unloaded": m}``.
        """
        dim = normalize_dimension(dimension)
        agent = str(agent)
        loads = self._clean_chunks(chunks)
        unl = self._clean_chunks(unloaded)
        replace = bool(replace) and len(chunks or ()) <= MAX_SEEN_PER_CALL
        now = self._clock()
        if not loads and not unl and not replace:
            return {"loaded": 0, "unloaded": 0}
        with self._lock:
            seen = self._seen_set(agent, dim)
            if dim != OVERWORLD:
                self._upsert_seen(agent, dim, loads + unl, now)
                self._conn.commit()
                seen.update(loads + unl)
                return {"loaded": len(loads), "unloaded": len(unl)}
            st = self._agent(agent)
            if (unloaded is not None or replace) and not st["tracked"]:
                self._become_tracked(agent, st)
            st["last_report"] = now
            tracked = st["tracked"]
            self._upsert_seen(agent, dim, loads, now, loaded=True)
            seen.update(loads)
            if tracked:
                st["loaded"].update(loads)
                self._unpin_loaded(agent, loads)
            gone = set(unl)
            if replace and tracked:
                gone |= st["loaded"] - set(loads)
            fresh = [k for k in gone if k not in seen]
            if fresh:
                self._upsert_seen(agent, dim, fresh, now, loaded=True)
                seen.update(fresh)
            if tracked and gone:
                st["loaded"] -= gone
                self._pin_unloaded(agent, sorted(gone), now)
            self._conn.commit()
        return {"loaded": len(loads), "unloaded": len(gone)}

    @staticmethod
    def _clean_chunks(items):
        clean = []
        for item in items or []:
            if len(clean) >= MAX_SEEN_PER_CALL:
                break
            try:
                clean.append((int(item[0]), int(item[1])))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        return clean

    def _upsert_seen(self, agent, dim, keys, now, loaded=False):
        if not keys:
            return
        self._conn.executemany(
            "INSERT INTO seen (agent, dimension, cx, cz, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent, dimension, cx, cz) DO UPDATE SET last_seen=excluded.last_seen"
            + (", frozen_rev=NULL, frozen_at=NULL" if loaded else ""),
            [(agent, dim, cx, cz, now, now) for cx, cz in keys],
        )

    # ------------------------------------------------------------ seen ledger
    def _seen_set(self, agent: str, dimension: str = OVERWORLD) -> set:
        """Lazily loaded in-memory set of (cx, cz) the agent has seen."""
        key = (str(agent), normalize_dimension(dimension))
        with self._lock:
            cached = self._seen_cache.get(key)
            if cached is None:
                rows = self._conn.execute(
                    "SELECT cx, cz FROM seen WHERE agent=? AND dimension=?", key
                ).fetchall()
                cached = self._seen_cache[key] = {(r[0], r[1]) for r in rows}
            return cached

    def mark_seen(self, agent: str, chunks, dimension=None) -> int:
        """Upsert chunks into the agent's ledger (at most 5000 per call); they count as loaded.

        The legacy entry point: it never switches an agent to the tracked protocol.
        """
        return self.report_chunks(agent, chunks, None, dimension)["loaded"]

    def seen_filter(self, agent: str, chunk_keys, dimension=None) -> set:
        """Return the subset of ``chunk_keys`` the agent has seen."""
        seen = self._seen_set(agent, normalize_dimension(dimension))
        return {k for k in chunk_keys if k in seen}

    def is_seen(self, agent: str, cx: int, cz: int) -> bool:
        return (cx, cz) in self._seen_set(agent)

    def seen_stats(self, agent: str, dimension=None) -> dict:
        dim = normalize_dimension(dimension)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), MIN(first_seen), MAX(last_seen) FROM seen "
                "WHERE agent=? AND dimension=?",
                (str(agent), dim),
            ).fetchone()
        return {
            "agent": str(agent),
            "chunks": row[0] or 0,
            "first_seen": row[1],
            "last_seen": row[2],
        }

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
                        data.get("rev") or 1,
                    )
                )
            self._dirty.clear()
            self._conn.executemany(
                "INSERT INTO chunks (cx, cz, surface_json, composition_json, updated_at, "
                "save_ts, loaded_at, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cx, cz) DO UPDATE SET "
                "surface_json=excluded.surface_json, "
                "composition_json=excluded.composition_json, "
                "updated_at=excluded.updated_at, "
                "save_ts=excluded.save_ts, "
                "loaded_at=excluded.loaded_at, "
                "rev=excluded.rev",
                rows,
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('rev_counter', ?)",
                (str(self._rev_counter),),
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
                try:
                    self.sweep_loaded()
                except Exception:  # pragma: no cover - defensive
                    log.exception("sweep_loaded failed")

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
                "history_rows": len(self._hist_index),
                "history_bytes": self._hist_bytes,
                "history_evicted": self.history_evicted,
                "pinned_chunks": len(self._pins),
                "unresolved_pins": len(self._unresolved),
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
