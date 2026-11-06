"""Per-agent gated view of the map store.

``StoreView(store, agent)`` exposes the same read methods the query functions use
(``get_or_load``, ``get_chunk``, ``touch_window``, ``get_window`` ...) but treats every chunk
that is not in the agent's seen ledger as MISSING: the data is never returned. Gating lives
here, at the store boundary, so endpoints cannot forget it. Chunks refused this way are
collected in ``unexplored`` (a set of (cx, cz)).
"""

from __future__ import annotations

_DELEGATED = {"generation", "loader", "stats", "world_id"}


class StoreView:
    def __init__(self, store, agent: str):
        self._store = store
        self.agent = str(agent)
        self.unexplored: set = set()

    def _seen(self):
        return self._store._seen_set(self.agent)

    def __getattr__(self, name):
        if name in _DELEGATED:
            return getattr(self._store, name)
        raise AttributeError(name)

    def _allowed(self, cx, cz) -> bool:
        if (int(cx), int(cz)) in self._seen():
            return True
        self.unexplored.add((int(cx), int(cz)))
        return False

    def get_chunk(self, cx, cz):
        return self._store.get_chunk(cx, cz) if self._allowed(cx, cz) else None

    def get_or_load(self, cx, cz):
        return self._store.get_or_load(cx, cz) if self._allowed(cx, cz) else None

    def get_chunk_at(self, x, z):
        return self.get_chunk(int(x) // 16, int(z) // 16)

    def touch_window(self, center_cx, center_cz, size_chunks):
        records, missing = self._store.touch_window(center_cx, center_cz, size_chunks)
        seen = self._seen()
        kept = []
        for rec in records:
            key = (rec["cx"], rec["cz"])
            if key in seen:
                kept.append(rec)
            else:
                self.unexplored.add(key)
                missing += 1
        return kept, missing

    def get_window(self, center_cx, center_cz, radius_chunks):
        records, missing = [], 0
        r = max(0, int(radius_chunks))
        for cx in range(center_cx - r, center_cx + r + 1):
            for cz in range(center_cz - r, center_cz + r + 1):
                data = self.get_or_load(cx, cz)
                if data is None:
                    missing += 1
                else:
                    records.append(data)
        return records, missing


def unexplored_keys(store) -> set:
    """Chunks refused so far by a gated view (empty for a plain store)."""
    return getattr(store, "unexplored", None) or set()
