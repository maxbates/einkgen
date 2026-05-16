"""Manifest schema and helpers.

See ARCHITECTURE §7 for the wire format. The manifest lives at
`s3://<bucket>/current/manifest.json` and is the only thing the device
fetches on every wake.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Default display block — Inkplate 10 (see ARCHITECTURE §1).
DEFAULT_DISPLAY: dict[str, int] = {"width": 1200, "height": 825, "levels": 8}

# A tick boundary "exactly now" should round up to the *next* tick. We
# use a small epsilon to make that test stable against millisecond drift.
_TICK_EPSILON = timedelta(microseconds=1)


@dataclass
class Manifest:
    """The JSON document at `current/manifest.json`.

    Field names and order match the example in ARCHITECTURE §7. `source.model`
    and `source.prompt` may be omitted for image-kind uploads.
    """

    version: int
    generated_at: str
    image_url: str
    image_sha256: str
    image_bytes: int
    display: dict[str, int]
    next_check_after: str
    source: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=False)

    @classmethod
    def from_json(cls, data: str | bytes) -> "Manifest":
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        obj = json.loads(data)
        return cls(
            version=obj["version"],
            generated_at=obj["generated_at"],
            image_url=obj["image_url"],
            image_sha256=obj["image_sha256"],
            image_bytes=obj["image_bytes"],
            display=obj["display"],
            next_check_after=obj["next_check_after"],
            source=obj.get("source", {}),
        )


def compute_sha256(data: bytes) -> str:
    """Hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def compute_next_check_after(
    now: datetime,
    *,
    tick_interval: timedelta = timedelta(hours=1),
    buffer: timedelta = timedelta(minutes=5),
) -> datetime:
    """Return the next tick boundary after `now`, plus `buffer`.

    Tick boundaries are anchored at the Unix epoch, so for the default
    1-hour interval they fall at 00:00, 01:00, 02:00 UTC, ...

    The default is the firmware's nominal device-poll cadence. Operators
    can override per-deploy via the ``EINKGEN_POLL_INTERVAL_SECONDS``
    env var (read by ``publish.publish``) — the firmware's
    ``SLEEP_MAX_SECONDS`` must be raised in lockstep if the override
    exceeds 1 hour, or it will silently clamp.

    If `now` lies exactly on a tick (or within an epsilon of one), we
    return the *next* tick — never "right now". This keeps the device
    from waking up the instant the manifest claims it should and racing
    the next generator run.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    interval_us = int(tick_interval.total_seconds() * 1_000_000)
    if interval_us <= 0:
        raise ValueError("tick_interval must be positive")

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = now - epoch + _TICK_EPSILON
    elapsed_us = int(elapsed.total_seconds() * 1_000_000)

    ticks_passed = elapsed_us // interval_us
    next_tick_us = (ticks_passed + 1) * interval_us
    next_tick = epoch + timedelta(microseconds=next_tick_us)

    return next_tick + buffer


def iso_utc(dt: datetime) -> str:
    """Format `dt` as an ISO 8601 UTC string with a trailing Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # Strip microseconds — manifests are read by humans and firmware.
    dt = dt.replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
