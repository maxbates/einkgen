"""Tests for the convert pipeline.

The dither inner loop is pure Python and runs over 990K pixels at the panel's
1200x825 resolution, which is slow in CPython. We exercise the full pipeline
at small "panel" sizes by monkeypatching PANEL_WIDTH/PANEL_HEIGHT, and run one
end-to-end test at native resolution to verify the BMP header.
"""

from __future__ import annotations

import io
import struct

import pytest
from PIL import Image

from einkgen.core import convert as convert_mod
from einkgen.core.convert import (
    PALETTE_LEVELS,
    PANEL_HEIGHT,
    PANEL_WIDTH,
    convert,
    dither_to_levels,
)


def _make_gradient(w: int, h: int) -> Image.Image:
    """A horizontal grayscale gradient — gives the ditherer real work to do."""
    img = Image.new("L", (w, h))
    px = img.load()
    for x in range(w):
        v = int(round(255 * x / max(1, w - 1)))
        for y in range(h):
            px[x, y] = v
    return img.convert("RGB")


@pytest.fixture
def tiny_panel(monkeypatch):
    """Shrink the panel so the dither loop is tractable for tests."""
    monkeypatch.setattr(convert_mod, "PANEL_WIDTH", 60)
    monkeypatch.setattr(convert_mod, "PANEL_HEIGHT", 40)


def _parse_bmp_header(data: bytes) -> dict:
    """Pull the fields we care about out of an indexed BMP."""
    assert data[:2] == b"BM", "missing BMP magic"
    file_size = struct.unpack_from("<I", data, 2)[0]
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    dib_size = struct.unpack_from("<I", data, 14)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    height = struct.unpack_from("<i", data, 22)[0]
    planes = struct.unpack_from("<H", data, 26)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    colors_used = struct.unpack_from("<I", data, 46)[0]
    return {
        "file_size": file_size,
        "pixel_offset": pixel_offset,
        "dib_size": dib_size,
        "width": width,
        "height": abs(height),
        "planes": planes,
        "bpp": bpp,
        "colors_used": colors_used,
    }


def test_large_image_center_crops_no_resampling(tiny_panel):
    # 2x panel area — should center-crop, no scaling.
    src = _make_gradient(100, 80)
    bmp = convert(src, dither="atkinson")
    header = _parse_bmp_header(bmp)
    assert header["bpp"] == 8, "expected 8-bit indexed BMP"
    assert header["width"] == 60
    assert header["height"] == 40

    # Round-trip the BMP and check the palette.
    img = Image.open(io.BytesIO(bmp))
    assert img.mode == "P"
    grayscale = img.convert("L")
    unique = set(grayscale.getdata())
    # All pixels must come from the 8-level palette.
    assert unique.issubset(set(PALETTE_LEVELS))
    # Center-cropping a 100x80 gradient drops the extreme edges (0 and 255), so
    # we don't see every palette level — but a gradient across the middle band
    # should still produce a healthy mix.
    assert len(unique) >= 6, f"expected at least 6 levels, got {sorted(unique)}"


def test_small_image_scales_and_pads(tiny_panel):
    # Smaller than panel in both dims → scale-fit + white pad.
    src = _make_gradient(40, 20)  # panel is 60x40
    bmp = convert(src, dither="atkinson")
    header = _parse_bmp_header(bmp)
    assert header["bpp"] == 8
    assert header["width"] == 60
    assert header["height"] == 40

    img = Image.open(io.BytesIO(bmp))
    grayscale = img.convert("L")
    unique = set(grayscale.getdata())
    assert unique.issubset(set(PALETTE_LEVELS))
    # White padding plus the gradient guarantees at least the brightest level.
    assert PALETTE_LEVELS[-1] in unique


def test_both_dither_algorithms_produce_valid_output(tiny_panel):
    src = _make_gradient(80, 60)
    for algo in ("atkinson", "floyd-steinberg"):
        bmp = convert(src, dither=algo)
        header = _parse_bmp_header(bmp)
        assert header["bpp"] == 8, f"{algo}: expected 8-bit indexed BMP"
        assert header["width"] == 60
        assert header["height"] == 40
        img = Image.open(io.BytesIO(bmp))
        unique = set(img.convert("L").getdata())
        assert unique.issubset(set(PALETTE_LEVELS)), f"{algo}: off-palette pixel"
        # The gradient should produce multiple levels under either algorithm.
        assert len(unique) >= 4, f"{algo}: expected variety, got {unique}"


def test_dither_to_levels_returns_grayscale_image(tiny_panel):
    src = _make_gradient(60, 40).convert("L")
    out = dither_to_levels(src, levels=8, algorithm="atkinson")
    assert out.mode == "L"
    assert out.size == (60, 40)
    assert set(out.getdata()).issubset(set(PALETTE_LEVELS))


def test_native_panel_smoke():
    """One end-to-end run at native panel resolution to catch header regressions."""
    # Use a solid color to make the dither loop trivial (no error propagation cost).
    src = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (128, 128, 128))
    bmp = convert(src, dither="atkinson")
    header = _parse_bmp_header(bmp)
    assert header["bpp"] == 8
    assert header["width"] == PANEL_WIDTH
    assert header["height"] == PANEL_HEIGHT
