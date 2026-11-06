"""Shared fixtures for the topography service test suite.

The helpers build real Anvil region files so the loader, paletted-container
decoder, map store, HTTP API and preload path can be exercised end to end
without a running Minecraft server.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

TAG_BYTE = 1
TAG_INT = 3
TAG_LONG = 4
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_LONG_ARRAY = 12

AIR = "minecraft:air"
STONE = "minecraft:stone"
DIRT = "minecraft:dirt"
GRASS = "minecraft:grass_block"
SAND = "minecraft:sand"
WATER = "minecraft:water"
LOG = "minecraft:oak_log"
LEAVES = "minecraft:oak_leaves"


# ---------------------------------------------------------------- NBT writer
def _named(tag_type: int, name: str, payload: bytes) -> bytes:
    raw = name.encode("utf-8")
    return bytes([tag_type]) + struct.pack(">H", len(raw)) + raw + payload


def string_payload(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def compound_payload(entries: list[bytes]) -> bytes:
    return b"".join(entries) + b"\x00"


def tag_compound(name: str, entries: list[bytes]) -> bytes:
    return _named(TAG_COMPOUND, name, compound_payload(entries))


def tag_byte(name: str, value: int) -> bytes:
    return _named(TAG_BYTE, name, struct.pack(">b", int(value)))


def tag_int(name: str, value: int) -> bytes:
    return _named(TAG_INT, name, struct.pack(">i", int(value)))


def tag_string(name: str, value: str) -> bytes:
    return _named(TAG_STRING, name, string_payload(value))


def tag_list(name: str, item_type: int, payloads: list[bytes]) -> bytes:
    payload = bytes([item_type]) + struct.pack(">i", len(payloads)) + b"".join(payloads)
    return _named(TAG_LIST, name, payload)


def tag_long_array(name: str, values: list[int]) -> bytes:
    return _named(
        TAG_LONG_ARRAY,
        name,
        struct.pack(">i", len(values)) + b"".join(struct.pack(">q", v) for v in values),
    )


def pack_indices(indices: list[int], bits: int) -> list[int]:
    """Pack palette indices the way Minecraft 1.16+ stores block_states."""
    per_long = 64 // bits
    mask = (1 << bits) - 1
    packed = []
    for start in range(0, len(indices), per_long):
        value = 0
        for slot, index in enumerate(indices[start:start + per_long]):
            value |= (index & mask) << (slot * bits)
        if value >= 1 << 63:
            value -= 1 << 64
        packed.append(value)
    return packed


# ------------------------------------------------------------- chunk builder
def build_section(section_y: int, block_at, biome: str = "minecraft:plains") -> bytes:
    names = set()
    for local_y in range(16):
        world_y = section_y * 16 + local_y
        for local_z in range(16):
            for local_x in range(16):
                names.add(block_at(world_y, local_x, local_z))

    non_air = sorted(name for name in names if name != AIR)
    if not non_air:
        palette = [AIR]
    elif len(non_air) == 1:
        palette = non_air
    else:
        palette = [AIR] + non_air
    entries = [tag_byte("Y", section_y)]

    if len(palette) == 1:
        block_states = tag_compound(
            "block_states",
            [
                tag_list(
                    "palette",
                    TAG_COMPOUND,
                    [compound_payload([tag_string("Name", palette[0])])],
                )
            ],
        )
    else:
        index_of = {name: index for index, name in enumerate(palette)}
        bits = max(4, (len(palette) - 1).bit_length())
        indices = [0] * 4096
        for local_y in range(16):
            world_y = section_y * 16 + local_y
            for local_z in range(16):
                for local_x in range(16):
                    index = (local_z * 16 + local_x) + local_y * 256
                    indices[index] = index_of[block_at(world_y, local_x, local_z)]
        block_states = tag_compound(
            "block_states",
            [
                tag_list(
                    "palette",
                    TAG_COMPOUND,
                    [
                        compound_payload([tag_string("Name", name)])
                        for name in palette
                    ],
                ),
                tag_long_array("data", pack_indices(indices, bits)),
            ],
        )

    biomes = tag_compound(
        "biomes",
        [tag_list("palette", TAG_STRING, [string_payload(biome)])],
    )
    entries.extend([block_states, biomes])
    return compound_payload(entries)


def build_chunk_root(
    cx: int,
    cz: int,
    ground_fn,
    surface_fn=None,
    water_level_fn=None,
    biome: str = "minecraft:plains",
    status: str | None = "minecraft:full",
    extras_fn=None,
) -> bytes:
    """Encode one chunk as a root NBT compound.

    ``status`` is written as the NBT ``Status`` tag (``None`` omits it).

    ``ground_fn(x, z)`` returns the solid ground height (or None for an empty
    column), ``surface_fn(x, z)`` the surface block name, and
    ``water_level_fn(x, z)`` the water surface height (or None for dry land).
    ``extras_fn(x, z)`` optionally returns ``{y: block_name}`` of blocks placed
    on top of the column (tree trunks, leaves, buildings).
    """
    surface_fn = surface_fn or (lambda x, z: GRASS)
    water_level_fn = water_level_fn or (lambda x, z: None)

    highest = 0
    for local_z in range(16):
        for local_x in range(16):
            x, z = cx * 16 + local_x, cz * 16 + local_z
            ground = ground_fn(x, z)
            if ground is None:
                continue
            level = water_level_fn(x, z)
            highest = max(highest, ground, level if level is not None else ground)
            if extras_fn is not None:
                highest = max([highest, *(extras_fn(x, z) or {})])

    def block_at(world_y, local_x, local_z):
        x, z = cx * 16 + local_x, cz * 16 + local_z
        ground = ground_fn(x, z)
        if ground is None:
            return AIR
        if extras_fn is not None:
            extra = (extras_fn(x, z) or {}).get(world_y)
            if extra is not None:
                return extra
        level = water_level_fn(x, z)
        top = level if level is not None and level > ground else ground
        if world_y > top:
            return AIR
        if level is not None and ground < world_y <= level:
            return WATER
        if world_y == ground:
            return surface_fn(x, z)
        if world_y >= ground - 3:
            return DIRT
        return STONE

    section_count = highest // 16 + 1
    sections = [
        build_section(section_y, block_at, biome=biome)
        for section_y in range(section_count)
    ]
    return tag_compound(
        "",
        [
            tag_int("xPos", cx),
            tag_int("zPos", cz),
            *([tag_string("Status", status)] if status else []),
            tag_list("sections", TAG_COMPOUND, sections),
        ],
    )


def write_region(path, chunks: dict[tuple[int, int], bytes], timestamps=None) -> Path:
    """Write chunks keyed by local (x, z) into one .mca region file.

    ``timestamps`` optionally maps local (x, z) to the header save time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    header = bytearray(8192)
    body = bytearray()
    next_sector = 2

    for (local_x, local_z), root in chunks.items():
        compressed = zlib.compress(root, 6)
        payload = struct.pack(">I", len(compressed) + 1) + b"\x02" + compressed
        sectors = (len(payload) + 4095) // 4096
        padded = payload.ljust(sectors * 4096, b"\x00")

        index = (local_x % 32) + (local_z % 32) * 32
        header[index * 4:index * 4 + 3] = next_sector.to_bytes(3, "big")
        header[index * 4 + 3] = sectors
        stamp = (timestamps or {}).get((local_x, local_z))
        if stamp is not None:
            header[4096 + index * 4:4096 + index * 4 + 4] = int(stamp).to_bytes(4, "big")

        body.extend(padded)
        next_sector += sectors

    path.write_bytes(bytes(header) + bytes(body))
    return path


