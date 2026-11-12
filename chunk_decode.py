"""Decode one bot-fed chunk into a store record.

Top-level and free of shared state so it can run in a worker process (``api.TopographyAPI`` with
``decode_workers``): decoding is pure Python and CPU-bound, and in the server process it competes
for the interpreter lock with every interactive request (/route, /perception).
"""

from __future__ import annotations

import logging

import bot_chunk
import composition
import loader

log = logging.getLogger("topography.decode")

OK, LIMIT, REJECTED = "ok", "limit", "rejected"


def decode_record(chunk):
    """``(OK, cx, cz, record)``, ``(LIMIT, message)`` for a payload over a limit, or ``(REJECTED,)``."""
    try:
        cx, cz, root = bot_chunk.to_root(chunk)
    except bot_chunk.PayloadLimit as exc:
        return (LIMIT, str(exc))
    except (bot_chunk.PayloadError, TypeError, ValueError):
        return (REJECTED,)
    try:
        surface = loader.decode_surface(root)
        stats = composition.analyze_chunk(root, cx, cz) or {}
        record = loader.build_chunk_record(cx, cz, surface, stats, source="bot")
    except Exception:  # pragma: no cover - defensive
        log.exception("Failed to decode a bot chunk (%s, %s)", cx, cz)
        return (REJECTED,)
    return (OK, cx, cz, record)
