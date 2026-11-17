"""HTTP API queried by Mindcraft and fed by the Fabric diff plugin."""

from __future__ import annotations

import hmac
import json
import logging
import math
import random
import zlib
import concurrent.futures
import concurrent.futures.process
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bot_chunk
import chunk_decode
import landforms
import map_store
import overview
import perception
import prefetch
import route
import settlements
import store_view
import timing
import survey
import terrain_queries
import topography

log = logging.getLogger("cartography.api")

MAX_RADIUS = 256
MAX_COORD = 30_000_000
MAX_LINE_LENGTH = 4096
MAX_BODY_BYTES = 1_000_000
MAX_CHUNK_RAW_BYTES = 4_000_000
MAX_CHUNK_RAW_COMPRESSED = 4_000_000
MAX_PERCEPTION_SIZE = 32
MAX_PERCEPTION_CHUNKS = 1024
DEFAULT_PERCEPTION_SIZE = 16


class TopographyAPI:
    def __init__(self, store, host: str = "127.0.0.1", port: int = 8090, prefetcher=None, token=None,
                 decode_workers: int = 0):
        self.store = store
        self.host = host
        self.port = port
        self.token = token or None
        self.server: ThreadingHTTPServer | None = None
        self.prefetcher = prefetcher or prefetch.PrefetchWorker(store)
        # Bot-chunk decoding in worker processes keeps it off the interpreter lock that /route and
        # /perception need; 0 decodes in the request thread (tests, tiny deployments).
        self.decode_workers = max(0, int(decode_workers or 0))
        self._decode_pool = None
        self._decode_pool_lock = threading.Lock()

    def decode_chunks(self, chunks):
        """``chunk_decode.decode_record`` for each chunk, in the worker pool when there is one."""
        if self.decode_workers:
            with self._decode_pool_lock:
                if self._decode_pool is None:
                    self._decode_pool = concurrent.futures.ProcessPoolExecutor(
                        max_workers=self.decode_workers, initializer=chunk_decode.watch_parent)
                pool = self._decode_pool
            try:
                return list(pool.map(chunk_decode.decode_record, chunks))
            except concurrent.futures.process.BrokenProcessPool:
                log.exception("Decode workers failed; decoding in the server from now on")
                with self._decode_pool_lock:
                    self.decode_workers = 0
                    self._decode_pool = None
        return [chunk_decode.decode_record(chunk) for chunk in chunks]

    def make_handler(self):
        api_self = self
        store = self.store
        prefetcher = self.prefetcher
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            server_version = "TopographyService/1.0"

            def log_message(self, fmt, *args):
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send(self, status: int, payload, content_type="application/json"):
                if isinstance(payload, bytes):
                    body = payload
                elif isinstance(payload, (dict, list)):
                    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                else:
                    body = str(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _error(self, status: int, message: str):
                self._send(status, {"error": message})

            def _authorized(self):
                if not token:
                    return True
                header = self.headers.get("Authorization") or ""
                return hmac.compare_digest(header, f"Bearer {token}")

            def do_OPTIONS(self):
                self._send(204, b"", "text/plain")

            def do_GET(self):
                with timing.RequestTimer("GET", urlparse(self.path).path):
                    return self._do_get()

            def do_POST(self):
                with timing.RequestTimer("POST", urlparse(self.path).path):
                    return self._do_post()

            def _do_get(self):
                if not self._authorized():
                    return self._error(401, "Unauthorized")
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                try:
                    if parsed.path in ("/", "/health"):
                        return self._health()
                    dimension = (params.get("dimension") or [None])[0]
                    if not map_store.is_overworld(dimension):
                        return self._unavailable_dimension(dimension)
                    if parsed.path == "/seen_stats":
                        return self._seen_stats(params)
                    if parsed.path == "/perception":
                        return self._perception(params)
                    if parsed.path == "/topography":
                        return self._topography(params)
                    if parsed.path == "/area_composition":
                        return self._area_composition(params)
                    if parsed.path == "/shoreline":
                        return self._shoreline(params)
                    if parsed.path == "/flat_areas":
                        return self._flat_areas(params)
                    if parsed.path == "/bridge_span":
                        return self._bridge_span(params)
                    if parsed.path == "/recall":
                        return self._recall(params)
                    if parsed.path == "/chunk":
                        return self._chunk(params)
                    if parsed.path == "/settlements":
                        return self._settlements(params)
                    if parsed.path == "/landforms":
                        return self._landforms(params)
                    if parsed.path == "/survey":
                        return self._survey(params)
                    if parsed.path == "/route":
                        return self._route(params)
                    if parsed.path == "/overview":
                        return self._overview(params)
                    return self._error(404, f"Unknown endpoint {parsed.path}")
                except ValueError as exc:
                    return self._error(400, str(exc))
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("Unhandled GET error")
                    return self._error(500, f"Internal error: {exc}")

            def _do_post(self):
                parsed = urlparse(self.path)
                if parsed.path not in ("/diff", "/position", "/chunk_snapshot", "/seen", "/chunk_raw"):
                    return self._error(404, f"Unknown endpoint {parsed.path}")
                if not self._authorized():
                    return self._error(401, "Unauthorized")
                raw_endpoint = parsed.path == "/chunk_raw"
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._error(400, "Invalid Content-Length")
                if length <= 0:
                    return self._error(400, "Empty body")
                if length > (MAX_CHUNK_RAW_COMPRESSED if raw_endpoint else MAX_BODY_BYTES):
                    return self._error(413, "Body too large")
                raw = self.rfile.read(length)
                if raw_endpoint:
                    if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                        # Bounded: decompress at most one byte past the documented limit.
                        d = zlib.decompressobj(wbits=31)
                        try:
                            raw = d.decompress(raw, MAX_CHUNK_RAW_BYTES + 1)
                        except zlib.error as exc:
                            return self._error(400, f"Invalid gzip body: {exc}")
                        if len(raw) > MAX_CHUNK_RAW_BYTES or d.unconsumed_tail:
                            return self._error(413, "Body too large")
                    if len(raw) > MAX_CHUNK_RAW_BYTES:
                        return self._error(413, "Body too large")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return self._error(400, f"Invalid JSON: {exc}")
                if parsed.path == "/position":
                    return self._position(payload)
                if parsed.path == "/chunk_snapshot":
                    return self._chunk_snapshot(payload)
                if parsed.path == "/seen":
                    return self._seen(payload)
                if raw_endpoint:
                    return self._chunk_raw(payload)
                return self._diff(payload)

            # ------------------------------------------------------- endpoints
            def _target(self, params):
                """Store, or an agent-gated StoreView when ``agent=`` is given."""
                agent = (params.get("agent") or [""])[0].strip()
                if not agent:
                    return store
                return store_view.StoreView(store, agent[:64])

            @staticmethod
            def _tag_unexplored(result, target):
                if target is not store and isinstance(result, dict):
                    result.setdefault("unexplored_chunks", len(target.unexplored))
                return result

            def _seen(self, payload):
                if not isinstance(payload, dict):
                    return self._error(400, "Expected a JSON object")
                agent = str(payload.get("agent") or "").strip()[:64]
                if not agent:
                    return self._error(400, "Missing 'agent'")
                chunks = payload.get("chunks")
                if not isinstance(chunks, list):
                    return self._error(400, "Expected 'chunks': [[cx, cz], ...]")
                unloaded = payload.get("unloaded")
                if unloaded is not None and not isinstance(unloaded, list):
                    return self._error(400, "Expected 'unloaded': [[cx, cz], ...]")
                if not map_store.is_overworld(payload.get("dimension")):
                    return self._send(200, {"accepted": 0, "ignored": len(chunks) + len(unloaded or [])})
                replace = payload.get("replace") is True
                result = store.report_chunks(
                    agent, chunks, unloaded, payload.get("dimension"), replace=replace
                )
                body = {"accepted": result["loaded"]}
                if unloaded is not None:
                    body["unloaded"] = result["unloaded"]
                ignored = len(chunks) - result["loaded"]
                if ignored > 0:
                    body["ignored"] = ignored
                self._send(200, body)

            def _chunk_raw(self, payload):
                """Bot-fed chunk ingestion: name palettes + packed indices (CONTRACT.md)."""
                if not isinstance(payload, dict):
                    return self._error(400, "Expected a JSON object")
                agent = str(payload.get("agent") or "").strip()[:64]
                if not agent:
                    return self._error(400, "Missing 'agent'")
                if payload.get("format") != bot_chunk.FORMAT:
                    return self._error(400, f"Unsupported format {payload.get('format')!r}")
                chunks = payload.get("chunks")
                if not isinstance(chunks, list):
                    return self._error(400, "Expected 'chunks': [...]")
                if len(chunks) > bot_chunk.MAX_CHUNKS:
                    return self._error(
                        400, f"More than {bot_chunk.MAX_CHUNKS} chunks in one request"
                    )
                dimension = payload.get("dimension")
                if not map_store.is_overworld(dimension):
                    return self._send(200, {
                        "accepted": 0, "unchanged": 0, "rejected": 0,
                        "ignored": len(chunks),
                    })
                accepted = unchanged = rejected = 0
                decoded = api_self.decode_chunks(chunks)
                for item in decoded:
                    if item[0] == chunk_decode.LIMIT:
                        return self._error(400, item[1])
                for item in decoded:
                    if item[0] != chunk_decode.OK:
                        rejected += 1
                        continue
                    _, cx, cz, record = item
                    result = store.apply_bot_chunk(agent, dimension, record)
                    if result is True:
                        accepted += 1
                    elif result == "unchanged":
                        unchanged += 1
                    else:
                        rejected += 1
                self._send(200, {
                    "accepted": accepted, "unchanged": unchanged,
                    "rejected": rejected, "ignored": 0,
                })

            def _seen_stats(self, params):
                agent = (params.get("agent") or [""])[0].strip()
                if not agent:
                    raise ValueError("Missing required parameter 'agent'")
                self._send(200, store.seen_stats(agent[:64]))

            def _settlements(self, params):
                target = self._target(params)
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _size_param(params, "radius", 96, settlements.MAX_RADIUS,
                                     minimum=settlements.MIN_RADIUS)
                self._send(200, settlements.find_settlements(target, x, z, radius))

            def _landforms(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _size_param(params, "radius", landforms.DEFAULT_RADIUS,
                                     landforms.MAX_RADIUS, minimum=landforms.MIN_RADIUS)
                target = self._target(params)
                result = landforms.find_landforms(target, x, z, radius)
                self._send(200, result)

            def _route(self, params):
                agent = (params.get("agent") or [""])[0].strip()
                if not agent:
                    raise ValueError("Missing required parameter 'agent' (routes plan over what that agent has seen)")
                coords = {n: _int_param(params, n) for n in ("x", "z", "x2", "z2")}
                if any(abs(v) > MAX_COORD for v in coords.values()):
                    raise ValueError(f"Coordinates must be within +-{MAX_COORD}")
                seed = params.get("seed")
                rng = random.Random(_int_param(params, "seed")) if seed else None
                shore = (params.get("shore") or [None])[0]
                if shore is not None:
                    shore = str(shore).strip().lower()
                    if shore not in ("auto", "left", "right"):
                        raise ValueError("Parameter 'shore' must be auto, left or right")
                shore_from = None
                raw_from = (params.get("shore_from") or [None])[0]
                if raw_from is not None:
                    try:
                        shore_from = float(raw_from)
                    except (TypeError, ValueError):
                        raise ValueError("Parameter 'shore_from' must be a non-negative number")
                    if not math.isfinite(shore_from) or shore_from < 0:
                        raise ValueError("Parameter 'shore_from' must be a non-negative number")
                target = store_view.StoreView(store, agent[:64])
                log.info("route plan from (%s, %s) to (%s, %s) for %s shore=%s shore_from=%s",
                         coords["x"], coords["z"], coords["x2"], coords["z2"], agent[:64],
                         shore, shore_from)
                result = route.route(
                    target, coords["x"], coords["z"], coords["x2"], coords["z2"],
                    rng=rng, shore=shore, shore_from=shore_from,
                )
                log.info("route result %s/%s blocked_by=%s shore=%s wps=%s",
                         result.get("status"), result.get("reason"), result.get("blocked_by"),
                         (result.get("shore") or {}).get("side"),
                         len(result.get("waypoints") or []))
                self._send(200, result)

            def _survey(self, params):
                if not getattr(store.loader, "available", False):
                    return self._send(200, {"unavailable": "no save"})
                target = self._target(params)
                result = survey.survey(
                    target, _int_param(params, "x1"), _int_param(params, "z1"),
                    _int_param(params, "x2"), _int_param(params, "z2"),
                )
                self._send(200, result)

            def _overview(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _size_param(params, "radius", overview.DEFAULT_RADIUS,
                                     overview.MAX_RADIUS, minimum=overview.MIN_RADIUS)
                target = self._target(params)
                self._send(200, overview.build(target, x, z, radius))

            def _health(self):
                health = {
                    "status": "ok",
                    "service": "cartography",
                    "loader_available": bool(
                        getattr(store.loader, "available", False)
                    ),
                    "region_dir": str(getattr(store.loader, "region_dir", "") or ""),
                    "region_files": getattr(store.loader, "region_count", 0),
                }
                health.update(store.stats())
                self._send(200, health)

            def _perception(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                size_chunks = _size_param(
                    params, "size_chunks", DEFAULT_PERCEPTION_SIZE, MAX_PERCEPTION_SIZE
                )
                if size_chunks * size_chunks > MAX_PERCEPTION_CHUNKS:
                    raise ValueError(
                        f"Perception window too large ({size_chunks * size_chunks} chunks); "
                        f"max {MAX_PERCEPTION_CHUNKS}"
                    )
                materials = _bool_param(params, "materials", default=False)
                target = self._target(params)
                result = perception.build_perception(
                    target, x, z, size_chunks, materials=materials
                )
                self._send(200, self._tag_unexplored(result, target))

            def _topography(self, params):
                x1 = _int_param(params, "x1")
                z1 = _int_param(params, "z1")
                x2 = _int_param(params, "x2")
                z2 = _int_param(params, "z2")
                length = max(abs(x2 - x1), abs(z2 - z1)) + 1
                if length > MAX_LINE_LENGTH:
                    raise ValueError(
                        f"Line too long ({length} samples); max {MAX_LINE_LENGTH}"
                    )
                target = self._target(params)
                result = topography.heightmap_line(target, x1, z1, x2, z2)
                self._send(200, self._tag_unexplored(result, target))

            def _area_composition(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _radius_param(params)
                target = self._target(params)
                result = topography.area_summary(target, x, z, radius)
                self._send(200, self._tag_unexplored(result, target))

            def _shoreline(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _radius_param(params)
                target = self._target(params)
                result = topography.shoreline_polygon(target, x, z, radius)
                self._send(200, self._tag_unexplored(result, target))

            def _flat_areas(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                target = self._target(params)
                result = terrain_queries.flat_areas(
                    target, x, z,
                    radius=_size_param(params, "radius", 64, terrain_queries.MAX_FLAT_RADIUS),
                    width=_size_param(params, "width", 9, terrain_queries.MAX_FOOTPRINT),
                    depth=_size_param(params, "depth", 9, terrain_queries.MAX_FOOTPRINT),
                    max_delta=_size_param(params, "max_delta", 1, 64, minimum=0),
                )
                self._send(200, self._tag_unexplored(result, target))

            def _bridge_span(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                target = self._target(params)
                if params.get("x2") and params.get("z2"):
                    result = terrain_queries.bridge_span(
                        target, x, z, target=(_int_param(params, "x2"), _int_param(params, "z2"))
                    )
                else:
                    direction = (params.get("dir") or [""])[0].strip().lower()
                    result = terrain_queries.bridge_span(target, x, z, direction=direction)
                self._send(200, self._tag_unexplored(result, target))

            def _recall(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                target = self._target(params)
                result = topography.recall(target, x, z)
                if result is None:
                    if target is not store and target.unexplored:
                        return self._error(
                            404,
                            f"Chunk at ({x}, {z}) has not been explored by agent "
                            f"'{target.agent}' yet",
                        )
                    return self._error(404, f"No stored chunk at ({x}, {z})")
                self._send(200, result)

            def _chunk(self, params):
                cx = _int_param(params, "cx")
                cz = _int_param(params, "cz")
                target = self._target(params)
                data = target.get_or_load(cx, cz)
                if data is None:
                    if target is not store and target.unexplored:
                        return self._error(
                            404,
                            f"Chunk ({cx}, {cz}) has not been explored by agent "
                            f"'{target.agent}' yet",
                        )
                    return self._error(404, f"No chunk at ({cx}, {cz})")
                self._send(200, data)

            def _unavailable_dimension(self, dimension):
                self._send(
                    200,
                    {
                        "service": "cartography",
                        "unavailable": "dimension",
                        "dimension": map_store.normalize_dimension(dimension),
                    },
                )

            def _chunk_snapshot(self, payload):
                """POST /chunk_snapshot: replace whole-chunk surfaces (idempotent)."""
                if isinstance(payload, dict):
                    snaps = payload.get("snapshots")
                    default_dim = payload.get("dimension")
                else:
                    snaps, default_dim = payload, None
                if not isinstance(snaps, list):
                    return self._error(400, "Expected {\"snapshots\": [...]}")
                prepared = []
                for snap in snaps:
                    if isinstance(snap, dict) and "dimension" not in snap and default_dim:
                        snap = dict(snap, dimension=default_dim)
                    prepared.append(snap)
                accepted, rejected, ignored = store.apply_snapshots(prepared)
                self._send(
                    200,
                    {"accepted": accepted, "rejected": rejected, "ignored": ignored},
                )

            def _position(self, payload):
                if not isinstance(payload, dict):
                    return self._error(400, "Expected a JSON object")
                if not map_store.is_overworld(payload.get("dimension")):
                    return self._send(
                        200,
                        {
                            "accepted": False,
                            "unavailable": "dimension",
                            "dimension": map_store.normalize_dimension(payload.get("dimension")),
                        },
                    )
                try:
                    x = int(payload["x"])
                    z = int(payload["z"])
                except (KeyError, TypeError, ValueError):
                    return self._error(400, "Missing or invalid 'x'/'z'")
                size_chunks = payload.get("size_chunks", DEFAULT_PERCEPTION_SIZE)
                try:
                    size_chunks = int(size_chunks)
                except (TypeError, ValueError):
                    return self._error(400, "Parameter 'size_chunks' must be a number")
                if size_chunks < 1 or size_chunks > MAX_PERCEPTION_SIZE:
                    return self._error(
                        400,
                        f"Parameter 'size_chunks' must be between 1 and {MAX_PERCEPTION_SIZE}",
                    )
                if size_chunks * size_chunks > MAX_PERCEPTION_CHUNKS:
                    return self._error(
                        400,
                        f"Perception window too large ({size_chunks * size_chunks} chunks); "
                        f"max {MAX_PERCEPTION_CHUNKS}",
                    )
                queued = prefetcher.submit(x, z, size_chunks)
                self._send(200, {"accepted": True, "queued": queued})

            def _diff(self, payload):
                default_dim = payload.get("dimension") if isinstance(payload, dict) else None
                if isinstance(payload, dict) and "deltas" in payload:
                    deltas = payload["deltas"]
                elif isinstance(payload, dict):
                    deltas = [payload]
                else:
                    deltas = payload
                if not isinstance(deltas, list):
                    return self._error(400, "Expected a delta object or list")

                accepted = 0
                rejected = 0
                ignored = 0
                for delta in deltas:
                    if isinstance(delta, dict):
                        dim = delta.get("dimension", default_dim)
                        if not map_store.is_overworld(dim):
                            ignored += 1
                            continue
                    if isinstance(delta, dict) and store.apply_diff(delta):
                        accepted += 1
                    else:
                        rejected += 1
                result = {"accepted": accepted, "rejected": rejected}
                if ignored:
                    result["ignored"] = ignored
                self._send(200, result)

        return Handler

    def serve_forever(self):
        self.prefetcher.start()
        self.server = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        log.info("Topography API listening on http://%s:%s", self.host, self.port)
        self.server.serve_forever()

    def shutdown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.prefetcher.stop()
        with self._decode_pool_lock:
            pool, self._decode_pool = self._decode_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def _int_param(params, name):
    values = params.get(name)
    if not values:
        raise ValueError(f"Missing required parameter '{name}'")
    try:
        return int(float(values[0]))
    except (TypeError, ValueError):
        raise ValueError(f"Parameter '{name}' must be a number")


def _radius_param(params, default=32):
    values = params.get("radius")
    if not values:
        return default
    try:
        radius = int(float(values[0]))
    except (TypeError, ValueError):
        raise ValueError("Parameter 'radius' must be a number")
    if radius < 1 or radius > MAX_RADIUS:
        raise ValueError(f"Parameter 'radius' must be between 1 and {MAX_RADIUS}")
    return radius


def _size_param(params, name, default, maximum, minimum=1):
    values = params.get(name)
    if not values:
        return default
    try:
        size = int(float(values[0]))
    except (TypeError, ValueError):
        raise ValueError(f"Parameter '{name}' must be a number")
    if size < minimum or size > maximum:
        raise ValueError(f"Parameter '{name}' must be between {minimum} and {maximum}")
    return size


def _bool_param(params, name, default=False):
    values = params.get(name)
    if not values:
        return default
    raw = str(values[0]).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Parameter '{name}' must be 0 or 1")
