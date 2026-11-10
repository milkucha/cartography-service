"""Bot-fed chunk ingestion: payload conversion, parity, store effects, HTTP, flags."""

from __future__ import annotations

import base64
import gzip
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import bot_chunk
import composition
import loader
import main
import map_store
import mc_blocks
import palette
import store_view
from api import TopographyAPI
from tests import helpers


def _iter_section(root):
    for section in root["sections"]:
        block_states = section["block_states"]
        names = [entry["Name"] for entry in block_states["palette"]]
        bits = palette.bits_needed(len(names), minimum=4)
        data = block_states.get("data") or []
        indices = palette.unpack_indices(data, bits, 4096) if len(names) > 1 else [0] * 4096
        yield section, names, indices


def _world_surface(root):
    """Independent WORLD_SURFACE heightmap (topmost non-air Y + 1, minus min_y)."""
    sections = root["sections"]
    min_y = min(section["Y"] for section in sections) * 16
    height = len(sections) * 16
    tops = [0] * 256
    for section, names, indices in _iter_section(root):
        for local_y in range(16):
            row = local_y * 256
            world_y = section["Y"] * 16 + local_y
            for column in range(256):
                if names[indices[row + column]] not in mc_blocks.AIR:
                    tops[column] = world_y + 1 - min_y
    bits = palette.bits_needed(height + 1, minimum=1)
    return palette.pack_indices(tops, bits)


def _with_heightmap(root, key="MOTION_BLOCKING"):
    copy = dict(root)
    copy["Heightmaps"] = {key: _world_surface(root)}
    return copy


def _biome_payload(root):
    out = []
    for section in root["sections"]:
        biomes = section.get("biomes")
        if not biomes:
            continue
        names = list(biomes["palette"])
        item = {"y": section["Y"], "palette": names}
        if len(names) > 1:
            bits = palette.bits_needed(len(names))
            indices = palette.unpack_indices(biomes.get("data") or [], bits, 64)
            item["enc"] = "packed"
            item["bits"] = bits
            item["data"] = _packed_b64(palette.pack_indices(indices, bits))
        out.append(item)
    return out


def _packed_b64(longs):
    raw = bytearray()
    for value in longs:
        raw += (value & 0xFFFFFFFF).to_bytes(4, "little")
        raw += ((value >> 32) & 0xFFFFFFFF).to_bytes(4, "little")
    return base64.b64encode(bytes(raw)).decode("ascii")


def _u16_b64(indices):
    raw = b"".join(int(value).to_bytes(2, "little") for value in indices)
    return base64.b64encode(raw).decode("ascii")


def _payload(root, cx, cz, *, omit_air=False, enc="packed", min_y=None, height=None, biomes=True):
    sections = root["sections"]
    first = min(section["Y"] for section in sections)
    if min_y is None:
        min_y = first * 16
    if height is None:
        height = len(sections) * 16
    out = []
    for section, names, indices in _iter_section(root):
        if omit_air and all(names[index] in mc_blocks.AIR for index in indices):
            continue
        item = {"y": section["Y"], "palette": names}
        if len(names) > 1:
            if enc == "u16":
                item["enc"] = "u16"
                item["data"] = _u16_b64(indices)
            else:
                item["enc"] = "packed"
                item["bits"] = palette.bits_needed(len(names), minimum=4)
                item["data"] = _packed_b64(palette.pack_indices(indices, item["bits"]))
        out.append(item)
    chunk = {"cx": cx, "cz": cz, "min_y": min_y, "height": height, "sections": out}
    if biomes:
        chunk["biomes"] = _biome_payload(root)
    return chunk


def _record_for(root, cx, cz):
    surface = loader.decode_surface(root)
    stats = composition.analyze_chunk(root, cx, cz) or {}
    return loader.build_chunk_record(cx, cz, surface, stats)


class PackIndicesTests(unittest.TestCase):
    def test_roundtrip_non_spanning(self):
        for bits in (1, 2, 4, 5, 8, 12, 16):
            mask = (1 << bits) - 1
            indices = [(i * 37) & mask for i in range(4096)]
            packed = palette.pack_indices(indices, bits)
            self.assertEqual(palette.unpack_indices(packed, bits, 4096), indices)

    def test_zero_bits(self):
        self.assertEqual(palette.pack_indices([0, 0], 0), [])


class ToRootParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        world = helpers.build_coast_world(cls._tmp.name)
        cls.loader = loader.ChunkLoader(world)

    @classmethod
    def tearDownClass(cls):
        cls.loader.close()
        cls._tmp.cleanup()

    def _roundtrip(self, cx, cz, **kwargs):
        root = self.loader.read_chunk_root(cx, cz)
        self.assertIsNotNone(root)
        reference = _with_heightmap(root)
        payload = _payload(root, cx, cz, **kwargs)
        got_cx, got_cz, rebuilt = bot_chunk.to_root(payload)
        self.assertEqual((got_cx, got_cz), (cx, cz))
        expected = _record_for(reference, cx, cz)
        actual = _record_for(rebuilt, cx, cz)
        self.assertEqual(actual["surface"], expected["surface"])
        self.assertEqual(actual["composition"], expected["composition"])

    def test_packed_payload_matches_loader(self):
        self._roundtrip(0, 0)

    def test_u16_payload_matches_loader(self):
        self._roundtrip(1, 0, enc="u16")

    def test_omitted_air_sections_are_refilled(self):
        root = self.loader.read_chunk_root(0, 1)
        self.assertIsNotNone(root)
        padded_root = dict(root)
        # A real 1.20 chunk spans 24 sections from y=-64; the fixture only has 0..4.
        sections = list(root["sections"])
        existing = {section["Y"] for section in sections}
        for y in range(-4, 20):
            if y not in existing:
                sections.append({
                    "Y": y,
                    "block_states": {"palette": [{"Name": "minecraft:air"}]},
                })
        padded_root["sections"] = sorted(sections, key=lambda s: s["Y"])
        payload_source = dict(root)
        payload_source["sections"] = root["sections"]
        payload = _payload(payload_source, 0, 1, omit_air=True, min_y=-64, height=384)
        _, _, rebuilt = bot_chunk.to_root(payload)
        self.assertEqual(len(rebuilt["sections"]), 24)
        expected = _record_for(_with_heightmap(padded_root), 0, 1)
        actual = _record_for(rebuilt, 0, 1)
        self.assertEqual(actual["surface"], expected["surface"])
        self.assertEqual(actual["composition"], expected["composition"])

    def test_payload_limits(self):
        root = self.loader.read_chunk_root(0, 0)
        good = _payload(root, 0, 0)
        bad = json.loads(json.dumps(good))
        with_data = next(i for i, section in enumerate(bad["sections"]) if "data" in section)
        bad["sections"][with_data]["bits"] = 3  # data no longer matches
        with self.assertRaises(bot_chunk.PayloadError):
            bot_chunk.to_root(bad)
        outside = json.loads(json.dumps(good))
        outside["sections"][0]["y"] = 99
        with self.assertRaises(bot_chunk.PayloadError):
            bot_chunk.to_root(outside)


class StoreBotChunkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = map_store.MapStore(
            str(Path(self._tmp.name) / "map.db"), helpers.FakeLoader({})
        )

    def tearDown(self):
        self.store.stop()
        self._tmp.cleanup()

    def _record(self, cx=0, cz=0, height=64):
        return helpers.make_record(cx, cz, height=height)

    def test_apply_marks_seen_and_loaded(self):
        record = self._record()
        self.assertTrue(self.store.apply_bot_chunk("Topo", "minecraft:overworld", record))
        stats = self.store.seen_stats("Topo")
        self.assertEqual(stats["chunks"], 1)
        view = store_view.StoreView(self.store, "Topo")
        self.assertIsNotNone(view.get_chunk(0, 0))

    def test_unchanged_reforward_costs_nothing(self):
        record = self._record()
        self.assertTrue(self.store.apply_bot_chunk("Topo", "minecraft:overworld", record))
        generation = self.store.generation
        rev = self.store.known_rev((0, 0))
        self.assertEqual(
            self.store.apply_bot_chunk("Topo", "minecraft:overworld", record),
            "unchanged",
        )
        self.assertEqual(self.store.generation, generation)
        self.assertEqual(self.store.known_rev((0, 0)), rev)

    def test_change_bumps_revision_and_keeps_tier(self):
        record = self._record()
        self.assertTrue(self.store.apply_bot_chunk("Topo", "minecraft:overworld", record))
        rev = self.store.known_rev((0, 0))
        self.assertNotIn((0, 0), self.store._hot)
        changed = self._record(height=70)
        self.assertTrue(self.store.apply_bot_chunk("Topo", "minecraft:overworld", changed))
        self.assertNotEqual(self.store.known_rev((0, 0)), rev)
        self.assertNotIn((0, 0), self.store._hot)
        self.assertEqual(self.store.get_chunk(0, 0)["surface"]["ground_y"][0], 70)

    def test_non_overworld_is_ignored(self):
        self.assertEqual(
            self.store.apply_bot_chunk("Topo", "minecraft:the_nether", self._record()),
            "ignored",
        )
        self.assertEqual(self.store.seen_stats("Topo")["chunks"], 0)

    def test_invalid_record_rejected(self):
        self.assertFalse(self.store.apply_bot_chunk("Topo", "minecraft:overworld", {"cx": 0}))


class ChunkRawApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        world = helpers.build_coast_world(cls._tmp.name)
        cls.loader = loader.ChunkLoader(world)
        cls.store = map_store.MapStore(str(Path(cls._tmp.name) / "map.db"), cls.loader)
        cls.api = TopographyAPI(cls.store, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(400):
            if cls.api.server is not None:
                break
            time.sleep(0.005)
        cls.base = f"http://127.0.0.1:{cls.api.server.server_address[1]}"
        cls.root = cls.loader.read_chunk_root(0, 0)

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.thread.join(timeout=5)
        cls.store.stop()
        cls.loader.close()
        cls._tmp.cleanup()

    def _post(self, payload, headers=None, gzipped=False):
        raw = json.dumps(payload).encode("utf-8")
        all_headers = {"Content-Type": "application/json"}
        if gzipped:
            raw = gzip.compress(raw)
            all_headers["Content-Encoding"] = "gzip"
        all_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + "/chunk_raw", data=raw, headers=all_headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def _body(self, chunks):
        return {
            "agent": "Topo",
            "dimension": "minecraft:overworld",
            "format": 1,
            "chunks": chunks,
        }

    def test_accept_then_unchanged_then_gzip(self):
        chunk = _payload(self.root, 0, 0)
        status, payload = self._post(self._body([chunk]))
        self.assertEqual(status, 200)
        self.assertEqual(payload["accepted"], 1)
        status, payload = self._post(self._body([chunk]))
        self.assertEqual(status, 200)
        self.assertEqual(payload["unchanged"], 1)
        status, payload = self._post(self._body([_payload(self.root, 5, 5)]), gzipped=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["accepted"], 1)

    def test_limits_and_bad_format(self):
        body = self._body([])
        body["format"] = 2
        self.assertEqual(self._post(body)[0], 400)
        body = self._body([{} for _ in range(17)])
        self.assertEqual(self._post(body)[0], 400)
        body = self._body([{"cx": 0}])
        status, payload = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["rejected"], 1)
        # a documented limit is a request error (400), not a per-chunk rejection
        bad = _payload(self.root, 0, 0)
        with_data = next(i for i, section in enumerate(bad["sections"]) if "data" in section)
        bad["sections"][with_data]["data"] = "AAAA"
        self.assertEqual(self._post(self._body([bad]))[0], 400)

    def test_non_overworld_ignored(self):
        body = self._body([_payload(self.root, 0, 0)])
        body["dimension"] = "minecraft:the_nether"
        status, payload = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["ignored"], 1)

    def test_missing_agent_rejected(self):
        body = self._body([_payload(self.root, 0, 0)])
        body.pop("agent")
        self.assertEqual(self._post(body)[0], 400)


class TokenTests(unittest.TestCase):
    def test_token_required_and_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunk_loader = loader.ChunkLoader(Path(tmp) / "missing")
            store = map_store.MapStore(str(Path(tmp) / "map.db"), chunk_loader)
            api = TopographyAPI(store, "127.0.0.1", 0, token="s3cret")
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            try:
                for _ in range(400):
                    if api.server is not None:
                        break
                    time.sleep(0.005)
                base = f"http://127.0.0.1:{api.server.server_address[1]}"
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + "/health", timeout=10)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()
                request = urllib.request.Request(
                    base + "/health", headers={"Authorization": "Bearer s3cret"}
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
            finally:
                api.shutdown()
                thread.join(timeout=5)
                store.stop()
                chunk_loader.close()


class SurveyAndFlagTests(unittest.TestCase):
    def test_survey_without_a_save_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunk_loader = loader.ChunkLoader(Path(tmp) / "missing")
            store = map_store.MapStore(str(Path(tmp) / "map.db"), chunk_loader)
            api = TopographyAPI(store, "127.0.0.1", 0)
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            try:
                for _ in range(400):
                    if api.server is not None:
                        break
                    time.sleep(0.005)
                base = f"http://127.0.0.1:{api.server.server_address[1]}"
                with urllib.request.urlopen(
                    base + "/survey?x1=0&z1=0&x2=15&z2=15", timeout=10
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload, {"unavailable": "no save"})
            finally:
                api.shutdown()
                thread.join(timeout=5)
                store.stop()
                chunk_loader.close()

    def test_parser_name_and_token(self):
        args = main.build_parser().parse_args(["--name", "my world", "--token", "x"])
        self.assertEqual(args.name, "my world")
        self.assertEqual(args.token, "x")
        self.assertTrue(main.named_db_path("my world").endswith("my_world.db"))
        self.assertTrue(main.named_db_path("my world").split("\\")[-2] == "maps"
                        or main.named_db_path("my world").split("/")[-2] == "maps")

    def test_loopback_detection(self):
        self.assertTrue(main.is_loopback("127.0.0.1"))
        self.assertTrue(main.is_loopback("::1"))
        self.assertTrue(main.is_loopback("localhost"))
        self.assertFalse(main.is_loopback("0.0.0.0"))
        self.assertFalse(main.is_loopback("192.168.1.10"))

    def test_refuses_non_loopback_without_token(self):
        self.assertEqual(main.main(["--host", "0.0.0.0"]), 2)


if __name__ == "__main__":
    unittest.main()
