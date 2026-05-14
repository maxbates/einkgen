"""Image conversion pipeline for the Inkplate 10 panel.

Takes an arbitrary source image and produces an 8-bit indexed BMP at the
panel's native resolution (1200x825) with an 8-entry evenly-spaced grayscale
palette. Per README §6, generated images come in at 1536x1024 so we always
center-crop with zero resampling; uploaded images that are smaller in either
dimension get scale-fit + white pad.
"""

from __future__ import annotations

import io
from typing import Union

from PIL import Image

# Panel native resolution (landscape, no rotation).
PANEL_WIDTH = 1200
PANEL_HEIGHT = 825

# 8-level grayscale palette, evenly spaced across 0..255 (steps of ~36).
PALETTE_LEVELS: tuple[int, ...] = (0, 36, 73, 109, 146, 182, 219, 255)

# BMP palette: 8 entries x 3 channels (R=G=B since grayscale).
_PALETTE_FLAT: list[int] = [v for level in PALETTE_LEVELS for v in (level, level, level)]

ImageInput = Union[Image.Image, bytes]


def _load(src: ImageInput) -> Image.Image:
    if isinstance(src, (bytes, bytearray)):
        return Image.open(io.BytesIO(src))
    return src


def _fit_to_canvas(img: Image.Image) -> Image.Image:
    """Get the image to exactly PANEL_WIDTH x PANEL_HEIGHT.

    - If both dims are >= panel: center-crop (pixel-exact, no resampling).
    - Otherwise: scale-fit (preserve aspect) + white pad to the canvas.
    """
    w, h = img.size
    if w >= PANEL_WIDTH and h >= PANEL_HEIGHT:
        left = (w - PANEL_WIDTH) // 2
        top = (h - PANEL_HEIGHT) // 2
        return img.crop((left, top, left + PANEL_WIDTH, top + PANEL_HEIGHT))

    # Source is smaller in at least one dimension. Scale to fit while preserving
    # aspect ratio, then pad with white to fill the canvas.
    scale = min(PANEL_WIDTH / w, PANEL_HEIGHT / h)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    scaled = img.resize((new_w, new_h), Image.LANCZOS)

    # Pad with white. Use mode "L" if input was grayscale-ish; we'll convert
    # to L further down anyway, so RGB white is fine here.
    canvas_mode = "RGB" if scaled.mode not in ("L", "LA", "P") else "L"
    fill = (255, 255, 255) if canvas_mode == "RGB" else 255
    canvas = Image.new(canvas_mode, (PANEL_WIDTH, PANEL_HEIGHT), fill)
    paste_x = (PANEL_WIDTH - new_w) // 2
    paste_y = (PANEL_HEIGHT - new_h) // 2
    # Use the scaled image's alpha as a mask if present.
    if scaled.mode in ("RGBA", "LA"):
        canvas.paste(scaled.convert(canvas_mode), (paste_x, paste_y), scaled.split()[-1])
    else:
        canvas.paste(scaled.convert(canvas_mode), (paste_x, paste_y))
    return canvas


def _to_grayscale(img: Image.Image) -> Image.Image:
    """Luminance conversion to 8-bit grayscale."""
    if img.mode == "L":
        return img
    # Pillow's "L" conversion uses ITU-R 601-2 luma: L = R*299/1000 + G*587/1000 + B*114/1000
    return img.convert("L")


# ---------- Dithering ----------------------------------------------------------


def _quantize_level(value: float) -> tuple[int, int]:
    """Snap a 0..255 value to the nearest palette level.

    Returns (palette_index, palette_value_0_255).
    """
    if value <= 0:
        return 0, PALETTE_LEVELS[0]
    if value >= 255:
        return len(PALETTE_LEVELS) - 1, PALETTE_LEVELS[-1]
    # Linear search is fine — only 8 entries.
    best_i = 0
    best_d = abs(value - PALETTE_LEVELS[0])
    for i in range(1, len(PALETTE_LEVELS)):
        d = abs(value - PALETTE_LEVELS[i])
        if d < best_d:
            best_i = i
            best_d = d
    return best_i, PALETTE_LEVELS[best_i]


