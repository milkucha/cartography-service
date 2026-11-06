"""Topography map service entry point.

Usage:
    python main.py --world "C:/path/to/server/world" [--host 127.0.0.1] [--port 8090]

The service always indexes the save's region files lazily. Use --preload to
decode every chunk up front (only sensible for small test worlds).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time

import loader
import map_store
import prefetch
from api import TopographyAPI

log = logging.getLogger("topography")


def build_parser():
    parser = argparse.ArgumentParser(description="Mindcraft topography map service")
    parser.add_argument(
        "--world",
        default=os.environ.get("TOPOGRAPHY_WORLD", "world"),
        help="Path to the Minecraft world/save folder (default: $TOPOGRAPHY_WORLD or ./world)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TOPOGRAPHY_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TOPOGRAPHY_PORT", "8090")),
        help="HTTP port (default: 8090)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("TOPOGRAPHY_DB"),
        help=(
            "SQLite cold-store path (default: maps/<level>-<worldid>.db next to this file, one "
            "database per world so caches and seen-chunk ledgers never mix between worlds)"
        ),
    )
    parser.add_argument(
        "--preload",
        action="store_true",
        default=os.environ.get("TOPOGRAPHY_PRELOAD", "").lower() in ("1", "true", "yes"),
        help="Decode every chunk from the save at startup (slow on large worlds)",
    )
    parser.add_argument(
        "--preload-limit",
        type=int,
        default=int(os.environ.get("TOPOGRAPHY_PRELOAD_LIMIT", "0")),
        help="Max chunks to preload, 0 = unlimited",
    )
    return parser


def default_db_path(chunk_loader) -> str:
    """One database per world: ``maps/<level name>-<world id>.db``.

    The world id (region dir + seed) is stable for a given save, so switching the
    ``--world`` argument between runs switches databases instead of wiping or
    reusing another world's chunk cache and per-agent "seen" ledger.
    Falls back to the legacy ``map_store.db`` when there is no readable save.
    """
    world_id = chunk_loader.world_id() if getattr(chunk_loader, "available", False) else None
    if not world_id:
        return "map_store.db"
    level = chunk_loader.region_dir.parent.name or "world"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", level).strip("._") or "world"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", f"{slug[:40]}-{world_id[:8]}.db")


def preload(store, limit: int = 0):
    if store.loader is None or not store.loader.available:
        log.warning("Preload requested but no readable world save was found")
        return
    started = time.time()
    count = 0
    for record in store.loader.iter_all_chunks():
        store.put_chunk(record)
        count += 1
        if count % 1000 == 0:
            store.flush()
            log.info("Preloaded %s chunks...", count)
        if limit and count >= limit:
            break
    store.flush()
    log.info("Preloaded %s chunks in %.1fs", count, time.time() - started)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)

    chunk_loader = loader.ChunkLoader(args.world)
    if chunk_loader.available:
        indexed = chunk_loader.region_count
        log.info("World save: %s (%s region files)", chunk_loader.region_dir, indexed)
    else:
        log.warning(
            "No region/ folder found under %r. Running in diff-only mode "
            "(the Fabric plugin can still push live changes).",
            args.world,
        )

    db_path = args.db or default_db_path(chunk_loader)
    log.info("Map database: %s", db_path)
    store = map_store.MapStore(db_path, chunk_loader)
    store.start_flush_thread()

    if args.preload:
        preload(store, args.preload_limit)

    api = TopographyAPI(store, args.host, args.port)
    prefetcher = api.prefetcher
    prefetcher.start()
    server_thread = threading.Thread(target=api.serve_forever, name="topography-http", daemon=True)
    server_thread.start()

    try:
        while server_thread.is_alive():
            server_thread.join(0.5)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        api.shutdown()
        prefetcher.stop()
        store.stop()
        chunk_loader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
