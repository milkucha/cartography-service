"""End-to-end HTTP API tests, including the Java plugin diff contract."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import loader
import map_store
from api import TopographyAPI
from tests import helpers


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        world = helpers.build_coast_world(cls._tmp.name)
        cls.loader = loader.ChunkLoader(world)
        cls.store = map_store.MapStore(
            str(Path(cls._tmp.name) / "map_store.db"), cls.loader
        )
        cls.api = TopographyAPI(cls.store, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(400):
            if cls.api.server is not None:
                break
            time.sleep(0.005)
        if cls.api.server is None:
            raise RuntimeError("topography API did not start")
        cls.port = cls.api.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.thread.join(timeout=5)
        cls.store.stop()
        cls.loader.close()
        cls._tmp.cleanup()

    def _request(self, method, path, params=None, body=None):
        query = ""
        if params:
            from urllib.parse import urlencode

            query = "?" + urlencode(params)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path + query, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_health_reports_loader(self):
        status, payload = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["loader_available"])
        self.assertEqual(payload["region_files"], 1)
        self.assertIn("hot_chunks", payload)

    def test_topography_endpoint(self):
        status, payload = self._request(
            "GET", "/topography", {"x1": 10, "z1": 8, "x2": 22, "z2": 8}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["sample_count"], 13)
        self.assertEqual(payload["min_y"], 63)
        self.assertEqual(payload["max_y"], 66)
        self.assertEqual(len(payload["transitions"]), 1)
        self.assertEqual(payload["transitions"][0]["x"], 16)

    def test_area_composition_endpoint(self):
        status, payload = self._request(
            "GET", "/area_composition", {"x": 8, "z": 8, "radius": 6}
        )
        self.assertEqual(status, 200)
        self.assertGreater(payload["columns"], 0)
        self.assertEqual(payload["dominant_biome"], "minecraft:plains")
        self.assertIn("natural", payload["composition"])

    def test_shoreline_endpoint(self):
        status, payload = self._request(
            "GET", "/shoreline", {"x": 10, "z": 8, "radius": 8}
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["water_at_center"])
        self.assertGreater(payload["point_count"], 0)
        self.assertTrue(payload["segments"])

    def test_recall_endpoint(self):
        status, payload = self._request("GET", "/recall", {"x": 10, "z": 8})
        self.assertEqual(status, 200)
        self.assertEqual(payload["chunk"], {"cx": 0, "cz": 0})
        self.assertEqual(payload["column"]["material"], "minecraft:grass_block")

    def test_chunk_endpoint(self):
        status, payload = self._request("GET", "/chunk", {"cx": 1, "cz": 0})
        self.assertEqual(status, 200)
        self.assertEqual(set(payload["surface"]["materials"]), {"minecraft:water"})

    def test_diff_pipeline_places_and_breaks(self):
        place = {
            "deltas": [
                {
                    "cx": 0,
                    "cz": 0,
                    "x": 10,
                    "y": 70,
                    "z": 8,
                    "action": "place",
                    "block_name": "minecraft:oak_planks",
                }
            ]
        }
        status, payload = self._request("POST", "/diff", body=place)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"accepted": 1, "rejected": 0})

        status, payload = self._request(
            "GET", "/topography", {"x1": 10, "z1": 8, "x2": 10, "z2": 8}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["runs"][0]["y"], 70)
        self.assertEqual(payload["runs"][0]["material"], "minecraft:oak_planks")

        status, chunk = self._request("GET", "/chunk", {"cx": 0, "cz": 0})
        self.assertEqual(status, 200)
        index = (10 % 16) + (8 % 16) * 16
        self.assertEqual(chunk["surface"]["y"][index], 70)
        self.assertEqual(chunk["surface"]["materials"][index], "minecraft:oak_planks")

        broken = {
            "deltas": [
                {
                    "cx": 0,
                    "cz": 0,
                    "x": 10,
                    "y": 70,
                    "z": 8,
                    "action": "break",
                    "block_name": "minecraft:oak_planks",
                    "new_top_y": 65,
                    "new_top_block": "minecraft:grass_block",
                    "new_ground_y": 65,
                    "new_ground_block": "minecraft:grass_block",
                }
            ]
        }
        status, payload = self._request("POST", "/diff", body=broken)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"accepted": 1, "rejected": 0})

        status, chunk = self._request("GET", "/chunk", {"cx": 0, "cz": 0})
        self.assertEqual(status, 200)
        index = (10 % 16) + (8 % 16) * 16
        self.assertEqual(chunk["surface"]["y"][index], 65)
        self.assertEqual(chunk["surface"]["materials"][index], "minecraft:grass_block")

    def test_perception_endpoint(self):
        status, payload = self._request(
            "GET", "/perception", {"x": 8, "z": 8, "size_chunks": 4}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "topography")
        self.assertEqual(payload["resolution"], "lod")
        self.assertEqual(payload["center"], {"x": 8, "z": 8})
        self.assertEqual(payload["size_chunks"], 4)
        self.assertEqual(
            payload["window"], {"cx1": -2, "cz1": -2, "cx2": 1, "cz2": 1}
        )
        self.assertEqual(len(payload["lod"]), 1)
        self.assertEqual(payload["lod"][0]["cell_chunks"], 1)
        self.assertEqual(payload["lod"][0]["cells_per_side"], 4)
        self.assertEqual(len(payload["lod"][0]["heights"]), 16)
        self.assertEqual(payload["missing_chunks"], 12)
        self.assertFalse(payload["water"]["at_center"])
        self.assertEqual(payload["water"]["nearest"]["direction"], "east")
        self.assertNotIn("materials", payload["lod"][0])
        self.assertIn("window_version", payload)
        self.assertIn("terrain", payload)
        self.assertIn("highest", payload["terrain"])
        self.assertIn("highest_by_direction", payload["terrain"])
        self.assertIn("engineered_blocks", payload["composition"])

    def test_perception_default_size(self):
        status, payload = self._request("GET", "/perception", {"x": 8, "z": 8})
        self.assertEqual(status, 200)
        self.assertEqual(payload["size_chunks"], 16)
        self.assertEqual(len(payload["lod"]), 3)

    def test_perception_materials_flag(self):
        status, payload = self._request(
            "GET", "/perception", {"x": 8, "z": 8, "size_chunks": 4, "materials": 1}
        )
        self.assertEqual(status, 200)
        self.assertIn("materials", payload["lod"][0])
        self.assertEqual(len(payload["lod"][0]["materials"]), 16)
        self.assertEqual(payload["lod"][0]["materials"][10], "minecraft:grass_block")
        self.assertEqual(payload["lod"][0]["materials"][11], "minecraft:water")

    def test_perception_param_validation(self):
        status, payload = self._request("GET", "/perception", {"z": 0})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

        for params in (
            {"x": 0, "z": 0, "size_chunks": 0},
            {"x": 0, "z": 0, "size_chunks": 33},
            {"x": 0, "z": 0, "materials": 2},
            {"x": "nope", "z": 0},
        ):
            status, payload = self._request("GET", "/perception", params)
            self.assertEqual(status, 400, params)
            self.assertIn("error", payload)

    def test_position_prefetch_endpoint(self):
        status, payload = self._request(
            "POST", "/position", body={"x": 8, "z": 8, "size_chunks": 4}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["queued"], 16)

    def test_position_prefetch_validation(self):
        for body in (
            {},
            {"x": 0},
            {"x": 0, "z": 0, "size_chunks": 0},
            {"x": 0, "z": 0, "size_chunks": 33},
            {"x": "nope", "z": 0},
        ):
            status, payload = self._request("POST", "/position", body=body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)

    def test_diff_rejects_bad_payload(self):
        status, payload = self._request(
            "POST", "/diff", body={"deltas": [{"action": "place"}]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"accepted": 0, "rejected": 1})

        status, payload = self._request("POST", "/diff", body={"deltas": "nope"})
        self.assertEqual(status, 400)

    def test_bad_params_and_unknown_endpoints(self):
        status, payload = self._request("GET", "/topography", {"x1": 0})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

        status, payload = self._request(
            "GET", "/area_composition", {"x": 0, "z": 0, "radius": 9999}
        )
        self.assertEqual(status, 400)

        status, payload = self._request("GET", "/nope")
        self.assertEqual(status, 404)

        status, payload = self._request("POST", "/nope", body={})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
