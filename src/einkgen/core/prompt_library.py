"""S3-backed prompt library for the cron's ``random`` picks.

The library lives at ``s3://<bucket>/config/prompt_library.txt`` as a plain
text file:

    # one prompt per line; blank lines and #-prefixed lines are ignored
    Geometric composition — overlapping circles, squares, triangles.
    Botanical illustration — pen-and-ink style; a single plant or flower.

It's a file (not Secrets Manager) so an operator can edit it from the SPA
admin tab, the AWS console, the CLI, or ``aws s3 cp`` — there's nothing
sensitive here. The Lambda keeps the loaded list in module scope to
amortise the GetObject across warm invocations.

If the S3 object is missing, ``load()`` falls back to ``DEFAULTS`` (the
original 10 entries from ARCHITECTURE §6) so a fresh deploy never picks
from an empty bank.
"""

from __future__ import annotations

import random
import time

from einkgen.core import s3

PROMPT_LIBRARY_KEY = "config/prompt_library.txt"

# Mirrors device_status / email_allowlist — bounds how long a stale list
# survives on warm Lambda containers after the operator edits it.
PROMPT_LIBRARY_CACHE_TTL_SECONDS = 60

# Same 10 entries previously hardcoded in generate.PROMPT_LIBRARY. They live
# here now so missing-file fallback and "reset to defaults" share one source.
DEFAULTS: tuple[str, ...] = (
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
)

_HEADER = (
    "# einkgen prompt library — random subject lines the cron picks from.\n"
    "# One prompt per line. Lines starting with # are ignored.\n"
    "# Managed by `einkgen prompts {ls,edit,reset}` and the SPA admin tab,\n"
    "# but free to edit by hand.\n"
)

_cached: tuple[str, ...] | None = None
_cached_at: float = 0.0


def _reset_cache() -> None:
    """Drop the cached library. Used by tests."""
    global _cached, _cached_at
    _cached = None
    _cached_at = 0.0


def _parse(body: str) -> tuple[str, ...]:
    """Plain-text -> ordered tuple of prompts.

    Preserves order (so the operator can group related prompts together)
    and de-dupes by case-sensitive exact match (so "Cats" and "cats" are
    distinct prompts on purpose — capitalization is significant to the
    image model).
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return tuple(out)


def load(*, force: bool = False) -> tuple[str, ...]:
    """Return the current prompt library, refreshing from S3 if stale.

    Missing file → falls back to ``DEFAULTS`` (never returns empty).
    Empty file → returns ``DEFAULTS`` too: a library with zero entries
    would crash ``random.choice`` and isn't a state the operator can
    plausibly want.
    """
    global _cached, _cached_at
    now = time.monotonic()
    if not force and _cached is not None and (now - _cached_at) < PROMPT_LIBRARY_CACHE_TTL_SECONDS:
        return _cached
    head = s3.head_object(PROMPT_LIBRARY_KEY)
    if head is None:
        _cached = DEFAULTS
    else:
        body = s3.get_object(PROMPT_LIBRARY_KEY).decode("utf-8", errors="replace")
        parsed = _parse(body)
        _cached = parsed if parsed else DEFAULTS
    _cached_at = now
    return _cached


def write(prompts: list[str]) -> tuple[str, ...]:
    """Replace the library with ``prompts`` and return the persisted tuple.

    Entries are trimmed and de-duped (preserving first-seen order). An empty
    list is rejected — the cron needs at least one entry to pick from.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in prompts:
        if not isinstance(raw, str):
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        cleaned.append(line)
    if not cleaned:
        raise ValueError("prompt library must contain at least one entry")
    body = _HEADER + "\n".join(cleaned) + "\n"
    s3.put_object(
        PROMPT_LIBRARY_KEY,
        body.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )
    _reset_cache()
    return tuple(cleaned)


def reset_to_defaults() -> tuple[str, ...]:
    """Persist ``DEFAULTS`` to S3 and return the resulting tuple."""
    return write(list(DEFAULTS))


def random_prompt() -> str:
    """Return one entry at random from the current library."""
    return random.choice(load())
