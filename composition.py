"""Per-chunk block-composition analysis for building/city detection.

Copied from the Archeology tools. Uniform sections (a whole 16x16x16 section
that's one block, e.g. solid stone underground or empty sky) are handled via
a cheap O(1) shortcut and kept OUT of the artificiality score on purpose:
they're overwhelmingly deep terrain or empty air, and including them would
drown out the signal from sections that actually have variety. Only
non-uniform sections (real terrain variation or construction) are fully
decoded and scored.
"""

from collections import Counter

import mc_blocks
import palette

TOP_BLOCKS_LIMIT = 8

# How far below the chunk's lowest surface height a section can start and
# still be treated as "possibly a basement/foundation" rather than deep
# mining/cave activity.
SURFACE_MARGIN_BELOW = 32


def _decode_heightmap(chunk_root, world_bottom_y, world_height):
    heightmaps = chunk_root.get("Heightmaps") or {}
    data = heightmaps.get("MOTION_BLOCKING") or heightmaps.get("WORLD_SURFACE")
    if not data:
        return None
    # Heightmap entries encode 0..world_height inclusive, so that's
    # world_height + 1 distinct values to represent.
    bits = palette.bits_needed(world_height + 1, minimum=1)
    heights = palette.unpack_indices(data, bits, 256)
    return [h + world_bottom_y for h in heights]


def _decode_section_blocks(section, uniform_volume, decoded_counts, warnings, depth_cutoff_y,
                            cx, cz, tracked_positions):
    if depth_cutoff_y is not None:
        section_top = section["Y"] * 16 + 15
        if section_top < depth_cutoff_y:
            return

    block_states = section.get("block_states")
    if not block_states:
        return
    pal = block_states.get("palette") or []
    if not pal:
        return

    if len(pal) == 1:
        name = pal[0]["Name"]
        uniform_volume[mc_blocks.classify_block(name)] += 4096
        return

    bits = palette.bits_needed(len(pal), minimum=4)
    if bits > 8:
        warnings["unsupported_sections"] = warnings.get("unsupported_sections", 0) + 1
        return

    data = block_states.get("data") or []
    indices = palette.unpack_indices(data, bits, 4096)
    names = [p["Name"] for p in pal]
    for idx, count in Counter(indices).items():
        decoded_counts[names[idx]] += count

    tracked_indices = {i for i, name in enumerate(names) if name in mc_blocks.LINE_TRACKED_BLOCKS}
    if tracked_indices:
        section_y = section["Y"]
        for local_index, idx in enumerate(indices):
            if idx in tracked_indices:
                lx = local_index % 16
                lz = (local_index // 16) % 16
                ly = local_index // 256
                tracked_positions.append(
                    (cx * 16 + lx, section_y * 16 + ly, cz * 16 + lz, names[idx])
                )


def _decode_section_biome(section, biome_counts):
    biomes = section.get("biomes")
    if not biomes:
        return
    pal = biomes.get("palette") or []
    if not pal:
        return

    if len(pal) == 1:
        biome_counts[pal[0]] += 64
        return

    bits = palette.bits_needed(len(pal))
    data = biomes.get("data") or []
    indices = palette.unpack_indices(data, bits, 64)
    for idx, count in Counter(indices).items():
        biome_counts[pal[idx]] += count


def analyze_chunk(chunk_root, cx, cz):
    """Returns a stats dict for one chunk, or None if it has no sections."""
    sections = chunk_root.get("sections")
    if not sections:
        return None

    world_bottom_y = min(s["Y"] for s in sections) * 16
    world_height = len(sections) * 16
    heights = _decode_heightmap(chunk_root, world_bottom_y, world_height)
    surface_min = min(heights) if heights else None
    depth_cutoff_y = (surface_min - SURFACE_MARGIN_BELOW) if surface_min is not None else None

    uniform_volume = Counter()
    decoded_counts = Counter()
    biome_counts = Counter()
    warnings = {}
    tracked_positions = []

    for section in sections:
        _decode_section_blocks(section, uniform_volume, decoded_counts, warnings, depth_cutoff_y,
                                cx, cz, tracked_positions)
        _decode_section_biome(section, biome_counts)

    class_counts = Counter()
    strong_indicators = Counter()
    decay_indicators = Counter()
    for name, count in decoded_counts.items():
        cls = mc_blocks.classify_block(name)
        class_counts[cls] += count
        if mc_blocks.is_strong_indicator(name):
            strong_indicators[name] += count
        if mc_blocks.is_decay_indicator(name):
            decay_indicators[name] += count

    natural = class_counts.get("natural", 0)
    engineered = class_counts.get("engineered", 0)
    unknown = class_counts.get("unknown", 0)
    denom = natural + engineered
    artificiality_score = (engineered / denom) if denom else 0.0

    distinct_engineered_types = sum(
        1 for name in decoded_counts if mc_blocks.classify_block(name) == "engineered"
    )

    dominant_biome = biome_counts.most_common(1)[0][0] if biome_counts else None

    top_blocks = [
        (name, count) for name, count in decoded_counts.most_common(TOP_BLOCKS_LIMIT)
    ]

    return {
        "artificiality_score": artificiality_score,
        "decoded_natural": natural,
        "decoded_engineered": engineered,
        "decoded_unknown": unknown,
        "decoded_liquid": class_counts.get("liquid", 0),
        "decoded_air": class_counts.get("air", 0),
        "distinct_engineered_types": distinct_engineered_types,
        "strong_indicator_count": sum(strong_indicators.values()),
        "strong_indicator_types": list(strong_indicators.keys()),
        "strong_indicator_counts": dict(strong_indicators),
        "decay_indicator_count": sum(decay_indicators.values()),
        "decay_indicator_types": list(decay_indicators.keys()),
        "uniform_volume": dict(uniform_volume),
        "dominant_biome": dominant_biome,
        "surface_min_y": surface_min,
        "depth_cutoff_y": depth_cutoff_y,
        "top_blocks": top_blocks,
        "tracked_positions": tracked_positions,
        "warnings": warnings,
    }
