"""One queue item -> a rendered frame.

Two render flows share the generate-and-convert front half:

- ``buffer_item(item)`` archives the frame under ``history/<id>/`` and
  enqueues a marker in the generated queue (``core.generated_queue``).
  The device only sees the frame once a ``/wake`` advance pops the
  marker and re-points ``current/manifest.json`` at it. This is the
  cron-driven path.

- ``publish_item(item)`` archives the frame AND immediately points
  ``current/manifest.json`` at it (legacy ``publish``). Used by the
  admin **Now** / **Run** overrides where the operator wants the image
  on the panel right away rather than through the buffer.

Both flows handle the ``prompt`` / ``image`` / ``image+prompt`` /
``random`` cases via the same ``_render(item)`` helper. Lazy-imports
``generate`` / ``convert`` / ``publish`` so this module loads cleanly
in worktrees where those siblings have not been written yet (and so
tests can inject mocks via ``sys.modules``).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from einkgen.core import generated_queue, s3
from einkgen.core.queue import QueueItem

log = logging.getLogger(__name__)


class PermanentItemError(Exception):
    """An item can never succeed and must be dropped from the queue.

    Raised when the upstream model rejects the request in a way retrying
    cannot recover from — most commonly OpenAI's safety system returning
    ``moderation_blocked`` (HTTP 400). Lambda's async-invoke retry treats
    every exception as transient, so without this signal a single blocked
    prompt pins the head of the queue forever. The generator handler
    finalizes the item when it sees this.
    """


# OpenAI 400 codes that mean "this *specific* prompt/image will never
# succeed" — i.e. retrying the same input is hopeless. Everything else
# (billing cap, quota, auth, account state) is account-level state the
# operator can fix, and the item should survive so it gets retried on
# the next cron tick. Keep this set narrow on purpose: when in doubt,
# err transient — preserving a queue item is cheap, destroying a
# user-submitted prompt is not.
_PERMANENT_OPENAI_CODES = frozenset(
    {
        "moderation_blocked",
        "content_policy_violation",
        "invalid_image",
        "invalid_image_format",
        "invalid_image_url",
        "unsupported_image_format",
    }
)


def _is_permanent_openai_error(exc: BaseException) -> bool:
    """True iff the BadRequestError carries an item-specific code.

    Checks both the structured ``code`` attribute (preferred) and falls
    back to substring matching on ``str(exc)`` for SDK versions / error
    shapes where the attribute isn't populated. Both are best-effort —
    when in doubt we return False (transient) so the item survives.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _PERMANENT_OPENAI_CODES:
        return True
    message = str(exc)
    return any(c in message for c in _PERMANENT_OPENAI_CODES)


