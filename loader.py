"""Base world-state loader.

Reads the Minecraft save's Anvil region files directly and reduces each chunk
to the surface-only, token-efficient model described in TOPOGRAPHY_SPEC.md:

- topmost non-air block per 16x16 column (surface Y + material)
- topmost solid block per column (used for shoreline / land elevation)
- whether the column is water-covered
- block composition counts + strong indicators + dominant biome

Chunks are decoded lazily and cached by the map store; the full 60+ GB world
is never held in memory. Set TOPOGRAPHY_PRELOAD=1 to walk every region file
at startup (slow, mostly useful for small test worlds).
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from pathlib import Path

import composition
import mc_blocks
import palette
import region
import terrain_rules

log = logging.getLogger("topography.loader")

REGION_RE = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")

AIR_BLOCKS = set(mc_blocks.AIR) | {
    "minecraft:light",
    "minecraft:structure_void",
    "minecraft:moving_piston",
}
LIQUID_BLOCKS = set(mc_blocks.LIQUID)
# Aquatic plants make the topmost non-air block a plant even though the
# column is really water. Treat them as water for shoreline purposes.
WATER_TOP_BLOCKS = LIQUID_BLOCKS | {
    "minecraft:kelp",
    "minecraft:kelp_plant",
    "minecraft:seagrass",
    "minecraft:tall_seagrass",
    "minecraft:sea_pickle",
    "minecraft:lily_pad",
}
# Blocks that are not a walkable solid: liquids and vegetation. Only used to
# find the ground under water when computing ground_y.
NON_SOLID_BLOCKS = AIR_BLOCKS | LIQUID_BLOCKS | {
    "minecraft:kelp",
    "minecraft:kelp_plant",
    "minecraft:seagrass",
    "minecraft:tall_seagrass",
    "minecraft:sea_pickle",
    "minecraft:lily_pad",
    "minecraft:short_grass",
    "minecraft:grass",
    "minecraft:tall_grass",
    "minecraft:fern",
    "minecraft:large_fern",
    "minecraft:dead_bush",
    "minecraft:sugar_cane",
    "minecraft:bamboo",
    "minecraft:cactus",
    "minecraft:snow",
    "minecraft:vine",
    "minecraft:glow_lichen",
}
for _flower in (
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip",
    "orange_tulip", "white_tulip", "pink_tulip", "oxeye_daisy", "cornflower",
    "lily_of_the_valley", "wither_rose", "torchflower", "sunflower", "lilac",
    "rose_bush", "peony", "pink_petals", "spore_blossom",
):
    NON_SOLID_BLOCKS.add(f"minecraft:{_flower}")


def resolve_region_dir(world_path: str | Path) -> Path | None:
    """Find the overworld region/ directory under a save path.

    Handles both vanilla (world/<level>/region) and Paper/Spigot split
    layouts (world/<level>/region next to <level>_nether/). Accepts a path
    to the world root, a save root, or the level folder itself.
    """
    root = Path(world_path).expanduser()
    if not root.exists():
        return None
    if root.is_file():
        root = root.parent

    candidates = []

    def consider(folder: Path, depth: int):
        if not folder.is_dir():
            return
        name = folder.name.lower()
        if name.endswith("_nether") or name.endswith("_the_end"):
            return
        reg = folder / "region"
        if reg.is_dir():
            candidates.append((depth, str(folder), reg))

    consider(root, 0)
    if candidates:
        return sorted(candidates)[0][2]

    # BFS a few levels deep; world roots often wrap the actual level folder.
    frontier = [root]
    for depth in range(1, 4):
        next_frontier = []
        for folder in frontier:
            try:
                children = sorted(p for p in folder.iterdir() if p.is_dir())
            except OSError:
                continue
            for child in children:
                consider(child, depth)
                if depth < 3:
                    next_frontier.append(child)
        if candidates:
            return sorted(candidates)[0][2]
        frontier = next_frontier

    # Vanilla single-player: world/level/region was already covered. If a
    # path directly contains DIM-1/DIM1 but no region, keep looking below.
    return None


class ChunkLoader:
    def __init__(self, world_path: str | Path, region_cache_size: int = 8):
        self.region_dir = resolve_region_dir(world_path)
        self.region_cache_size = region_cache_size
        self._regions: OrderedDict[Path, region.RegionFile] = OrderedDict()
        self._index: dict[tuple[int, int], Path] = {}
        self._indexed = False
        self._index_mtime = None
        self._io_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.region_dir is not None and self.region_dir.is_dir()

    def close(self) -> None:
        """Close all cached region file handles."""
        with self._io_lock:
            for handle in self._regions.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._regions.clear()

    def index_regions(self, force: bool = False) -> int:
        """Map region coordinates to files, refreshing when the folder changes.

        A live world keeps generating region files while the service runs, so
        the index must not be frozen at startup. The folder mtime is cheap to
        check and changes when files are added or removed; content changes
        inside an existing region file do not require a re-index.
        """
        if not self.available:
            return 0
        try:
            mtime = self.region_dir.stat().st_mtime_ns
        except OSError:
            return len(self._index)
        if self._indexed and not force and mtime == self._index_mtime:
            return len(self._index)

        index: dict[tuple[int, int], Path] = {}
        for path in sorted(self.region_dir.glob("r.*.*.mca")):
            match = REGION_RE.match(path.name)
            if not match:
                continue
            index[(int(match.group(1)), int(match.group(2)))] = path
        self._index = index
        self._indexed = True
        self._index_mtime = mtime
        return len(self._index)

    @property
    def region_count(self) -> int:
        return self.index_regions()

    def _region_for(self, cx: int, cz: int):
        rx, rz = cx // 32, cz // 32
        path = self._index.get((rx, rz))
        if path is None:
            return None
        cached = self._regions.get(path)
        if cached is not None:
            self._regions.move_to_end(path)
            return cached
        try:
            handle = region.RegionFile(path)
        except OSError as exc:
            log.warning("Could not open region %s: %s", path, exc)
            return None
        self._regions[path] = handle
        while len(self._regions) > self.region_cache_size:
            _, old = self._regions.popitem(last=False)
            try:
                old.close()
            except Exception:
                pass
        return handle

    def has_region_for(self, cx: int, cz: int) -> bool:
        self.index_regions()
        return (cx // 32, cz // 32) in self._index

    def load_chunk(self, cx: int, cz: int):
        """Decode one chunk, returning the compact chunk record or None."""
        if not self.available:
            return None
        self.index_regions()
        handle = self._region_for(cx, cz)
        if handle is None:
            return None
        try:
            with self._io_lock:
                root = handle.read_chunk_nbt(cx % 32, cz % 32)
        except Exception as exc:
            log.warning("Failed to read chunk (%s, %s): %s", cx, cz, exc)
            return None
        if not root:
            return None

        try:
            surface = decode_surface(root)
        except Exception as exc:
            log.warning("Failed to decode surface for chunk (%s, %s): %s", cx, cz, exc)
            surface = _empty_surface()

        try:
            stats = composition.analyze_chunk(root, cx, cz) or {}
        except Exception as exc:
            log.warning("Failed to analyze composition for chunk (%s, %s): %s", cx, cz, exc)
            stats = {}

        return build_chunk_record(cx, cz, surface, stats, source="base")

    def iter_all_chunks(self):
        """Yield every chunk present in the indexed region files."""
        self.index_regions()
        for (rx, rz), path in sorted(self._index.items()):
            try:
                handle = region.RegionFile(path)
            except OSError:
                continue
            try:
                for local_x in range(32):
                    for local_z in range(32):
                        if not handle.has_chunk(local_x, local_z):
                            continue
                        cx = rx * 32 + local_x
                        cz = rz * 32 + local_z
                        record = self.load_chunk(cx, cz)
                        if record is not None:
                            yield record
            finally:
                try:
                    handle.close()
                except Exception:
                    pass


def _empty_surface():
    return {
        "y": [None] * 256,
        "materials": [None] * 256,
        "ground_y": [None] * 256,
        "ground_materials": [None] * 256,
        "water": [False] * 256,
        "terrain_y": [None] * 256,
        "terrain_materials": [None] * 256,
    }


def _section_names(section):
    block_states = section.get("block_states")
    if block_states is None:
        # Pre-1.16 fallback (camelCase palette / block states).
        pal = section.get("Palette")
        data = section.get("BlockStates")
        if pal is None:
            return None, None
        return [entry.get("Name") for entry in pal], data
    pal = block_states.get("palette") or []
    return [entry.get("Name") for entry in pal], block_states.get("data")


def decode_surface(root):
    """Return per-column surface/ground/terrain data for a chunk root compound.

    ``terrain_y`` / ``terrain_materials`` follow ``terrain_rules.is_ground``:
    the topmost block that is not air, liquid, vegetation or leaves (so
    canopies are skipped, and under water the bed is reported).
    """
    sections = root.get("sections") or []
    result = _empty_surface()
    surface_y = result["y"]
    surface_mat = result["materials"]
    ground_y = result["ground_y"]
    ground_mat = result["ground_materials"]
    terrain_y = result["terrain_y"]
    terrain_mat = result["terrain_materials"]
    water = result["water"]

    ordered = sorted(
        (s for s in sections if isinstance(s, dict) and "Y" in s),
        key=lambda s: s["Y"],
        reverse=True,
    )

    # Columns still missing a value for each of the three trackers.
    pend_surface = set(range(256))
    pend_ground = set(range(256))
    pend_terrain = set(range(256))
    for section in ordered:
        if not (pend_surface or pend_ground or pend_terrain):
            break
        names, _ = _section_names(section)
        if not names:
            continue

        section_y = section["Y"]
        base_y = section_y * 16

        if len(names) == 1:
            name = names[0]
            if name is None or name in AIR_BLOCKS:
                continue
            top_y = base_y + 15
            for index in pend_surface:
                surface_y[index] = top_y
                surface_mat[index] = name
            pend_surface = set()
            if name not in NON_SOLID_BLOCKS:
                for index in pend_ground:
                    ground_y[index] = top_y
                    ground_mat[index] = name
                pend_ground = set()
            if terrain_rules.is_ground(name):
                for index in pend_terrain:
                    terrain_y[index] = top_y
                    terrain_mat[index] = name
                pend_terrain = set()
            continue

        block_states = section.get("block_states")
        data = block_states.get("data") if block_states else section.get("BlockStates")
        bits = palette.bits_needed(len(names), minimum=4)
        if not data or bits > 16:
            continue
        indices = palette.unpack_indices(data, bits, 4096)
        if len(indices) < 4096:
            continue

        # Per palette entry: (is_air, counts_as_ground, is_terrain)
        flags = [
            (
                n is None or n in AIR_BLOCKS,
                n is not None and n not in NON_SOLID_BLOCKS,
                terrain_rules.is_ground(n),
            )
            for n in names
        ]
        for ly in range(15, -1, -1):
            if not (pend_surface or pend_ground or pend_terrain):
                break
            row = ly * 256
            y = base_y + ly
            for index in sorted(pend_surface | pend_ground | pend_terrain):
                pal_index = indices[row + index]
                is_air, is_solid, is_terrain = flags[pal_index]
                if is_air:
                    continue
                name = names[pal_index]
                if index in pend_surface:
                    surface_y[index] = y
                    surface_mat[index] = name
                    pend_surface.discard(index)
                if is_solid and index in pend_ground:
                    ground_y[index] = y
                    ground_mat[index] = name
                    pend_ground.discard(index)
                if is_terrain and index in pend_terrain:
                    terrain_y[index] = y
                    terrain_mat[index] = name
                    pend_terrain.discard(index)

    for index, material in enumerate(surface_mat):
        water[index] = material in WATER_TOP_BLOCKS

    return result


def build_chunk_record(cx, cz, surface, stats, source="base"):
    class_counts = {
        "natural": stats.get("decoded_natural", 0),
        "engineered": stats.get("decoded_engineered", 0),
        "unknown": stats.get("decoded_unknown", 0),
        "liquid": stats.get("decoded_liquid", 0),
        "air": stats.get("decoded_air", 0),
    }
    for key, count in (stats.get("uniform_volume") or {}).items():
        class_counts[key] = class_counts.get(key, 0) + count

    return {
        "cx": cx,
        "cz": cz,
        "surface": surface,
        "composition": {
            "counts": class_counts,
            "top_blocks": stats.get("top_blocks") or [],
            "engineered_blocks": stats.get("engineered_blocks") or [],
            "rail_columns": stats.get("rail_columns") or [],
            "rail_column_count": stats.get("rail_column_count", 0),
            "strong_indicators": stats.get("strong_indicator_types") or [],
            "strong_indicator_count": stats.get("strong_indicator_count", 0),
            "dominant_biome": stats.get("dominant_biome"),
            "artificiality_score": stats.get("artificiality_score", 0.0),
            "surface_min_y": stats.get("surface_min_y"),
        },
        "source": source,
    }
