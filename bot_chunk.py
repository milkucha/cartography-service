"""Convert a bot-forwarded chunk payload into the Anvil-shaped chunk root.

``POST /chunk_raw`` receives name palettes plus packed palette indices (see
``CONTRACT.md``).  This module rebuilds the same dictionary ``region.py``
produces from a save file, so ``loader.decode_surface`` and
``composition.analyze_chunk`` run unchanged and the resulting record is
byte-for-byte comparable with the save-loaded one (the parity gate).

Only the Overworld is forwarded.  Sections that are entirely air may be
omitted by the bot; they are re-inserted here as single-value air sections so
the derived world height (and therefore the composition cutoff) matches the
save reader.
"""

from __future__ import annotations

import base64
import binascii

import mc_blocks
import palette

FORMAT = 1
MAX_CHUNKS = 16
MAX_SECTIONS = 64
MAX_PALETTE = 4096
BLOCKS_PER_SECTION = 4096
BIOMES_PER_SECTION = 64
AIR = "minecraft:air"


class PayloadError(ValueError):
    """A chunk payload that cannot be decoded (answered with 400/rejected)."""


def _b64(value, what):
    if not isinstance(value, str):
        raise PayloadError(f"{what}: expected a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise PayloadError(f"{what}: invalid base64")


def _names(entries, what):
    if not isinstance(entries, list) or not entries:
        raise PayloadError(f"{what}: expected a non-empty palette")
    if len(entries) > MAX_PALETTE:
        raise PayloadError(f"{what}: palette over {MAX_PALETTE} entries")
    names = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            raise PayloadError(f"{what}: palette entries must be names")
        names.append(entry)
    return names


def _packed_indices(data, bits, entries, what):
    if not isinstance(bits, int) or bits <= 0 or bits > 32:
        raise PayloadError(f"{what}: bad bits")
    per_long = 64 // bits
    needed = (entries + per_long - 1) // per_long
    raw = _b64(data, what)
    if len(raw) != needed * 8:
        raise PayloadError(f"{what}: data length does not match bits")
    longs = []
    for i in range(needed):
        low = int.from_bytes(raw[i * 8:i * 8 + 4], "little")
        high = int.from_bytes(raw[i * 8 + 4:i * 8 + 8], "little")
        longs.append(low | (high << 32))
    return palette.unpack_indices(longs, bits, entries)


def _u16_indices(data, entries, what):
    raw = _b64(data, what)
    if len(raw) != entries * 2:
        raise PayloadError(f"{what}: data length does not match u16")
    return [int.from_bytes(raw[i * 2:i * 2 + 2], "little") for i in range(entries)]


def _decode_indices(section, names, entries, what):
    data = section.get("data")
    if data is None:
        if len(names) != 1:
            raise PayloadError(f"{what}: data missing for a multi-entry palette")
        return [0] * entries
    enc = section.get("enc", "packed")
    if enc == "u16":
        indices = _u16_indices(data, entries, what)
    elif enc == "packed":
        indices = _packed_indices(data, section.get("bits"), entries, what)
    else:
        raise PayloadError(f"{what}: unknown enc {enc!r}")
    for index in indices:
        if not 0 <= index < len(names):
            raise PayloadError(f"{what}: palette index out of range")
    return indices


def _block_section(section, what):
    names = _names(section.get("palette"), what)
    indices = _decode_indices(section, names, BLOCKS_PER_SECTION, what)
    entry = {
        "Y": int(section.get("y")),
        "block_states": {"palette": [{"Name": name} for name in names]},
    }
    if len(names) > 1:
        bits = palette.bits_needed(len(names), minimum=4)
        if bits > 16:
            raise PayloadError(f"{what}: palette too large for a 16-bit section")
        entry["block_states"]["data"] = palette.pack_indices(indices, bits)
    return entry, names, indices


def _biome_section(section, what):
    names = _names(section.get("palette"), what)
    if len(names) > 256:
        raise PayloadError(f"{what}: biome palette over 256 entries")
    indices = _decode_indices(section, names, BIOMES_PER_SECTION, what)
    entry = {"Y": int(section.get("y")), "biomes": {"palette": list(names)}}
    if len(names) > 1:
        entry["biomes"]["data"] = palette.pack_indices(indices, palette.bits_needed(len(names)))
    return entry


def _heightmap(by_y, min_y, height):
    """WORLD_SURFACE in the save's encoding: topmost non-air Y + 1, minus min_y."""
    tops = [0] * 256
    for y_index in sorted(by_y):
        _, names, indices = by_y[y_index]
        for local_y in range(16):
            row = local_y * 256
            world_y = y_index * 16 + local_y
            offset = world_y + 1 - min_y
            for column in range(256):
                if names[indices[row + column]] not in mc_blocks.AIR:
                    tops[column] = offset
    bits = palette.bits_needed(height + 1, minimum=1)
    return palette.pack_indices(tops, bits)


def to_root(chunk):
    """Build an Anvil-shaped root dict from one ``chunks[]`` payload item."""
    if not isinstance(chunk, dict):
        raise PayloadError("chunk must be an object")
    try:
        cx, cz = int(chunk["cx"]), int(chunk["cz"])
        min_y, height = int(chunk["min_y"]), int(chunk["height"])
    except (KeyError, TypeError, ValueError):
        raise PayloadError("chunk needs integer cx, cz, min_y and height")
    if height <= 0 or height % 16 or height > 1024:
        raise PayloadError("height must be a positive multiple of 16 up to 1024")
    raw_sections = chunk.get("sections") or []
    if not isinstance(raw_sections, list):
        raise PayloadError("sections must be a list")
    if len(raw_sections) > MAX_SECTIONS:
        raise PayloadError(f"more than {MAX_SECTIONS} sections in one chunk")

    first_y = min_y >> 4
    section_count = height // 16
    last_y = first_y + section_count

    by_y = {}
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise PayloadError("section must be an object")
        try:
            y = int(raw["y"])
        except (KeyError, TypeError, ValueError):
            raise PayloadError("section needs an integer y")
        if y < first_y or y >= last_y:
            raise PayloadError("section outside the chunk's vertical range")
        if y in by_y:
            raise PayloadError("duplicate section y")
        by_y[y] = _block_section(raw, f"section {y}")

    for y in range(first_y, last_y):
        if y not in by_y:
            by_y[y] = (
                {"Y": y, "block_states": {"palette": [{"Name": AIR}]}},
                [AIR],
                [0] * BLOCKS_PER_SECTION,
            )

    sections = [by_y[y][0] for y in sorted(by_y)]

    raw_biomes = chunk.get("biomes") or []
    if raw_biomes and not isinstance(raw_biomes, list):
        raise PayloadError("biomes must be a list")
    biome_entries = {}
    for raw in raw_biomes:
        if not isinstance(raw, dict):
            raise PayloadError("biome section must be an object")
        try:
            y = int(raw["y"])
        except (KeyError, TypeError, ValueError):
            raise PayloadError("biome section needs an integer y")
        if y < first_y or y >= last_y:
            raise PayloadError("biome section outside the chunk's vertical range")
        biome_entries[y] = _biome_section(raw, f"biome section {y}")

    for y, entry in biome_entries.items():
        by_y[y][0]["biomes"] = entry["biomes"]

    return cx, cz, {
        "Status": "minecraft:full",
        "sections": sections,
        "Heightmaps": {"WORLD_SURFACE": _heightmap(by_y, min_y, height)},
    }