# ----------------------------------------------------------- coastal fixture
def coast_ground(x: int, z: int) -> int:
    if x < 16:
        return 64 + ((x * 7 + z * 3) % 3)
    return 60


def coast_surface(x: int, z: int) -> str:
    return GRASS if x < 16 else SAND


def coast_water_level(x: int, z: int):
    return None if x < 16 else 63


def build_coast_world(root) -> Path:
    """Create ``<root>/world/region/r.0.0.mca`` with a 2x2-chunk coastline."""
    region = Path(root) / "world" / "region"
    chunks = {}
    for cx in (0, 1):
        for cz in (0, 1):
            chunks[(cx, cz)] = build_chunk_root(
                cx, cz, coast_ground, coast_surface, coast_water_level
            )
    write_region(region / "r.0.0.mca", chunks)
    return Path(root) / "world"


# -------------------------------------------------------------- fake loader
def make_record(
    cx: int,
    cz: int,
    height: int = 64,
    water_level=None,
    surface: str = GRASS,
    ground_material: str = GRASS,
    biome: str = "minecraft:plains",
    terrain_height=None,
):
    """Build a compact chunk record with the same shape as loader output."""
    water = water_level is not None and water_level > height
    top_y = water_level if water else height
    top_material = WATER if water else surface

    return {
        "cx": cx,
        "cz": cz,
        "surface": {
            "y": [top_y] * 256,
            "materials": [top_material] * 256,
            "ground_y": [height] * 256,
            "ground_materials": [ground_material] * 256,
            "water": [water] * 256,
            "terrain_y": [height if terrain_height is None else terrain_height] * 256,
            "terrain_materials": [ground_material] * 256,
        },
        "composition": {
            "counts": {
                "natural": 4096,
                "engineered": 0,
                "unknown": 0,
                "liquid": 4096 if water else 0,
                "air": 0,
            },
            "top_blocks": [[top_material, 256]],
            "strong_indicators": [],
            "strong_indicator_count": 0,
            "dominant_biome": biome,
            "artificiality_score": 0.0,
            "surface_min_y": height,
        },
        "source": "base",
    }


