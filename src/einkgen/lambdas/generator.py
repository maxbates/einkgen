"""``einkgen-generator`` Lambda — cron-driven top-up + admin overrides.

Triggers
--------
- **EventBridge cron** (rate driven by ``einkgenPollIntervalSeconds``
  in ``infra/cdk.json`` — default 30 min). Each tick:

    1. ``_top_up_prompt_queue()`` — if the prompt queue holds fewer
       than ``TARGET_PROMPT_QUEUE_LENGTH`` pending items, pick that
       many topics from the operator-editable prompt library, run each
       through ``generate.expand_topic()`` (text LLM) to turn the topic
       into a concrete image prompt, and enqueue the expansions at the
       bottom. Keeps the generated-queue refill step well-fed without
       requiring an admin to top the prompt library up by hand.

    2. ``_top_up_generated_queue()`` — while the generated queue holds
       fewer than ``TARGET_GENERATED_QUEUE_LENGTH`` markers, render
       prompts into the buffer. Each iteration tops the prompt queue
       up inline (so a deeply drained buffer can refill in one tick
       without running out of prompts mid-loop). The natural exit
       condition is "buffer at target" — there's no separate per-tick
       cap. ``MAX_RENDERS_PER_TICK`` is kept only as a defensive
       safety bound. Steady-state cron does 0–1 renders per tick
       because each ``/wake`` advance fires its own replenish.

    Cron does NOT touch ``current/manifest.json``. The device only sees
    a new frame once a ``/wake`` call pops the head of the generated
    queue and points current at it.

- **Direct invocation**, three actions — all fired by either the
  admin API or the device-status (``/wake``) Lambda via
  ``lambda:Invoke`` with ``InvocationType=Event`` so the HTTP request
  returns immediately:

    - ``{"action": "render_one"}`` — render the head of the prompt
      queue into the generated-queue buffer (no display advance). Used
      by ``/wake`` to replenish after a pop so the buffer stays at
      ``TARGET_GENERATED_QUEUE_LENGTH`` in steady state.

    - ``{"action": "render_now"}`` — render the current head AND set
      it as current. Used by the admin **Now** button on a new
      submission (the new item lands at the top of the prompt queue
      first, then this is fired). Skips the generated queue buffer.

    - ``{"action": "render_item", "item_id": "..."}`` — render the
      *specific* item with that id AND set it as current. Used by the
      per-row **Run** button. Skips the generated queue buffer.

There is **no S3 ObjectCreated drain**. Items sit on the prompt queue
until either cron renders them or an operator triggers
``render_now``/``render_item``. Reserved concurrency = 1 keeps every
render serial — including overlapping cron + admin + wake-driven
replenishment.
"""

from __future__ import annotations

import logging
from typing import Any

from einkgen.core import failures, generate, pipeline, prompt_library, queue
from einkgen.core.pipeline import PermanentItemError
from einkgen.core.queue import QueueItem

log = logging.getLogger(__name__)

# Floor for the prompt queue. The cron tops up to this depth each tick
# via text-LLM expansion of random library topics. The buffer-refill
# loop also tops up inline whenever it runs dry, so this can stay
# small even though a deep cold-start refill drains 10 items.
TARGET_PROMPT_QUEUE_LENGTH = 5

# Backwards-compat alias for older callers / tests that reference the
# pre-buffer name. New code should use TARGET_PROMPT_QUEUE_LENGTH.
TARGET_QUEUE_LENGTH = TARGET_PROMPT_QUEUE_LENGTH

# Hard ceiling on prompt-queue text-expansions per single call to
# ``_top_up_prompt_queue``. The buffer loop calls it repeatedly, so the
# total per-tick text-LLM cost is bounded by how many renders cron does
# (i.e. by ``TARGET_GENERATED_QUEUE_LENGTH``) — this just prevents a
# single call from running away.
MAX_PROMPT_TOP_UP_PER_TICK = TARGET_PROMPT_QUEUE_LENGTH

