"""`einkgen history` — list recent published frames, newest first."""

from __future__ import annotations

import argparse
import json

from einkgen.core import s3

HISTORY_PREFIX = "history/"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("history", help="List recent published frames.")
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of entries to print (default: 20).",
    )
    p.set_defaults(func=run)


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "…"


def run(args: argparse.Namespace) -> int:
    objects = s3.list_objects(HISTORY_PREFIX)
    manifest_keys = [
        o["Key"] for o in objects
        if o["Key"].endswith("/manifest.json")
    ]

    entries: list[dict] = []
    for key in manifest_keys:
        try:
            body = s3.get_object(key)
            data = json.loads(body)
        except Exception:
            continue
        # key looks like `history/<id>/manifest.json`
        parts = key.split("/")
        item_id = parts[1] if len(parts) >= 3 else "?"
        entries.append(
            {
                "id": item_id,
                "generated_at": data.get("generated_at", ""),
                "source": data.get("source", {}) or {},
            }
        )

    entries.sort(key=lambda e: e["generated_at"], reverse=True)

    if not entries:
        print("No history entries found.")
        return 0

    for entry in entries[: args.limit]:
        source = entry["source"]
        kind = source.get("kind", "?")
        descr = source.get("prompt") or source.get("image_s3_key") or ""
        print(
            f"{entry['generated_at']}  {entry['id']}  {kind}  {_truncate(descr, 60)}"
        )
    return 0
