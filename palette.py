"""Bit-unpacking for Minecraft's paletted container format (block_states
and biomes sections), MC 1.16+ "no crossing" packing: entries never span
two longs, so each long holds floor(64 / bits_per_entry) whole entries
and any leftover bits in that long go unused.

Copied from the Archeology tools.
"""


def bits_needed(palette_size: int, minimum: int = 0) -> int:
    if palette_size <= 1:
        return 0
    return max(minimum, (palette_size - 1).bit_length())


def unpack_indices(data_longs, bits_per_entry: int, total_entries: int):
    if bits_per_entry == 0:
        return [0] * total_entries
    entries_per_long = 64 // bits_per_entry
    mask = (1 << bits_per_entry) - 1
    indices = []
    for value in data_longs:
        uval = value & 0xFFFFFFFFFFFFFFFF
        for i in range(entries_per_long):
            if len(indices) >= total_entries:
                return indices
            indices.append((uval >> (i * bits_per_entry)) & mask)
    return indices