def _call_openai(fn, *args, **kwargs):
    """Invoke an OpenAI call, translating only *item-specific* 400s.

    ``openai.BadRequestError`` is HTTP 400 — used for BOTH item-specific
    failures (moderation, invalid image format) AND account-level
    failures (``billing_hard_limit_reached``). The two need opposite
    handling: item-specific = drop the item (PermanentItemError);
    account-level = leave the item in the queue and let the next cron
    tick retry once the operator lifts the cap / adds credit.

    Reclassifying account-level errors as transient is essential —
    otherwise a single cap-hit cron tick silently destroys whatever
    user-submitted prompts happen to be at the head of the queue
    (this bit us in production; see CHANGELOG 0.6.6.0).

    Lazy import so this module stays importable when ``openai`` isn't
    installed (tests stub the ``generate`` module via ``sys.modules``).
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        try:
            from openai import BadRequestError
        except ImportError:
            raise exc
        if isinstance(exc, BadRequestError) and _is_permanent_openai_error(exc):
            raise PermanentItemError(str(exc)) from exc
        raise


def _render(item: QueueItem) -> tuple[bytes, bytes, dict[str, Any], bool]:
    """Generate or fetch the source, convert to dithered BMP.

    Returns ``(processed_bmp, original_png, source_dict, image_was_generated)``.
    Caller is responsible for archiving / publishing and for cleaning up
    the staged upload (for image-kind items).
    """
    generate = importlib.import_module("einkgen.core.generate")
    convert_mod = importlib.import_module("einkgen.core.convert")

    original_png: bytes
    if item.kind == "prompt":
        # BASE_PROMPT is prepended inside generate.generate.
        original_png = _call_openai(generate.generate, item.prompt)
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
            original_png = _call_openai(
                generate.generate_from_image,
                item.prompt,
                uploaded,
                image_filename=filename,
            )
        else:
            original_png = uploaded
    elif item.kind == "random":
        prompt = generate.random_prompt()
        item.prompt = prompt  # so publish/manifest can record the chosen subject
        original_png = _call_openai(generate.generate, prompt)
    else:
        raise ValueError(f"unknown kind: {item.kind!r}")

    # "uploaded" means the published frame is the user's bytes (passed through
    # B&W only). When an image is restyled via the image model, it's a generated
    # frame — record it as such so history shows the model that touched it.
    image_was_generated = item.kind != "image" or bool(item.prompt)

    # Generated images are 1200x832 composed for the whole canvas, so they
    # center-crop a 7-pixel sliver off the height without resampling. Raw
    # uploads can be any size — let convert() scale-fill them so the panel
    # fills, accepting a small crop on the long axis instead of leaving white
    # bars.
    processed_bmp = convert_mod.convert(original_png, is_generated=image_was_generated)
    source: dict[str, Any] = {
        "kind": "generated" if image_was_generated else "uploaded",
    }
    # ARCHITECTURE §7 says model/prompt may be omitted for image-kind uploads
    # that are passed through unchanged. A restyled image is a generated frame,
    # so it does carry model/prompt.
    if image_was_generated:
        source["model"] = generate.MODEL
    if item.prompt is not None:
        source["prompt"] = item.prompt

    return processed_bmp, original_png, source, image_was_generated


def _cleanup_staged(item: QueueItem) -> None:
    """Best-effort delete of ``queue/staged/<…>`` after a successful render."""
    if item.kind == "image" and item.image_s3_key:
        try:
            s3.delete_object(item.image_s3_key)
        except Exception:  # pragma: no cover - best-effort cleanup
            log.warning("failed to delete staged image %s", item.image_s3_key)


def buffer_item(item: QueueItem) -> None:
    """Render → ``history/<id>/`` → enqueue marker on the generated queue.

    The cron-driven render path. The device only sees this frame after a
    ``/wake`` advance pops the marker.
    """
    publish_mod = importlib.import_module("einkgen.core.publish")
    processed_bmp, original_png, source, _ = _render(item)
    manifest = publish_mod.archive_to_history(
        processed_bmp,
        source=source,
        item_id=item.id,
        original=original_png,
        prompt=item.prompt,
    )
    generated_queue.enqueue(
        item.id,
        image_sha256=manifest.image_sha256,
        image_bytes=manifest.image_bytes,
        source=manifest.source,
    )
    _cleanup_staged(item)


def publish_item(item: QueueItem) -> None:
    """Render → ``history/<id>/`` + set as current in one shot.

    Used by admin "Now" / "Run" overrides where the operator wants the
    image on the panel immediately, skipping the generated queue.
    """
    publish_mod = importlib.import_module("einkgen.core.publish")
    processed_bmp, original_png, source, _ = _render(item)
    publish_mod.publish(
        processed_bmp,
        source=source,
        item_id=item.id,
        original=original_png,
        prompt=item.prompt,
    )
    _cleanup_staged(item)


# Back-compat alias for the historical public name. Callers that just want the
# "archive + immediately set current" behavior (admin Now / Run, older tests)
# can keep using ``process_item``. The cron path uses ``buffer_item`` directly.
process_item = publish_item
