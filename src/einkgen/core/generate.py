"""OpenAI `gpt-image-1` adapter, base prompt, and the random prompt library.

The pipeline always requests 1536x1024 from the model — the closest size to
the panel's 1200x825 (1.5:1 vs ~1.4545:1) — so the downstream `convert()` step
can center-crop with zero resampling. See README §6.
"""

from __future__ import annotations

import base64
import random
from typing import Any

# Prepended to every user/random subject string. Lifted verbatim from README §6.
BASE_PROMPT = (
    "Compose a single image at 1536×1024 (landscape, 3:2). It will be center-cropped\n"
    "to 1200×825 (a 9.7\" e-paper panel) and dithered to 8 grayscale levels. Keep\n"
    "important content within the centered safe area (1200×825). Use high-contrast\n"
    "tones, bold shapes, and clean edges — subtle gradients and fine textures will\n"
    "not survive dithering. No text or watermarks. Subject:"
)

# The 10 entries from README §6. Each is a complete prompt string (the
# descriptive text after the dash, not just the title) so generators can append
# them to BASE_PROMPT verbatim.
PROMPT_LIBRARY: list[str] = [
    "Geometric composition — overlapping circles, squares, triangles; bold flat shapes; high contrast.",
    "Botanical illustration — pen-and-ink style; a single plant or flower; scientific-diagram aesthetic.",
    "Pixel art scene — 32×32 or 64×64 motif scaled up; chunky, low-detail.",
    "Architectural line drawing — building, bridge, or interior; technical-drawing feel.",
    "Topographic / contour pattern — abstract elevation lines or isobars.",
    "Vintage scientific diagram — anatomy, astronomy, or mechanical schematic.",
    "Baby-friendly collage — simple recognisable objects (animal, fruit, toy) arranged playfully.",
    "Abstract generative pattern — flow fields, Voronoi, fractal noise.",
    "Portrait study — single face, woodcut or charcoal feel.",
    "Model's choice — open-ended: anything striking that reads well in 8 grays.",
]

assert len(PROMPT_LIBRARY) == 10, "PROMPT_LIBRARY must have exactly 10 entries"

MODEL = "gpt-image-1"
IMAGE_SIZE = "1536x1024"


def _default_client() -> Any:
    """Lazily construct an OpenAI client. Imported lazily so import-time
    failures (e.g. missing OPENAI_API_KEY) don't break unrelated CLI commands."""
    from openai import OpenAI

    return OpenAI()


def generate(prompt: str, *, client: Any = None) -> bytes:
    """Generate a PNG via OpenAI gpt-image-1 and return raw PNG bytes.

    BASE_PROMPT is prepended to `prompt` inside this function — callers should
    pass only the subject text. `client` is a dependency-injection hook for
    tests; production callers should leave it as None.
    """
    if client is None:
        client = _default_client()
    full_prompt = f"{BASE_PROMPT} {prompt}".strip()
    response = client.images.generate(
        model=MODEL,
        prompt=full_prompt,
        size=IMAGE_SIZE,
        n=1,
    )
    datum = response.data[0]
    # `gpt-image-1` returns base64 in `b64_json` by default. Some wrappers/mocks
    # expose `url` instead; we don't fetch URLs here — callers/tests should
    # provide b64_json.
    b64 = getattr(datum, "b64_json", None)
    if b64 is None and isinstance(datum, dict):
        b64 = datum.get("b64_json")
    if b64 is None:
        raise RuntimeError("OpenAI response missing b64_json image payload")
    return base64.b64decode(b64)


def random_prompt() -> str:
    """Return a random entry from PROMPT_LIBRARY (raw subject string)."""
    return random.choice(PROMPT_LIBRARY)
