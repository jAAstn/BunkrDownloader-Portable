"""Determine an image's height from a partial download (header sniffing).

Used to enforce the minimum image height filter before a file is downloaded:
only the first few hundred kilobytes of the file are requested, which is
enough to read the dimensions for the supported formats
(png/jpg/jpeg/gif/webp/bmp). Any failure returns None, and callers treat an
unknown height as "download anyway" so the filter never drops a file it could
not inspect.
"""

from __future__ import annotations

import struct
from pathlib import Path

import requests

from src.config import DOWNLOAD_HEADERS

# Extensions covered by the height filter (matched against the filename).
IMAGE_EXTENSIONS = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})

# How many bytes of the file are read before giving up on finding a header.
HEADER_BYTES = 256 * 1024
_TIMEOUT = 10

# JPEG start-of-frame markers carry the dimensions (C4/C8/CC are not SOF).
_JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})


def is_image(filename: str) -> bool:
    """Return True when the filename has an extension covered by the filter."""
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def fetch_image_height(url: str) -> int | None:
    """Return the image height in pixels, or None when it cannot be determined.

    Reads at most HEADER_BYTES of the file via a ranged request (falling back
    to the first HEADER_BYTES of an un-ranged response), so a large image
    costs a single small request instead of a full download.
    """
    headers = {
        **DOWNLOAD_HEADERS,
        "Accept-Encoding": "identity",
        "Range": f"bytes=0-{HEADER_BYTES - 1}",
    }
    try:
        with requests.get(url, headers=headers, stream=True, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            data = response.raw.read(HEADER_BYTES)

    except (requests.RequestException, OSError):
        return None

    return image_height(data)


def image_height(data: bytes) -> int | None:
    """Parse an image's height from its leading bytes (best effort)."""
    if len(data) < 16:
        return None

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_height(data)

    if data[:6] in (b"GIF87a", b"GIF89a"):
        return _gif_height(data)

    if data.startswith(b"BM"):
        return _bmp_height(data)

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_height(data)

    if data[:2] == b"\xff\xd8":
        return _jpeg_height(data)

    return None


def _png_height(data: bytes) -> int | None:
    # 8B signature + 4B chunk length + "IHDR" + 4B width + 4B height
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">I", data[20:24])[0]


def _gif_height(data: bytes) -> int | None:
    # 6B signature + 2B logical screen width + 2B logical screen height
    if len(data) < 10:
        return None
    return struct.unpack("<H", data[8:10])[0]


def _bmp_height(data: bytes) -> int | None:
    # 14B file header, then the DIB header: size (4B), width/height fields.
    if len(data) < 22:
        return None
    dib_size = struct.unpack_from("<I", data, 14)[0]
    if dib_size == 12:  # BITMAPCOREHEADER uses 16-bit dimensions
        return abs(struct.unpack_from("<h", data, 20)[0])
    if len(data) < 26:
        return None
    return abs(struct.unpack_from("<i", data, 22)[0])


def _webp_height(data: bytes) -> int | None:
    chunk = data[12:16]
    if chunk == b"VP8X":  # extended: canvas size, stored minus one
        if len(data) < 30:
            return None
        return int.from_bytes(data[27:30], "little") + 1

    if chunk == b"VP8 ":  # lossy: sync code followed by 14-bit dimensions
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            return None
        return struct.unpack_from("<H", data, 28)[0] & 0x3FFF

    if chunk == b"VP8L":  # lossless: signature byte then packed dimensions
        if len(data) < 25 or data[20] != 0x2F:
            return None
        bits = struct.unpack_from("<I", data, 21)[0]
        return ((bits >> 14) & 0x3FFF) + 1

    return None


def _jpeg_height(data: bytes) -> int | None:
    # Walk the marker segments until a start-of-frame header is found.
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue

        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # no length field
            pos += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image / entropy data: no more headers
            break

        segment_length = struct.unpack_from(">H", data, pos + 2)[0]
        if segment_length < 2:
            break
        if marker in _JPEG_SOF_MARKERS:
            if pos + 9 > len(data):  # header not fully inside the buffered prefix
                break
            return struct.unpack_from(">H", data, pos + 5)[0]
        pos += 2 + segment_length

    return None