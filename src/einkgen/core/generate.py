"""OpenAI `gpt-image-2` adapter and the BASE_PROMPT preamble.

The pipeline always requests 1536x1024 from the model — the closest size to
the panel's 1200x825 (1.5:1 vs ~1.4545:1) — so the downstream `convert()` step
can center-crop with zero resampling. See ARCHITECTURE §6.

We deliberately call the model at ``quality="medium"`` rather than ``"high"``.
The dither step erases sub-pixel detail anyway, so the extra cost of high
quality is wasted on an 8-grayscale e-paper panel.

The random-pick prompt library is in ``einkgen.core.prompt_library`` —
operator-editable at runtime via the SPA admin tab and the ``einkgen
prompts`` CLI. ``random_prompt()`` below delegates to it.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from einkgen.core import prompt_library as _prompt_library

# Prepended to every user/random subject string. Lifted verbatim from ARCHITECTURE §6.
BASE_PROMPT = (
    "Compose a single image at 1536×1024 (landscape, 3:2). It will be center-cropped\n"
    "to 1200×825 (a 9.7\" e-paper panel) and dithered to 8 grayscale levels. Keep\n"
    "important content within the centered safe area (1200×825). Use high-contrast\n"
    "tones, bold shapes, and clean edges — subtle gradients and fine textures will\n"
    "not survive dithering. No text or watermarks. Subject:"
)

# Re-export the seed library so callers that want the unedited bank (tests,
# `einkgen prompts reset`) have one import path. The runtime library is
# operator-editable at s3://<bucket>/config/prompt_library.txt — see
# einkgen.core.prompt_library.
PROMPT_LIBRARY: list[str] = list(_prompt_library.DEFAULTS)

MODEL = "gpt-image-2"
IMAGE_SIZE = "1536x1024"
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
    """Return a random entry from the operator-editable prompt library.

    Reads ``s3://<bucket>/config/prompt_library.txt`` via the prompt_library
    module (cached for 60 s on warm Lambdas). Falls back to ``PROMPT_LIBRARY``
    (the seed defaults) if the file is missing.
    """
    return _prompt_library.random_prompt()
