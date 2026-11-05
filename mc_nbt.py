"""Minimal, dependency-free NBT (Named Binary Tag) reader.

Copied from the Archeology tools. Supports the classic big-endian NBT format
used on disk by Java Edition (region files, level.dat, player data).
Read-only: this service never writes back to world files.
"""

import struct

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


def parse(data: bytes):
    """Parse a full NBT buffer. Returns (root_name, root_value)."""
    name, pos, value = _read_named_tag(data, 0)
    return name, value


def _read_named_tag(buf: bytes, pos: int):
    tag_type = buf[pos]
    pos += 1
    if tag_type == TAG_END:
        return None, pos, None
    name_len = struct.unpack_from(">H", buf, pos)[0]
    pos += 2
    name = buf[pos:pos + name_len].decode("utf-8")
    pos += name_len
    value, pos = _read_payload(buf, pos, tag_type)
    return name, pos, value


def _read_payload(buf: bytes, pos: int, tag_type: int):
    if tag_type == TAG_BYTE:
        return struct.unpack_from(">b", buf, pos)[0], pos + 1
    if tag_type == TAG_SHORT:
        return struct.unpack_from(">h", buf, pos)[0], pos + 2
    if tag_type == TAG_INT:
        return struct.unpack_from(">i", buf, pos)[0], pos + 4
    if tag_type == TAG_LONG:
        return struct.unpack_from(">q", buf, pos)[0], pos + 8
    if tag_type == TAG_FLOAT:
        return struct.unpack_from(">f", buf, pos)[0], pos + 4
    if tag_type == TAG_DOUBLE:
        return struct.unpack_from(">d", buf, pos)[0], pos + 8
    if tag_type == TAG_BYTE_ARRAY:
        n = struct.unpack_from(">i", buf, pos)[0]
        pos += 4
        return buf[pos:pos + n], pos + n
    if tag_type == TAG_STRING:
        n = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
        return buf[pos:pos + n].decode("utf-8"), pos + n
    if tag_type == TAG_LIST:
        item_type = buf[pos]
        pos += 1
        n = struct.unpack_from(">i", buf, pos)[0]
        pos += 4
        items = []
        for _ in range(n):
            value, pos = _read_payload(buf, pos, item_type)
            items.append(value)
        return items, pos
    if tag_type == TAG_COMPOUND:
        result = {}
        while True:
            tag_type = buf[pos]
            if tag_type == TAG_END:
                pos += 1
                break
            name, pos, value = _read_named_tag(buf, pos)
            result[name] = value
        return result, pos
    if tag_type == TAG_INT_ARRAY:
        n = struct.unpack_from(">i", buf, pos)[0]
        pos += 4
        arr = list(struct.unpack_from(f">{n}i", buf, pos))
        return arr, pos + 4 * n
    if tag_type == TAG_LONG_ARRAY:
        n = struct.unpack_from(">i", buf, pos)[0]
        pos += 4
        arr = list(struct.unpack_from(f">{n}q", buf, pos))
        return arr, pos + 8 * n
    raise ValueError(f"Unknown NBT tag type: {tag_type} at offset {pos}")
