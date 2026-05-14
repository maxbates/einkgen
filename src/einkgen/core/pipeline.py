"""One queue item -> a published frame.

Lazy-imports ``generate``, ``convert``, ``publish`` so this module loads cleanly
in worktrees where those siblings have not been written yet (and so tests can
inject mocks via ``sys.modules``).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from einkgen.core import s3
from einkgen.core.queue import QueueItem

log = logging.getLogger(__name__)


def process_item(item: QueueItem) -> None:
    """Generate (or fetch) -> convert -> publish.

    For ``image`` kind, the staged source is removed from S3 after a
    successful publish so ``queue/staged/`` does not grow unboundedly.
    """
    generate = importlib.import_module("einkgen.core.generate")
    convert_mod = importlib.import_module("einkgen.core.convert")
    publish_mod = importlib.import_module("einkgen.core.publish")

    original_png: bytes
    if item.kind == "prompt":
        # BASE_PROMPT is prepended inside generate.generate.
        original_png = generate.generate(item.prompt)
    elif item.kind == "image":
        if not item.image_s3_key:
            raise ValueError(f"image item {item.id} has no image_s3_key")
        original_png = s3.get_object(item.image_s3_key)
    elif item.kind == "random":
        prompt = generate.random_prompt()
        item.prompt = prompt  # so publish/manifest can record the chosen subject
        original_png = generate.generate(prompt)
    else:
        raise ValueError(f"unknown kind: {item.kind!r}")

    processed_bmp = convert_mod.convert(original_png)

    source: dict[str, Any] = {
        "kind": "generated" if item.kind != "image" else "uploaded",
    }
    # README §7 says model/prompt may be omitted for image-kind uploads.
    if item.kind != "image":
        source["model"] = "gpt-image-1"
    if item.prompt is not None:
        source["prompt"] = item.prompt

    publish_mod.publish(
        processed_bmp,
        source=source,
        item_id=item.id,
        original=original_png,
        prompt=item.prompt,
    )

    # Clean up the staged upload now that history/<id>/original.png is the
    # canonical archive. Best-effort: a failure here doesn't roll back the
    # published frame.
    if item.kind == "image" and item.image_s3_key:
        try:
            s3.delete_object(item.image_s3_key)
        except Exception:  # pragma: no cover - best-effort cleanup
            log.warning("failed to delete staged image %s", item.image_s3_key)
