"""HTTP API queried by Mindcraft and fed by the Fabric diff plugin."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import map_store
import perception
import prefetch
import terrain_queries
import topography

log = logging.getLogger("topography.api")

MAX_RADIUS = 256
MAX_LINE_LENGTH = 4096
MAX_BODY_BYTES = 1_000_000
MAX_PERCEPTION_SIZE = 32
MAX_PERCEPTION_CHUNKS = 1024
DEFAULT_PERCEPTION_SIZE = 16


class TopographyAPI:
    def __init__(self, store, host: str = "127.0.0.1", port: int = 8090, prefetcher=None):
        self.store = store
        self.host = host
        self.port = port
        self.server: ThreadingHTTPServer | None = None
        self.prefetcher = prefetcher or prefetch.PrefetchWorker(store)

    def make_handler(self):
        store = self.store
        prefetcher = self.prefetcher

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
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _error(self, status: int, message: str):
                self._send(status, {"error": message})

            def do_OPTIONS(self):
                self._send(204, b"", "text/plain")

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                try:
                    if parsed.path in ("/", "/health"):
                        return self._health()
                    dimension = (params.get("dimension") or [None])[0]
                    if not map_store.is_overworld(dimension):
                        return self._unavailable_dimension(dimension)
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
                    return self._error(404, f"Unknown endpoint {parsed.path}")
                except ValueError as exc:
                    return self._error(400, str(exc))
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("Unhandled GET error")
                    return self._error(500, f"Internal error: {exc}")

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path not in ("/diff", "/position", "/chunk_snapshot"):
                    return self._error(404, f"Unknown endpoint {parsed.path}")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self._error(400, "Invalid Content-Length")
                if length <= 0:
                    return self._error(400, "Empty body")
                if length > MAX_BODY_BYTES:
                    return self._error(413, "Body too large")
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    return self._error(400, f"Invalid JSON: {exc}")
                if parsed.path == "/position":
                    return self._position(payload)
                if parsed.path == "/chunk_snapshot":
                    return self._chunk_snapshot(payload)
                return self._diff(payload)

            # ------------------------------------------------------- endpoints
            def _health(self):
                health = {
                    "status": "ok",
                    "service": "topography",
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
                result = perception.build_perception(
                    store, x, z, size_chunks, materials=materials
                )
                self._send(200, result)

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
                result = topography.heightmap_line(store, x1, z1, x2, z2)
                self._send(200, result)

            def _area_composition(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _radius_param(params)
                result = topography.area_summary(store, x, z, radius)
                self._send(200, result)

            def _shoreline(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                radius = _radius_param(params)
                result = topography.shoreline_polygon(store, x, z, radius)
                self._send(200, result)

            def _flat_areas(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                result = terrain_queries.flat_areas(
                    store, x, z,
                    radius=_size_param(params, "radius", 64, terrain_queries.MAX_FLAT_RADIUS),
                    width=_size_param(params, "width", 9, terrain_queries.MAX_FOOTPRINT),
                    depth=_size_param(params, "depth", 9, terrain_queries.MAX_FOOTPRINT),
                    max_delta=_size_param(params, "max_delta", 1, 64, minimum=0),
                )
                self._send(200, result)

            def _bridge_span(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                if params.get("x2") and params.get("z2"):
                    result = terrain_queries.bridge_span(
                        store, x, z, target=(_int_param(params, "x2"), _int_param(params, "z2"))
                    )
                else:
                    direction = (params.get("dir") or [""])[0].strip().lower()
                    result = terrain_queries.bridge_span(store, x, z, direction=direction)
                self._send(200, result)

            def _recall(self, params):
                x = _int_param(params, "x")
                z = _int_param(params, "z")
                result = topography.recall(store, x, z)
                if result is None:
                    return self._error(404, f"No stored chunk at ({x}, {z})")
                self._send(200, result)

            def _chunk(self, params):
                cx = _int_param(params, "cx")
                cz = _int_param(params, "cz")
                data = store.get_or_load(cx, cz)
                if data is None:
                    return self._error(404, f"No chunk at ({cx}, {cz})")
                self._send(200, data)

            def _unavailable_dimension(self, dimension):
                self._send(
                    200,
                    {
                        "service": "topography",
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
