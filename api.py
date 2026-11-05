"""HTTP API queried by Mindcraft and fed by the Fabric diff plugin."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import topography

log = logging.getLogger("topography.api")

MAX_RADIUS = 256
MAX_LINE_LENGTH = 4096
MAX_BODY_BYTES = 1_000_000


class TopographyAPI:
    def __init__(self, store, host: str = "127.0.0.1", port: int = 8090):
        self.store = store
        self.host = host
        self.port = port
        self.server: ThreadingHTTPServer | None = None

    def make_handler(self):
        store = self.store

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
                    if parsed.path == "/topography":
                        return self._topography(params)
                    if parsed.path == "/area_composition":
                        return self._area_composition(params)
                    if parsed.path == "/shoreline":
                        return self._shoreline(params)
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
                if parsed.path != "/diff":
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

            def _diff(self, payload):
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
                for delta in deltas:
                    if isinstance(delta, dict) and store.apply_diff(delta):
                        accepted += 1
                    else:
                        rejected += 1
                self._send(200, {"accepted": accepted, "rejected": rejected})

        return Handler

    def serve_forever(self):
        self.server = ThreadingHTTPServer((self.host, self.port), self.make_handler())
        log.info("Topography API listening on http://%s:%s", self.host, self.port)
        self.server.serve_forever()

    def shutdown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


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
