"""``einkgen queue {ls,rm,prompt,image}`` CLI."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from einkgen.core import queue as queue_core
from einkgen.core import s3

PROMPT_PREVIEW = 60


def register(subparsers: argparse._SubParsersAction) -> None:
    queue_parser = subparsers.add_parser("queue", help="manage the pending image queue")
    qsub = queue_parser.add_subparsers(dest="queue_command", required=True)

    ls = qsub.add_parser("ls", help="list pending items")
    ls.set_defaults(func=_cmd_ls)

    rm = qsub.add_parser("rm", help="cancel a pending item by id")
    rm.add_argument("id")
    rm.set_defaults(func=_cmd_rm)

    pr = qsub.add_parser("prompt", help="enqueue a text prompt")
    pr.add_argument("text")
    pr.set_defaults(func=_cmd_prompt)

    im = qsub.add_parser("image", help="enqueue a local image file")
    im.add_argument("path")
    im.add_argument(
        "--prompt",
        help="optional restyle prompt; if set, the image is fed to gpt-image-2's "
        "edit endpoint instead of being passed through B&W only",
    )
    im.set_defaults(func=_cmd_image)


def _truncate(text: str, n: int = PROMPT_PREVIEW) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _cmd_ls(args: argparse.Namespace) -> int:
    items = queue_core.list()
    if not items:
        print("(queue empty)")
        return 0
    for item in items:
        detail = (
            _truncate(item.prompt or "")
            if item.kind in ("prompt", "random")
            else (item.image_s3_key or "")
        )
        print(f"{item.id}  {item.enqueued_at}  {item.kind}  {detail}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    if queue_core.cancel(args.id):
        print(f"cancelled {args.id}")
        return 0
    print(f"no pending item found for {args.id}", file=sys.stderr)
    return 1


def _cmd_prompt(args: argparse.Namespace) -> int:
    item = queue_core.enqueue("prompt", prompt=args.text)
    print(item.id)
    return 0


def _cmd_image(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1
    data = path.read_bytes()
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    staged_key = f"{queue_core.STAGED_PREFIX}{sha8}-{path.name}"
    s3.put_object(staged_key, data)

    item = queue_core.enqueue(
        "image", image_s3_key=staged_key, prompt=args.prompt or None
    )
    print(item.id)
    return 0
