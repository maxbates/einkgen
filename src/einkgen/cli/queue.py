"""``einkgen queue {ls,rm,prompt,image}`` CLI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import boto3

from einkgen.core import queue as queue_core
from einkgen.core import s3

log = logging.getLogger(__name__)

PROMPT_PREVIEW = 60

# Lambda function name the CLI invokes for ``--now``. Mirrors the env
# var the admin and device-status Lambdas use; defaults to the CDK
# function name so a freshly-deployed stack works without extra env.
DEFAULT_GENERATOR_FUNCTION_NAME = "einkgen-generator"


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
    placement_pr = pr.add_mutually_exclusive_group()
    placement_pr.add_argument(
        "--top",
        action="store_true",
        help="jump to the head of the queue (default: append to tail)",
    )
    placement_pr.add_argument(
        "--now",
        action="store_true",
        help="enqueue at the top AND async-invoke the generator so it "
        "renders into the buffer immediately, bypassing the 10-deep "
        "wait. Requires lambda:InvokeFunction on einkgen-generator.",
    )
    pr.set_defaults(func=_cmd_prompt)

    im = qsub.add_parser("image", help="enqueue a local image file")
    im.add_argument("path")
    im.add_argument(
        "--prompt",
        help="optional restyle prompt; if set, the image is fed to gpt-image-2's "
        "edit endpoint instead of being passed through B&W only",
    )
    placement_im = im.add_mutually_exclusive_group()
    placement_im.add_argument(
        "--top",
        action="store_true",
        help="jump to the head of the queue (default: append to tail)",
    )
    placement_im.add_argument(
        "--now",
        action="store_true",
        help="enqueue at the top AND async-invoke the generator so it "
        "renders into the buffer immediately, bypassing the 10-deep "
        "wait. Requires lambda:InvokeFunction on einkgen-generator.",
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


def _resolve_placement(args: argparse.Namespace) -> tuple[str, bool]:
    """Return ``(at, render_now)`` from the CLI flags.

    ``--now`` implies top placement plus an async render invoke;
    ``--top`` is just top placement. Default is bottom.
    """
    if getattr(args, "now", False):
        return "top", True
    if getattr(args, "top", False):
        return "top", False
    return "bottom", False


def _trigger_render_now() -> bool:
    """Fire-and-forget invoke of the generator to render the head item.

    Returns True on success. Failures are surfaced as a stderr warning
    by the caller — the item is on the queue regardless, so the worst
    case is "wait for the next cron tick" rather than data loss.
    """
    fn_name = os.environ.get(
        "EINKGEN_GENERATOR_FUNCTION_NAME", DEFAULT_GENERATOR_FUNCTION_NAME
    )
    try:
        boto3.client("lambda").invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=json.dumps({"action": "render_now"}).encode("utf-8"),
        )
        return True
    except Exception as exc:  # pragma: no cover - surfaced via stderr
        log.warning("failed to invoke %s: %s", fn_name, exc)
        return False


def _cmd_prompt(args: argparse.Namespace) -> int:
    at, render_now = _resolve_placement(args)
    item = queue_core.enqueue("prompt", prompt=args.text, at=at)
    print(item.id)
    if render_now and not _trigger_render_now():
        print(
            "warning: enqueued but failed to invoke the generator; "
            "item will render on the next cron tick",
            file=sys.stderr,
        )
    return 0


def _cmd_image(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1
    data = path.read_bytes()
    staged_key = queue_core.build_staged_key(data, path.name)
    s3.put_object(staged_key, data)

    at, render_now = _resolve_placement(args)
    item = queue_core.enqueue(
        "image", image_s3_key=staged_key, prompt=args.prompt or None, at=at
    )
    print(item.id)
    if render_now and not _trigger_render_now():
        print(
            "warning: enqueued but failed to invoke the generator; "
            "item will render on the next cron tick",
            file=sys.stderr,
        )
    return 0
