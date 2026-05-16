"""``einkgen allowlist {ls,add,rm}`` — manage the inbound-email allowlist.

Backed by ``s3://<bucket>/config/email_allowlist.txt``; see
``einkgen.core.email_allowlist`` for the on-disk format.
"""

from __future__ import annotations

import argparse
import sys

from einkgen.core import email_allowlist


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "allowlist", help="manage the inbound-email sender allowlist"
    )
    sub = p.add_subparsers(dest="allowlist_command", required=True)

    ls = sub.add_parser("ls", help="list allowed senders")
    ls.set_defaults(func=_cmd_ls)

    add = sub.add_parser("add", help="add a sender")
    add.add_argument("email")
    add.set_defaults(func=_cmd_add)

    rm = sub.add_parser("rm", help="remove a sender")
    rm.add_argument("email")
    rm.set_defaults(func=_cmd_rm)


def _cmd_ls(args: argparse.Namespace) -> int:
    entries = sorted(email_allowlist.load(force=True))
    if not entries:
        print("(allowlist empty)")
        return 0
    for e in entries:
        print(e)
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    current = set(email_allowlist.load(force=True))
    new = email_allowlist._normalize(args.email)
    if "@" not in new:
        print(f"not an email: {args.email}", file=sys.stderr)
        return 1
    if new in current:
        print(f"already present: {new}")
        return 0
    current.add(new)
    email_allowlist.write(sorted(current))
    print(f"added {new}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    current = set(email_allowlist.load(force=True))
    target = email_allowlist._normalize(args.email)
    if target not in current:
        print(f"not on allowlist: {target}", file=sys.stderr)
        return 1
    current.discard(target)
    email_allowlist.write(sorted(current))
    print(f"removed {target}")
    return 0
