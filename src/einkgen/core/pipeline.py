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
        uploaded = s3.get_object(item.image_s3_key)
        if item.prompt:
            # Image + prompt: feed both to the edit endpoint so the prompt
            # restyles the upload. Filename hint comes from the staged key
            # so the SDK picks a sane MIME based on extension.
            import os as _os

            filename = _os.path.basename(item.image_s3_key) or "input.png"
            original_png = generate.generate_from_image(
                item.prompt, uploaded, image_filename=filename
            )
        else:
            original_png = uploaded
    elif item.kind == "random":
        prompt = generate.random_prompt()
        item.prompt = prompt  # so publish/manifest can record the chosen subject
        original_png = generate.generate(prompt)
    else:
        raise ValueError(f"unknown kind: {item.kind!r}")

    processed_bmp = convert_mod.convert(original_png)

    # "uploaded" means the published frame is the user's bytes (passed through
    # B&W only). When an image is restyled via gpt-image-1, it's a generated
    # frame — record it as such so history shows the model that touched it.
    image_was_generated = item.kind != "image" or bool(item.prompt)
    source: dict[str, Any] = {
        "kind": "generated" if image_was_generated else "uploaded",
    }
    # ARCHITECTURE §7 says model/prompt may be omitted for image-kind uploads
    # that are passed through unchanged. A restyled image is a generated frame,
    # so it does carry model/prompt.
    if image_was_generated:
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
