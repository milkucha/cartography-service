"""Tiered terrain map store.

Hot   - chunks in the most recent perception window (in memory, window-driven)
Warm  - recently queried/loaded chunks outside the window, LRU (in memory)
Cold  - every chunk ever loaded or updated, persisted to SQLite

The hot and warm tiers are pure caches; correctness always falls back to
SQLite, then to the base-state loader reading the world save.

Tier rules:
  * ``touch_window`` makes exactly the requested window hot and demotes any
    hot chunk that left the window into warm.
  * Detailed queries (``get_chunk``/``get_or_load``) promote to warm only,
    unless the chunk is already hot.
  * Diffs update a resident chunk in place (hot stays hot, warm stays warm);
    a non-resident chunk is loaded and stored in warm. Diffs never promote.
  * ``put_chunk`` (preload) stores in warm, never hot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

import mc_blocks

log = logging.getLogger("topography.store")

MAX_PERCEPTION_CHUNKS = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    surface_json TEXT NOT NULL,
    composition_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (cx, cz)
);
"""


class MapStore:
    def __init__(self, db_path: str, loader, hot_limit: int = 2048, warm_limit: int = 2048):
        self.loader = loader
        self.hot_limit = hot_limit
        self.warm_limit = warm_limit
        self._lock = threading.RLock()
        self._hot: OrderedDict[tuple[int, int], dict] = OrderedDict()
        self._warm: OrderedDict[tuple[int, int], dict] = OrderedDict()
        self._dirty: dict[tuple[int, int], dict] = {}
        self._generation = 0
        self._stop = threading.Event()
        self._flush_thread: threading.Thread | None = None

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(SCHEMA)
        self._conn.commit()

    @property
    def generation(self) -> int:
        """Monotonic counter incremented whenever a diff is applied."""
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
            "SELECT surface_json, composition_json, updated_at FROM chunks "
            "WHERE cx=? AND cz=?",
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
            "source": "cold",
        }

    def _get_data(self, cx: int, cz: int, include_save: bool = True):
        """Read hot -> warm -> cold -> save without promoting anything."""
        key = (int(cx), int(cz))
        with self._lock:
            data = self._hot.get(key)
            if data is not None:
                return data
            data = self._warm.get(key)
            if data is not None:
                return data
            data = self._read_cold(key[0], key[1])
            if data is not None:
                return data
        if include_save and self.loader is not None:
            return self.loader.load_chunk(key[0], key[1])
        return None

    def get_chunk(self, cx: int, cz: int):
        """Detailed read path: promote to warm, never to hot."""
        key = (int(cx), int(cz))
        with self._lock:
            data = self._hot.get(key)
            if data is not None:
                return data
            data = self._warm.get(key)
            if data is not None:
                self._warm.move_to_end(key)
                return data
            data = self._read_cold(key[0], key[1])
            if data is not None:
                self._remember_warm(key, data)
            return data

    def get_or_load(self, cx: int, cz: int):
        """Detailed read path: promote to warm, loading the save on a miss."""
        key = (int(cx), int(cz))
        with self._lock:
            data = self._hot.get(key)
            if data is not None:
                return data
            data = self._warm.get(key)
            if data is not None:
                self._warm.move_to_end(key)
                return data
            data = self._read_cold(key[0], key[1])
            if data is not None:
                self._remember_warm(key, data)
                return data
        if self.loader is None:
            return None
        data = self.loader.load_chunk(key[0], key[1])
        if data is None:
            return None
        with self._lock:
            data["source"] = "base"
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
            },
            "composition": {
                "counts": {"natural": 0, "engineered": 0, "unknown": 0, "liquid": 0, "air": 0},
                "top_blocks": [],
                "strong_indicators": [],
                "strong_indicator_count": 0,
                "dominant_biome": None,
                "artificiality_score": 0.0,
                "surface_min_y": None,
            },
            "source": "diff",
        }

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
        if action in ("chunk_load", "chunk_unload"):
            return True
        if action == "place" and block_name is None:
            return False

        cx, cz = x // 16, z // 16
        key = (cx, cz)
        index = (x % 16) + (z % 16) * 16

        with self._lock:
            data = self._hot.get(key)
            tier = "hot" if data is not None else None
            if data is None:
                data = self._warm.get(key)
                if data is not None:
                    tier = "warm"
            if data is None:
                data = self._read_cold(cx, cz)
                if data is not None:
                    tier = "cold"
        if data is None and self.loader is not None:
            data = self.loader.load_chunk(cx, cz)
            if data is not None:
                tier = "base"
        if data is None:
            data = self._blank_chunk(cx, cz)
            tier = None

        with self._lock:
            if key in self._hot:
                data, tier = self._hot[key], "hot"
            elif key in self._warm:
                data, tier = self._warm[key], "warm"

            surface = data["surface"]
            old_material = surface["materials"][index]

            if action == "place":
                if surface["y"][index] is None or y >= surface["y"][index]:
                    surface["y"][index] = y
                    surface["materials"][index] = block_name
                    surface["water"][index] = block_name in {
                        "minecraft:water", "minecraft:bubble_column", "minecraft:kelp",
                        "minecraft:kelp_plant", "minecraft:seagrass", "minecraft:tall_seagrass",
                    }
                if block_name not in mc_blocks.AIR and y >= (surface["ground_y"][index] or -9999):
                    if block_name not in _NON_SOLID:
                        surface["ground_y"][index] = y
                        surface["ground_materials"][index] = block_name
                self._adjust_composition(data, block_name, +1)
            else:  # break
                new_top_y = delta.get("new_top_y")
                new_top_block = delta.get("new_top_block")
                new_ground_y = delta.get("new_ground_y")
                new_ground_block = delta.get("new_ground_block")
                if new_top_y is not None and new_top_block is not None:
                    new_top_y = int(new_top_y)
                    if surface["y"][index] is None or new_top_y <= surface["y"][index]:
                        surface["y"][index] = new_top_y
                        surface["materials"][index] = new_top_block
                        surface["water"][index] = new_top_block in {
                            "minecraft:water", "minecraft:bubble_column", "minecraft:kelp",
                            "minecraft:kelp_plant", "minecraft:seagrass", "minecraft:tall_seagrass",
                        }
                        if new_ground_y is not None and new_ground_block is not None:
                            surface["ground_y"][index] = int(new_ground_y)
                            surface["ground_materials"][index] = new_ground_block
                        elif new_top_block not in _NON_SOLID:
                            surface["ground_y"][index] = new_top_y
                            surface["ground_materials"][index] = new_top_block
                elif surface["y"][index] is not None and y == surface["y"][index]:
                    surface["y"][index] = y - 1
                    surface["materials"][index] = None
                broken_block = block_name or old_material
                if broken_block:
                    self._adjust_composition(data, broken_block, -1)

            data["source"] = "diff"
            if tier == "hot":
                self._hot[key] = data
            elif tier == "warm":
                self._warm[key] = data
                self._warm.move_to_end(key)
            else:
                self._remember_warm(key, data)
            self._dirty[key] = data
            self._generation += 1
        return True

    def _adjust_composition(self, data: dict, block_name: str, amount: int):
        comp = data.setdefault("composition", {})
        counts = comp.setdefault("counts", {})
        cls = mc_blocks.classify_block(block_name)
        counts[cls] = max(0, counts.get(cls, 0) + amount)
        indicators = comp.setdefault("strong_indicators", [])
        if mc_blocks.is_strong_indicator(block_name):
            if amount > 0 and block_name not in indicators:
                indicators.append(block_name)
                comp["strong_indicator_count"] = comp.get("strong_indicator_count", 0) + 1
            elif amount < 0 and block_name in indicators:
                indicators.remove(block_name)
                comp["strong_indicator_count"] = max(
                    0, comp.get("strong_indicator_count", 0) - 1
                )

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
                    )
                )
            self._dirty.clear()
            self._conn.executemany(
                "INSERT INTO chunks (cx, cz, surface_json, composition_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(cx, cz) DO UPDATE SET "
                "surface_json=excluded.surface_json, "
                "composition_json=excluded.composition_json, "
                "updated_at=excluded.updated_at",
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