class FakeLoader:
    """Minimal stand-in for loader.ChunkLoader used by store tests."""

    available = True
    region_dir = "fake"
    region_count = 0

    def __init__(self, records=None, timestamps=None, world_id=None):
        self.records = records or {}
        # Optional: only loaders given ``timestamps`` expose chunk_timestamp.
        self.timestamps = timestamps
        self._world_id = world_id
        if timestamps is None:
            self.chunk_timestamp = None  # type: ignore[assignment]
        if world_id is None:
            self.world_id = None  # type: ignore[assignment]

    def load_chunk(self, cx, cz):
        record = self.records.get((cx, cz))
        if record is not None and self.timestamps is not None:
            record = dict(record)
            record["save_ts"] = self.timestamps.get((cx, cz))
        return record

    def chunk_timestamp(self, cx, cz):
        return self.timestamps.get((cx, cz))

    def world_id(self):
        return self._world_id


# ------------------------------------------------- structure (settlement) fixtures
FLAT_GROUND = 20
PLANKS = "minecraft:oak_planks"
STAIRS = "minecraft:oak_stairs"
COBBLE = "minecraft:cobblestone"
PATH = "minecraft:dirt_path"


def house_extras(x0, z0, w, d, h=3, ground=FLAT_GROUND, door=True, extras=None, wall=PLANKS, roof=STAIRS):
    """Return ``{(x, z): {y: block}}`` for a walled house with a flat stair roof."""
    out = {}
    for x in range(x0, x0 + w):
        for z in range(z0, z0 + d):
            col = {}
            edge = x in (x0, x0 + w - 1) or z in (z0, z0 + d - 1)
            if edge:
                for y in range(ground + 1, ground + h + 1):
                    col[y] = wall
            col[ground + h + 1] = roof
            out[(x, z)] = col
    if door:
        out[(x0 + w // 2, z0)].update({ground + 1: "minecraft:oak_door", ground + 2: "minecraft:oak_door"})
        out[(x0, z0 + d // 2)][ground + 2] = "minecraft:glass_pane"
    for (ex, ez), blocks in (extras or {}).items():
        out[(ex, ez)].update(blocks)
    return out


def tree_extras(x, z, ground=FLAT_GROUND):
    out = {(x, z): {y: LOG for y in range(ground + 1, ground + 6)}}
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            col = out.setdefault((x + dx, z + dz), {})
            for y in (ground + 4, ground + 5):
                col.setdefault(y, LEAVES)
    for dx in range(-1, 2):
        for dz in range(-1, 2):
            out[(x + dx, z + dz)].setdefault(ground + 6, LEAVES)
    return out


def build_structure_world(root, chunk_boxes, extras, ground=FLAT_GROUND, surface=None, water=None):
    """Flat world (single region r.0.0) with the given per-column ``extras``.

    ``chunk_boxes`` is a list of (cx1, cz1, cx2, cz2); ``extras`` maps (x, z) to
    ``{y: block}``; ``surface``/``water`` map (x, z) to a surface block / water level.
    """
    surface = surface or {}
    water = water or {}
    region = Path(root) / "world" / "region"
    chunks = {}
    for (cx1, cz1, cx2, cz2) in chunk_boxes:
        for cx in range(cx1, cx2 + 1):
            for cz in range(cz1, cz2 + 1):
                chunks[(cx, cz)] = build_chunk_root(
                    cx, cz,
                    lambda x, z: ground - 2 if (x, z) in water else ground,
                    lambda x, z: surface.get((x, z), GRASS),
                    lambda x, z: water.get((x, z)),
                    extras_fn=lambda x, z: extras.get((x, z)),
                )
    write_region(region / "r.0.0.mca", chunks)
    return Path(root) / "world"
