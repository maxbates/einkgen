"""``einkgen prompts {ls,edit,reset}`` — manage the random-pick prompt library.

Backed by ``s3://<bucket>/config/prompt_library.txt``; see
``einkgen.core.prompt_library`` for the on-disk format.

``edit`` opens ``$EDITOR`` (or ``$VISUAL``, defaulting to ``vi``) on a temp
file pre-populated with the current library, then re-uploads on exit. Same
shape as ``git commit -m`` minus the message — convenient on a laptop where
the SPA isn't already open.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

from einkgen.core import prompt_library


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "prompts", help="manage the random-pick prompt library"
    )
    sub = p.add_subparsers(dest="prompts_command", required=True)

    ls = sub.add_parser("ls", help="list current prompts")
    ls.set_defaults(func=_cmd_ls)

    edit = sub.add_parser("edit", help="edit the library in $EDITOR")
    edit.set_defaults(func=_cmd_edit)

    reset = sub.add_parser("reset", help="restore the seed defaults")
    reset.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation",
    )
    reset.set_defaults(func=_cmd_reset)


def _cmd_ls(args: argparse.Namespace) -> int:
    current = prompt_library.load(force=True)
    if current == prompt_library.DEFAULTS:
        print(f"(seed defaults — {len(current)} entries)")
    else:
        print(f"({len(current)} entries)")
    for prompt in current:
        print(prompt)
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    current = prompt_library.load(force=True)
    header = (
        "# Edit one prompt per line. Lines starting with # are ignored.\n"
        "# Save and exit to upload; quit without saving to abort.\n"
    )
    initial = header + "\n".join(current) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="einkgen-prompts-", delete=False
    ) as tmp:
        tmp.write(initial)
        path = tmp.name
    try:
        result = subprocess.run([editor, path], check=False)
        if result.returncode != 0:
            print(f"editor exited {result.returncode}; aborting", file=sys.stderr)
            return result.returncode
        with open(path, encoding="utf-8") as fh:
            new_body = fh.read()
        if new_body == initial:
            print("no changes; library unchanged")
            return 0
        # Round-trip through the same parser the server uses so the user sees
        # the canonical persisted list — surprise-free.
        parsed = prompt_library._parse(new_body)
        if not parsed:
            print("refusing to save an empty library", file=sys.stderr)
            return 1
        persisted = prompt_library.write(list(parsed))
        print(f"saved {len(persisted)} prompts to s3://…/{prompt_library.PROMPT_LIBRARY_KEY}")
        return 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        confirm = input(
            "Replace the current library with the 10 seed prompts? [y/N] "
        )
        if confirm.strip().lower() not in {"y", "yes"}:
            print("aborted")
            return 1
    persisted = prompt_library.reset_to_defaults()
    print(f"restored {len(persisted)} seed prompts")
    return 0
