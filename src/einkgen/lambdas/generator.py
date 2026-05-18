"""``einkgen-generator`` Lambda — cron-driven render + queue top-up.

Triggers
--------
- **EventBridge cron** (rate driven by ``einkgenPollIntervalSeconds``
  in ``infra/cdk.json`` — default 30 min) — every tick:

    1. ``top_up_queue()`` — if the queue holds fewer than
       ``TARGET_QUEUE_LENGTH`` pending items, pick that many topics from
       the operator-editable prompt library, run each through
       ``generate.expand_topic()`` (text LLM) to turn the topic into a
       concrete image prompt, and enqueue the expansions at the bottom.
       This keeps the queue continuously full so the operator always has
       something to reorder / inspect / drop without the panel going
       stale.

    2. ``_render_head()`` — pop the lowest-position item, generate the
       image, publish.

- **Direct invocation**, two variants — both fired by the admin API via
  ``lambda:Invoke`` with ``InvocationType=Event`` so the HTTP request
  returns immediately:

    - ``{"action": "render_now"}`` — render the current head exactly
      once. Used by the **Now** button on a new submission (the new
      item lands at the top of the queue first, then this is fired).

    - ``{"action": "render_item", "item_id": "..."}`` — render the
      *specific* item with that id, regardless of its current queue
      position. Used by the per-row **Run** button so the operator can
      execute any pending item without first having to promote it to
      the head. The item is finalized (deleted from the queue) on
      success; if it has already been drained when the invocation
      lands, the handler logs and returns.

There is **no S3 ObjectCreated drain** anymore. Items sit on the queue
until either the cron tick renders them or an operator triggers
``render_now``. That's the entire point of the redesign: the queue is a
buffer the operator can curate, not a fire-and-forget pipe. New
submissions from CLI / email don't auto-render — they'll go out on the
next cron tick, or the operator can hit **Run** in the SPA.

Reserved concurrency = 1 keeps everything serial, including overlapping
cron + admin invocations: a "Now" request fired while the cron is mid-
render queues behind it and runs as soon as the cron returns.
"""

from __future__ import annotations

import logging
from typing import Any

from einkgen.core import failures, generate, pipeline, prompt_library, queue
from einkgen.core.pipeline import PermanentItemError
from einkgen.core.queue import QueueItem

log = logging.getLogger(__name__)

# Steady-state target queue depth. The cron tops up to this floor each
# tick, so an operator opening the dashboard always sees a handful of
# pending items to reorder or run. Topping up beyond this is fine — the
# operator can manually enqueue more — and the cron just won't add more
# until depth drops back below the floor.
TARGET_QUEUE_LENGTH = 5

# Hard ceiling on text-expansions per cron tick. Prevents a pathological
# (negative count, repeated failures) state from running away. In normal
# operation the cron tops up exactly 1 item per tick (steady state) and
# at most TARGET_QUEUE_LENGTH on first deploy.
MAX_TOP_UP_PER_TICK = TARGET_QUEUE_LENGTH


def _is_cron_event(event: dict[str, Any]) -> bool:
    if event.get("source") == "aws.events":
        return True
    if event.get("detail-type") == "Scheduled Event":
        return True
    return False


def _process_or_drop(item: QueueItem) -> None:
    """Run the pipeline; drop the item if it will never succeed.

    A ``PermanentItemError`` (e.g. OpenAI moderation_blocked on a prompt
    the safety system will never accept) means retrying is hopeless —
    finalize so the head can advance. Any other exception propagates and
    Lambda's async-invoke retry redelivers the event.
    """
    try:
        pipeline.process_item(item)
    except PermanentItemError as exc:
        log.error(
            "dropping queue item %s (%s) — permanent failure: %s",
            item.id,
            item.kind,
            exc,
        )
        # Best-effort operator-visible breadcrumb. If the write fails the
        # queue still advances — the failure_record is a notification, not
        # a queue primitive.
        failures.record(item, str(exc))
        queue.finalize(item)
        return
    queue.finalize(item)


def handler(event: dict[str, Any], context: Any = None) -> None:
    action = event.get("action")
    if action == "render_now":
        _render_head()
        return
    if action == "render_item":
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            log.warning("generator: render_item event missing item_id: %s", event)
            return
        _render_item_by_id(item_id)
        return

    if _is_cron_event(event):
        _top_up_queue()
        _render_head()
        return

    # Anything else — including stray legacy S3 ObjectCreated events
    # delivered after the trigger was removed but before all in-flight
    # notifications drained — is logged and ignored. We deliberately do
    # NOT fall back to draining the queue on unknown events: this Lambda
    # only renders when explicitly asked to (cron tick, render_now,
    # render_item).
    log.info("generator: ignoring unrecognised event shape: %s", event)


def _top_up_queue() -> None:
    """Ensure the queue holds at least ``TARGET_QUEUE_LENGTH`` items.

    Each missing slot is filled by picking a topic from the prompt
    library and asking the text LLM to expand it into a concrete image
    prompt. Failures fall back to enqueueing the raw topic so the queue
    still fills — better a less-detailed prompt than a stalled queue.
    """
    current = queue.count()
    if current >= TARGET_QUEUE_LENGTH:
        return

    needed = min(TARGET_QUEUE_LENGTH - current, MAX_TOP_UP_PER_TICK)
    log.info(
        "generator: topping up queue from %d to %d (+%d)",
        current, current + needed, needed,
    )
    for _ in range(needed):
        topic = prompt_library.random_prompt()
        try:
            expanded = generate.expand_topic(topic)
        except Exception:
            log.exception(
                "ERROR generator: expand_topic failed, enqueueing raw topic: %r",
                topic,
            )
            expanded = topic
        try:
            queue.enqueue("prompt", prompt=expanded, source="cron")
        except Exception:
            # An enqueue failure (S3 throttle, IAM blip) shouldn't halt
            # the render step below. Log and move on.
            log.exception("ERROR generator: enqueue during top-up failed")


def _render_head() -> None:
    """Render the current head item, if any.

    The pipeline already handles all three kinds (prompt / image /
    random) — ``random`` is preserved for items enqueued before the
    cron redesign; new items use ``prompt`` with an expanded subject.
    """
    head = queue.peek_head()
    if head is None:
        log.info("generator: queue empty, nothing to render")
        return
    log.info(
        "generator: rendering head id=%s kind=%s source=%s",
        head.id, head.kind, head.source,
    )
    # _process_or_drop handles the PermanentItemError → record + finalize
    # path so a moderation-blocked prompt at the head can't pin the queue.
    # Retryable failures still propagate and Lambda redelivers.
    _process_or_drop(head)


def _render_item_by_id(item_id: str) -> None:
    """Render a specific pending item, regardless of its queue position.

    Used by the admin **Run** button (`POST /admin/queue/<id>/run` →
    `lambda.invoke` with ``{"action": "render_item", "item_id": ...}``)
    so an operator can execute any pending item without having to first
    promote it to the head. The item is deleted on success.

    If the id doesn't match any pending item — e.g. the cron drained
    it between the click and the invocation — log and return without
    raising. The HTTP request already returned 202 by the time we get
    here, so the only thing this would accomplish is a Lambda retry
    that can never succeed.
    """
    item = queue.get(item_id)
    if item is None:
        log.info(
            "generator: render_item id=%s already drained or never existed, skipping",
            item_id,
        )
        return
    log.info(
        "generator: render_item id=%s kind=%s source=%s",
        item.id, item.kind, item.source,
    )
    # Same permanent-vs-retryable distinction as the cron head render.
    _process_or_drop(item)
