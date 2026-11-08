"""Per-agent gated view of the map store.

``StoreView(store, agent)`` exposes the same read methods the query functions use
(``get_or_load``, ``get_chunk``, ``touch_window``, ``get_window`` ...) with two rules:

* a chunk that is not in the agent's seen ledger is MISSING: the data is never returned. Chunks
  refused this way are collected in ``unexplored`` (a set of (cx, cz));
* a seen chunk the agent does not currently have loaded is shown *as it was when the agent last
  had it* (``rec["frozen"]`` is True; the record comes from the store's copy-on-write history).
  Chunks the agent has loaded, and every chunk of an agent that never reported unloads (old
  clients), are live.

Gating and freezing live here, at the store boundary, so endpoints cannot forget them. Frozen
records are never placed in the hot/warm tiers and are shared read-only between agents pinned at
the same revision; the query modules key their memo caches on the record's ``rev`` as well as
its identity, so a frozen record is never served as live data or the other way round.
"""

from __future__ import annotations

_DELEGATED = {"generation", "loader", "stats", "world_id"}


class StoreView:
    def __init__(self, store, agent: str):
        self._store = store
        self.agent = str(agent)
        self.unexplored: set = set()
        store.sweep_loaded()
        self._tracked, self._loaded = store.loaded_snapshot(self.agent)

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

    def _fetch(self, cx, cz, live_fn):
        key = (int(cx), int(cz))
        if not self._allowed(*key):
            return None
        if self._tracked and key not in self._loaded:
            store = self._store
            pin = store.pin_of(self.agent, key)
            if pin is not None:
                if pin[0] == 0 and not store.within_grace(pin):
                    return None   # seen when the store had nothing; later data is not "as last seen"
                rev = store.known_rev(key)
                if pin[0] and rev is not None and pin[0] < rev:
                    return store.history_record(key, pin[0])   # changed since: no live read at all
                return store.resolve_after_live(self.agent, key, live_fn(*key))
        return live_fn(*key)

    def _resolve(self, key, rec):
        if self._tracked and key not in self._loaded:
            return self._store.resolve_after_live(self.agent, key, rec)
        return rec

    def get_chunk(self, cx, cz):
        return self._fetch(cx, cz, self._store.get_chunk)

    def get_or_load(self, cx, cz):
        return self._fetch(cx, cz, self._store.get_or_load)

    def get_chunk_at(self, x, z):
        return self.get_chunk(int(x) // 16, int(z) // 16)

    def touch_window(self, center_cx, center_cz, size_chunks):
        size = int(size_chunks)
        half = size // 2
        cx1, cz1 = int(center_cx) - half, int(center_cz) - half
        seen = self._seen()
        window = [(cx, cz) for cz in range(cz1, cz1 + size) for cx in range(cx1, cx1 + size)]
        unseen = {k for k in window if k not in seen}
        self.unexplored |= unseen
        records, missing = self._store.touch_window(center_cx, center_cz, size_chunks)
        kept = []
        for r in records:
            key = (r["cx"], r["cz"])
            if key not in seen:
                continue
            r = self._resolve(key, r)
            if r is not None:
                kept.append(r)
        return kept, size * size - len(kept)

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
