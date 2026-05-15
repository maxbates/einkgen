"""einkgen CLI root dispatcher.

Top-level structure (per ARCHITECTURE §3): `status`, `history`, `queue …`, `local …`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from einkgen.cli import history as history_cmd
from einkgen.cli import local as local_cli
from einkgen.cli import queue as queue_cli
from einkgen.cli import status as status_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="einkgen",
        description="Generate and dither images for an Inkplate 10 e-paper display.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    status_cmd.register(sub)
    history_cmd.register(sub)
    queue_cli.register(sub)

    local_parser = sub.add_parser(
        "local",
        help="Local dev/debug commands (never touches S3).",
    )
    local_cli.register(local_parser)
    local_parser.set_defaults(func=local_cli.run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return 1
    return int(handler(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
