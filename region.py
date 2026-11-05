"""Reader for Anvil region files (.mca).

Copied from the Archeology tools. A region file holds up to 32x32 chunks. It
starts with an 8 KiB header: a 1024-entry chunk location table (4 bytes each:
3-byte sector offset + 1-byte sector count), followed by a 1024-entry
timestamp table (unused here). Each chunk's payload is a 4-byte big-endian
length, a 1-byte compression scheme, then the compressed NBT data.
"""

import gzip
import struct
import zlib
from pathlib import Path

import mc_nbt

SECTOR_SIZE = 4096
HEADER_SIZE = SECTOR_SIZE  # location table only; we don't need timestamps

COMPRESSION_GZIP = 1
COMPRESSION_ZLIB = 2
COMPRESSION_NONE = 3


class RegionFile:
    def __init__(self, path):
        self.path = Path(path)
        self._file = open(self.path, "rb")
        self._locations = self._file.read(HEADER_SIZE)
        if len(self._locations) < HEADER_SIZE:
            # Empty/placeholder region files (0 bytes is common) have no
            # header at all; treat every chunk in them as absent.
            self._locations += b"\x00" * (HEADER_SIZE - len(self._locations))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._file.close()

    def _location(self, local_x: int, local_z: int):
        idx = (local_x % 32) + (local_z % 32) * 32
        entry = self._locations[idx * 4:idx * 4 + 4]
        offset_sectors = int.from_bytes(entry[0:3], "big")
        sector_count = entry[3]
        return offset_sectors, sector_count

    def has_chunk(self, local_x: int, local_z: int) -> bool:
        offset_sectors, _ = self._location(local_x, local_z)
        return offset_sectors != 0

    def read_chunk_nbt(self, local_x: int, local_z: int):
        """Return the parsed root compound for chunk (local_x, local_z)
        within this region (each in [0, 32)), or None if absent."""
        offset_sectors, sector_count = self._location(local_x, local_z)
        if offset_sectors == 0 and sector_count == 0:
            return None

        self._file.seek(offset_sectors * SECTOR_SIZE)
        length = struct.unpack(">I", self._file.read(4))[0]
        compression = self._file.read(1)[0]
        payload = self._file.read(length - 1)

        if compression == COMPRESSION_GZIP:
            raw = gzip.decompress(payload)
        elif compression == COMPRESSION_ZLIB:
            raw = zlib.decompress(payload)
        elif compression == COMPRESSION_NONE:
            raw = payload
        else:
            raise ValueError(
                f"Unsupported chunk compression scheme {compression} "
                f"in {self.path} at chunk ({local_x}, {local_z})"
            )

        _, root = mc_nbt.parse(raw)
        return root
