"""Eyeball-quality check for the prompt-expansion steering.

Runs ``expand_topic`` N times against the *real* text model with the
same on-the-fly steering the cron uses (random angles per call,
running avoid-list of every prior expansion in the batch). Prints each
expansion alongside the angle tokens that were injected, so you can
scan a batch and see whether two calls on the same topic genuinely
produced different scenes.

Usage:

    OPENAI_API_KEY=sk-... \\
        uv run --extra dev python scripts/sample_expansions.py \\
            --topic "Architectural line drawing — building, bridge, or interior; technical-drawing feel." \\
            --n 8

Multiple ``--topic`` flags batch through one topic at a time. With no
``--topic`` flag, samples three library-style topics likely to suffer
mode-collapse (landmark, landscape, portrait) so a single run shows
the effect.

NOT installed as a CLI subcommand — this is a one-off operator tool
for verifying the steering quality after tweaks to the angle bag or
the system prompt, not something an end user runs.
"""

from __future__ import annotations

import argparse
import os
import sys

from einkgen.core import angles as angles_mod
from einkgen.core import generate

DEFAULT_TOPICS = (
    "Striking world landmark or world site.",
    "Striking landscape — natural place rendered as a single composition.",
    "Portrait study — single face, woodcut or charcoal feel.",
)


def _run_batch(topic: str, n: int) -> None:
    print(f"\n=== TOPIC: {topic}")
    avoid: list[str] = []
    for i in range(1, n + 1):
        chosen = angles_mod.sample_angles()
        try:
            expansion = generate.expand_topic(
                topic, angles=chosen, avoid=avoid,
            )
        except Exception as exc:  # pragma: no cover — script, not test
            print(f"  [{i}] ERROR: {exc}", file=sys.stderr)
            continue
        print(f"  [{i}] angles={chosen}")
        print(f"      {expansion}")
        avoid.insert(0, expansion)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topic", action="append", default=None,
        help="Topic string (repeatable). Defaults to 3 mode-collapse-prone ones.",
    )
    parser.add_argument(
        "--n", type=int, default=6,
        help="Expansions per topic (default 6).",
    )
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "ERROR: set OPENAI_API_KEY in env before running.",
            file=sys.stderr,
        )
        return 2

    topics = args.topic or list(DEFAULT_TOPICS)
    for topic in topics:
        _run_batch(topic, args.n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