# Floor for the generated queue — the pre-rendered buffer the device
# draws from. 10 items so the SPA can preview the next ~5 hours of
# panel content at the 30-min default cadence; the device pops one at
# each wake and ``/wake`` fires a single render_one to backfill.
TARGET_GENERATED_QUEUE_LENGTH = 10

# Defensive safety bound on image renders in a single cron tick. The
# natural exit condition of ``_top_up_generated_queue`` is "buffer at
# target", so this almost never bites — it's here so a pathological bug
# (e.g. ``buffer_item`` somehow not incrementing the buffer count) can't
# spin the Lambda into a timeout-killed loop burning OpenAI cost the
# whole way. Set comfortably above ``TARGET_GENERATED_QUEUE_LENGTH``
# since a real cold-start fill needs exactly that many. Each render is
# ~55 s; the Lambda timeout (15 min in CDK) caps total runtime to 15
# renders even if this bound were removed entirely.
MAX_RENDERS_PER_TICK = TARGET_GENERATED_QUEUE_LENGTH + 5


def _is_cron_event(event: dict[str, Any]) -> bool:
    if event.get("source") == "aws.events":
        return True
    if event.get("detail-type") == "Scheduled Event":
        return True
    return False


def _process(item: QueueItem, mode: str) -> None:
    """Run the chosen render flow on ``item``; drop on permanent failure.

    ``mode`` selects the pipeline entrypoint:

    - ``"buffer"`` — archive + enqueue a generated-queue marker. The
      cron + replenish path.
    - ``"publish"`` — archive + set as current. The admin override
      path (Now / Run).

    A ``PermanentItemError`` (e.g. OpenAI moderation_blocked on a prompt
    the safety system will never accept) means retrying is hopeless —
    finalize so the head can advance and record a breadcrumb the Admin
    tab can surface. Any other exception propagates and Lambda's
    async-invoke retry redelivers the event (but ``retryAttempts=0`` in
    infra so the redelivery is a no-op in practice).
    """
    if mode == "buffer":
        render = pipeline.buffer_item
    elif mode == "publish":
        render = pipeline.publish_item
    else:
        raise ValueError(f"unknown render mode: {mode!r}")
    try:
        render(item)
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
        _render_head_to_current()
        return
    if action == "render_item":
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            log.warning("generator: render_item event missing item_id: %s", event)
            return
        _render_item_by_id_to_current(item_id)
        return
    if action == "render_one":
        _render_one_into_buffer()
        return

    if _is_cron_event(event):
        # Buffer-refill is the heavy step. It tops the prompt queue up
        # inline whenever it runs dry so a deep cold-start deficit fills
        # in one tick. The trailing top-up call leaves the prompt queue
        # at its floor afterwards — purely so the SPA's "pending prompts"
        # section shows a sensible non-zero count between cron ticks.
        _top_up_generated_queue()
        _top_up_prompt_queue()
        return

    # Anything else — including stray legacy S3 ObjectCreated events
    # delivered after the trigger was removed but before all in-flight
    # notifications drained — is logged and ignored. We deliberately do
    # NOT fall back to draining the queue on unknown events: this Lambda
    # only renders when explicitly asked to.
    log.info("generator: ignoring unrecognised event shape: %s", event)


