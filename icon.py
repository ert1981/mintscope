"""MintScope 512x512 PNG icon, generated deterministically in pure Python (no image files in the image build).
Served at /static/icon.png and used as the x402 `resource.iconUrl` (the Bazaar does not accept SVG)."""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache

SIZE = 512
BG = (12, 18, 38)
RING = (0, 82, 255)
LENS = (120, 200, 255)
HANDLE = (230, 236, 250)


def _pixel(x: int, y: int) -> tuple[int, int, int]:
    cx, cy = 230, 230
    d2 = (x - cx) ** 2 + (y - cy) ** 2
    if d2 <= 120 ** 2:
        # Lens with a simple 3x3 "NFT grid".
        gx, gy = (x - (cx - 72)) // 50, (y - (cy - 72)) // 50
        inside = 0 <= gx < 3 and 0 <= gy < 3 and (x - (cx - 72)) % 50 < 42 and (y - (cy - 72)) % 50 < 42
        return LENS if inside and (gx + gy) % 2 == 0 else (24, 40, 80)
    if d2 <= 158 ** 2:
        return RING
    # Handle: thick diagonal bar toward the bottom-right corner.
    if x > cx and y > cy and abs((x - cx) - (y - cy)) <= 34 and (x - cx) + (y - cy) <= 500:
        return HANDLE
    return BG


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


@lru_cache(maxsize=1)
def png_bytes() -> bytes:
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)  # filter: none
        for x in range(SIZE):
            rows.extend(_pixel(x, y))
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _chunk(b"IEND", b""))
