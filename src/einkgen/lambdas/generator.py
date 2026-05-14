"""``einkgen-generator`` Lambda — drains the queue and handles the 2h cron.

Two triggers:

- **EventBridge ``rate(2 hours)``** (cron) — if the queue is empty, enqueue
  a ``random`` item. The S3 ObjectCreated event from that put re-invokes
  this Lambda and the same code path drains it. If the queue is non-empty,
  it's a no-op: pending items have priority.
- **S3 ObjectCreated** on ``queue/`` — drain pending items.

Reserved concurrency = 1 keeps drains serial. We drain to empty per
invocation because S3 ObjectCreated events can batch multiple records,
and a single delivery that only processes the first record would leak
the rest until the next cron tick.

The drain follows a *peek → process → finalize* pattern so a mid-pipeline
failure leaves the item on the queue; Lambda's async-invoke retry then
redelivers and we try again. Items that ultimately fail end up in the
configured DLQ (if any) after Lambda exhausts retries.
"""

from __future__ import annotations

import logging
from typing import Any

from einkgen.core import pipeline, queue

log = logging.getLogger(__name__)

# Safety cap so a pathological queue can't pin the Lambda forever.
# Reserved concurrency = 1 means subsequent S3 events queue inside Lambda;
# capping per-invocation drain bounds wall-clock time. Set this higher than
# any realistic burst.
MAX_ITEMS_PER_INVOCATION = 16


def _is_cron_event(event: dict[str, Any]) -> bool:
    if event.get("source") == "aws.events":
        return True
    if event.get("detail-type") == "Scheduled Event":
        return True
    return False


def handler(event: dict[str, Any], context: Any = None) -> None:
    if _is_cron_event(event):
        if queue.empty():
            queue.enqueue("random", source="cron")
        # Either way: cron itself never processes; the S3 event does.
        return

    # Any non-cron invocation: drain until empty (or until the safety cap).
    # We don't switch on ``event["Records"]`` — the queue itself is the
    # source of truth, and a single event may correspond to multiple
    # records anyway.
    drained = 0
    while drained < MAX_ITEMS_PER_INVOCATION:
        item = queue.peek_head()
        if item is None:
            return
        pipeline.process_item(item)
        # Only finalize after process_item succeeds; if it raised, Lambda
        # retries the whole invocation and we'll try this same item again.
        queue.finalize(item)
        drained += 1

    if not queue.empty():
        log.info(
            "hit per-invocation drain cap (%d); remaining items will be picked up "
            "by the next S3 event or cron tick",
            MAX_ITEMS_PER_INVOCATION,
        )
