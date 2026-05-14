"""``einkgen-generator`` Lambda — drains the queue and handles the 2h cron.

Two triggers:

- **EventBridge ``rate(2 hours)``** (cron) — if the queue is empty, enqueue
  a ``random`` item. The S3 ObjectCreated event from that put re-invokes
  this Lambda and the same code path drains it. If the queue is non-empty,
  it's a no-op: pending items have priority.
- **S3 ObjectCreated** on ``queue/`` — pop the head and process it.

Reserved concurrency = 1 keeps drains serial.
"""

from __future__ import annotations

from typing import Any

from einkgen.core import pipeline, queue


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

    # S3 ObjectCreated (or any other non-cron invocation): drain one item.
    item = queue.pop_head()
    if item is None:
        return
    pipeline.process_item(item)