def _top_up_prompt_queue() -> None:
    """Ensure the prompt queue holds at least ``TARGET_PROMPT_QUEUE_LENGTH`` items.

    Each missing slot is filled by picking a topic from the prompt
    library and asking the text LLM to expand it into a concrete image
    prompt. Failures fall back to enqueueing the raw topic so the queue
    still fills — better a less-detailed prompt than a stalled queue.
    """
    current = queue.count()
    if current >= TARGET_PROMPT_QUEUE_LENGTH:
        return

    needed = min(TARGET_PROMPT_QUEUE_LENGTH - current, MAX_PROMPT_TOP_UP_PER_TICK)
    log.info(
        "generator: topping up prompt queue from %d to %d (+%d)",
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


def _top_up_generated_queue() -> None:
    """Render prompts into the generated buffer until it reaches target.

    Each iteration archives one frame under ``history/<id>/`` and
    enqueues a marker on ``core.generated_queue``. The device sees
    nothing until a ``/wake`` call promotes a marker to current.

    Tops the prompt queue back up inline whenever it runs dry, so a
    deeply drained buffer (e.g. wake button drained the buffer faster
    than per-pop replenish kept up, or a fresh deploy) can refill the
    full ``TARGET_GENERATED_QUEUE_LENGTH`` in a single tick. Each render
    is ~55 s; the Lambda timeout (15 min in CDK) comfortably fits a
    full refill from zero.

    Exit conditions, in priority order:
      1. Buffer at ``TARGET_GENERATED_QUEUE_LENGTH``.
      2. ``MAX_RENDERS_PER_TICK`` safety bound hit (should never
         happen — it's set above the buffer target).
      3. Prompt queue cannot be replenished (every ``expand_topic``
         failed and the fallback raw-topic enqueue also failed).
    """
    from einkgen.core import generated_queue

    rendered = 0
    while rendered < MAX_RENDERS_PER_TICK:
        if generated_queue.count() >= TARGET_GENERATED_QUEUE_LENGTH:
            return
        # Re-fill the prompt queue any time it runs dry mid-loop. Cheap
        # text-LLM calls — the expensive thing is the image render below.
        if queue.empty():
            _top_up_prompt_queue()
        head = queue.peek_head()
        if head is None:
            log.info(
                "generator: prompt queue couldn't be refilled, stopping buffer top-up"
            )
            return
        log.info(
            "generator: buffering id=%s kind=%s source=%s (buffer at %d / target %d)",
            head.id, head.kind, head.source,
            generated_queue.count(), TARGET_GENERATED_QUEUE_LENGTH,
        )
        _process(head, mode="buffer")
        rendered += 1


def _render_one_into_buffer() -> None:
    """Render exactly one prompt-queue head into the generated buffer.

    Fired by ``/wake`` after it pops a marker so the buffer is back at
    ``TARGET_GENERATED_QUEUE_LENGTH`` by the next device wake. A no-op
    if the prompt queue is empty — cron's next tick will refill it.

    Caps at ``TARGET_GENERATED_QUEUE_LENGTH``: if the buffer is already
    at or above target (e.g. cron just finished a cold-start refill
    while wake events queued up behind reserved-concurrency=1), skip
    the render. Without this cap, a wake-button mash queues N
    ``render_one`` events behind the in-flight invocation and they all
    fire serially after cron completes, blowing the buffer past target
    and burning N image renders that aren't needed.
    """
    from einkgen.core import generated_queue

    if generated_queue.count() >= TARGET_GENERATED_QUEUE_LENGTH:
        log.info(
            "generator: render_one — buffer already at target (%d), skipping",
            generated_queue.count(),
        )
        return
    head = queue.peek_head()
    if head is None:
        log.info("generator: render_one — prompt queue empty, nothing to buffer")
        return
    log.info(
        "generator: render_one id=%s kind=%s source=%s",
        head.id, head.kind, head.source,
    )
    _process(head, mode="buffer")


def _render_head_to_current() -> None:
    """Admin **Now** path — render the prompt-queue head and set as current.

    Bypasses the generated buffer so the operator sees their submission
    on the panel immediately (not after the buffer has drained).
    """
    head = queue.peek_head()
    if head is None:
        log.info("generator: render_now — prompt queue empty, nothing to render")
        return
    log.info(
        "generator: render_now id=%s kind=%s source=%s",
        head.id, head.kind, head.source,
    )
    _process(head, mode="publish")


def _render_item_by_id_to_current(item_id: str) -> None:
    """Admin **Run** path — render a specific item and set as current.

    Same bypass as ``render_now``: the operator picked this item
    explicitly, so put it on the panel directly rather than tacking it
    onto the end of the buffer.

    If the id doesn't match any pending item — e.g. the cron drained
    it between the click and the invocation — log and return without
    raising.
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
    _process(item, mode="publish")
