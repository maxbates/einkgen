"""Read-side helpers over ``history/<id>/manifest.json``.

The CLI (`einkgen history`) and the read-api Lambda both already do
their own listing; this module exists for *internal* callers that need
recent prompt text and optional subject/medium metadata to keep the
automatic art rotation from repeating recent frames.

Kept slim and side-effect-free: no caching, no formatting, no SPA
shape. Callers compose.
"""

from __future__ import annotations

import json
import logging

from einkgen.core import s3

log = logging.getLogger(__name__)

HISTORY_PREFIX = "history/"


def recent_prompt_sources(limit: int = 30) -> list[dict]:
    """Return sources from the newest history manifests with prompts.

    Newest first. Items without a prompt (e.g. raw-image submissions
    archived without a textual prompt field) are skipped silently.
    Malformed manifests log + skip rather than raising so a single
    corrupted archive can't take the cron's expansion path down.

    Bound on cost: ``limit`` GetObject calls plus one ListObjectsV2
    page-walk. We rely on ULID ids being time-monotonic so a
    reverse-lex sort of the keys is already newest-first — same trick
    the read-api uses.
    """
    if limit <= 0:
        return []
    manifest_keys = [
        obj["Key"]
        for obj in s3.list_objects(HISTORY_PREFIX)
        if obj["Key"].endswith("/manifest.json")
    ]
    manifest_keys.sort(reverse=True)

    sources: list[dict] = []
    for key in manifest_keys:
        if len(sources) >= limit:
            break
        try:
            data = json.loads(s3.get_object(key))
        except Exception:
            log.warning("recent_prompts: skipping unreadable manifest %s", key)
            continue
        source = data.get("source") if isinstance(data, dict) else None
        if not isinstance(source, dict):
            continue
        prompt = source.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            sources.append(source)
    return sources


def recent_prompts(limit: int = 30) -> list[str]:
    """Return prompt text from recent history, newest first."""
    return [source["prompt"].strip() for source in recent_prompt_sources(limit)]
