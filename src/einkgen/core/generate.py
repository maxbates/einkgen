"""OpenAI `gpt-image-2` adapter, base prompt, and the random prompt library.

The pipeline always requests 1200x832 from the model — the smallest size
`gpt-image-2` accepts (both dims must be multiples of 16) that still covers
the panel's 1200x825 in both dimensions — so the downstream `convert()` step
center-crops 7 px off the height with zero resampling. Aspect 1.4423 vs panel
1.4545 is 0.84% off; the model effectively composes for the panel. This used
to be 1536x1024 (`gpt-image-1`'s only landscape option, inherited when we
upgraded to `gpt-image-2`) which generated 1,572,864 px and threw 37% away.
See ARCHITECTURE §6.

We deliberately call the model at ``quality="medium"`` rather than ``"high"``.
The dither step erases sub-pixel detail anyway, so the extra cost of high
quality is wasted on an 8-grayscale e-paper panel.
"""

from __future__ import annotations

import base64
import os
import random
from typing import Any

# Prepended to every user/random subject string. Lifted verbatim from ARCHITECTURE §6.
BASE_PROMPT = (
    "Compose a single image at 1200×832 (landscape, ~1.44:1). It will be displayed on\n"
    "a 1200×825 e-paper panel (a 7-pixel sliver trimmed off the height) and dithered\n"
    "to 8 grayscale levels. The whole canvas is visible — there is no safe-area inset.\n"
    "Use high-contrast tones, bold shapes, and clean edges — subtle gradients and fine\n"
    "textures will not survive dithering. No text or watermarks. Subject:"
)

# The 10 entries from ARCHITECTURE §6. Each is a complete prompt string (the
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

MODEL = "gpt-image-2"
IMAGE_SIZE = "1200x832"
QUALITY = "medium"


def _resolve_api_key() -> str | None:
    """Resolve the OpenAI API key from env or Secrets Manager.

    CLI / local dev path: ``OPENAI_API_KEY`` env var (works without AWS perms).
    Lambda path: ``OPENAI_API_KEY_SECRET_NAME`` env var names a Secrets Manager
    secret whose ``SecretString`` is the raw key. Falls through to None if
    neither is set — the OpenAI client will then raise its own error.
    """
    direct = os.environ.get("OPENAI_API_KEY")
    if direct:
        return direct
    secret_name = os.environ.get("OPENAI_API_KEY_SECRET_NAME")
    if not secret_name:
        return None
    import boto3  # local import to keep cold-start fast for non-Lambda callers

    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=secret_name)
    return resp.get("SecretString")


def _default_client() -> Any:
    """Lazily construct an OpenAI client. Imported lazily so import-time
    failures (e.g. missing OPENAI_API_KEY) don't break unrelated CLI commands."""
    from openai import OpenAI

    api_key = _resolve_api_key()
    if api_key is None:
        return OpenAI()  # let the SDK raise its standard "no key" error
    return OpenAI(api_key=api_key)


def generate(prompt: str, *, client: Any = None) -> bytes:
    """Generate a PNG via OpenAI gpt-image-2 and return raw PNG bytes.

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
        quality=QUALITY,
        n=1,
    )
    return _decode_first(response)


def generate_from_image(
    prompt: str,
    image_bytes: bytes,
    *,
    image_filename: str = "input.png",
    client: Any = None,
) -> bytes:
    """Restyle an input image per `prompt` via gpt-image-2's edit endpoint.

    Used when an email arrives with both an attachment and a body/subject —
    the attachment is the reference, the prompt steers the regeneration. The
    output is sized to ``IMAGE_SIZE`` so the downstream center-crop runs with
    no resampling, same as text-to-image.
    """
    if client is None:
        client = _default_client()
    full_prompt = f"{BASE_PROMPT} {prompt}".strip()
    # The SDK expects a file-like object; an in-memory BytesIO works and
    # avoids a temp file. The filename hint helps the SDK set MIME correctly
    # — extension matters more than the actual bytes.
    import io

    buf = io.BytesIO(image_bytes)
    buf.name = image_filename
    response = client.images.edit(
        model=MODEL,
        image=buf,
        prompt=full_prompt,
        size=IMAGE_SIZE,
        quality=QUALITY,
        n=1,
    )
    return _decode_first(response)


def _decode_first(response: Any) -> bytes:
    """Extract base64 PNG bytes from an OpenAI images response."""
    datum = response.data[0]
    # `gpt-image-2` returns base64 in `b64_json` by default. Some wrappers/mocks
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