def _dither_error_diffuse(
    gray: Image.Image,
    *,
    algorithm: str,
    levels: int,
) -> Image.Image:
    """Error-diffusion dither (Atkinson or Floyd–Steinberg) to an N-level palette.

    For this project `levels` is always 8 and the palette is `PALETTE_LEVELS`.
    The `levels` arg exists so `dither_to_levels` can stay flexible; if a caller
    passes something other than 8 we still snap to the nearest of an evenly
    spaced palette derived on the fly.
    """
    if levels == len(PALETTE_LEVELS):
        palette = PALETTE_LEVELS
    else:
        if levels < 2:
            raise ValueError("levels must be >= 2")
        step = 255.0 / (levels - 1)
        palette = tuple(int(round(i * step)) for i in range(levels))

    w, h = gray.size
    # Working buffer in float so error accumulates cleanly.
    pixels = [float(p) for p in gray.getdata()]

    if algorithm == "atkinson":
        # Atkinson distributes 1/8 of the error to 6 neighbours; 2/8 of the
        # error is intentionally discarded, which is what gives it the crisp
        # high-contrast Mac look.
        offsets = (
            (1, 0, 1 / 8),
            (2, 0, 1 / 8),
            (-1, 1, 1 / 8),
            (0, 1, 1 / 8),
            (1, 1, 1 / 8),
            (0, 2, 1 / 8),
        )
    elif algorithm in ("floyd-steinberg", "floyd_steinberg", "fs"):
        offsets = (
            (1, 0, 7 / 16),
            (-1, 1, 3 / 16),
            (0, 1, 5 / 16),
            (1, 1, 1 / 16),
        )
    else:
        raise ValueError(f"unknown dither algorithm: {algorithm!r}")

    def snap(v: float) -> int:
        if v <= 0:
            return palette[0]
        if v >= 255:
            return palette[-1]
        best = palette[0]
        best_d = abs(v - palette[0])
        for p in palette[1:]:
            d = abs(v - p)
            if d < best_d:
                best = p
                best_d = d
        return best

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            old = pixels[idx]
            new = snap(old)
            pixels[idx] = float(new)
            err = old - new
            if err == 0:
                continue
            for dx, dy, weight in offsets:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    pixels[ny * w + nx] += err * weight

    out = Image.new("L", (w, h))
    out.putdata([int(p) for p in pixels])
    return out


def dither_to_levels(
    gray: Image.Image,
    levels: int = 8,
    algorithm: str = "atkinson",
) -> Image.Image:
    """Dither a grayscale image to a 0..255 grayscale image quantized to `levels`.

    Returned image is mode "L" with pixel values restricted to the palette.
    Exposed publicly so callers can dither without going all the way to BMP.
    """
    if gray.mode != "L":
        gray = gray.convert("L")
    return _dither_error_diffuse(gray, algorithm=algorithm, levels=levels)


# ---------- BMP encode ---------------------------------------------------------


def _encode_indexed_bmp(dithered_gray: Image.Image) -> bytes:
    """Encode an 8-level grayscale "L" image as 8-bit indexed BMP.

    The palette is the 8-entry grayscale `PALETTE_LEVELS`. Browsers and the
    Inkplate Arduino library both render 8-bit indexed BMP natively.
    """
    # Build a value -> palette-index lookup.
    value_to_index = {v: i for i, v in enumerate(PALETTE_LEVELS)}

    w, h = dithered_gray.size
    src = dithered_gray.tobytes()
    # Map each grayscale value to its palette index (0..7). If a pixel slipped
    # off the palette for any reason, snap to the nearest level.
    out = bytearray(len(src))
    for i, b in enumerate(src):
        idx = value_to_index.get(b)
        if idx is None:
            idx, _ = _quantize_level(b)
        out[i] = idx

    indexed = Image.frombytes("P", (w, h), bytes(out))
    # Pillow expects a 768-entry palette (256*3). Pad unused slots with zeros.
    full_palette = list(_PALETTE_FLAT) + [0] * (768 - len(_PALETTE_FLAT))
    indexed.putpalette(full_palette)

    buf = io.BytesIO()
    indexed.save(buf, format="BMP")
    return buf.getvalue()


# ---------- Public API ---------------------------------------------------------


def convert(src_image: ImageInput, dither: str = "atkinson") -> bytes:
    """Run the full Inkplate image pipeline on `src_image`.

    Returns 8-bit indexed BMP bytes at PANEL_WIDTH x PANEL_HEIGHT with an
    8-entry grayscale palette.
    """
    img = _load(src_image)
    fitted = _fit_to_canvas(img)
    gray = _to_grayscale(fitted)
    dithered = dither_to_levels(gray, levels=len(PALETTE_LEVELS), algorithm=dither)
    return _encode_indexed_bmp(dithered)
