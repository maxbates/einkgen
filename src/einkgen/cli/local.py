"""`einkgen local {generate, convert, preview}` — dev/debug commands.

Never touches S3. Per README §3, these are pure local helpers for verifying
the model and the image pipeline before publishing.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image

from einkgen.core import convert as convert_mod
from einkgen.core import generate as generate_mod


def register(parser: argparse.ArgumentParser) -> None:
    """Attach `local` subparsers to a parser."""
    sub = parser.add_subparsers(dest="local_command", metavar="<subcommand>")

    p_convert = sub.add_parser(
        "convert",
        help="Load an image, run the pipeline, write 8-bit indexed BMP.",
    )
    p_convert.add_argument("input", type=Path, help="Source image path")
    p_convert.add_argument("output", type=Path, help="Destination .bmp path")
    p_convert.add_argument(
        "--dither",
        default="atkinson",
        choices=("atkinson", "floyd-steinberg"),
        help="Dither algorithm (default: atkinson)",
    )

    p_generate = sub.add_parser(
        "generate",
        help="Call gpt-image-1 with BASE_PROMPT + your text, save raw PNG.",
    )
    p_generate.add_argument("prompt", type=str, help="Subject text (BASE_PROMPT is prepended)")
    p_generate.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=Path("out.png"),
        help="Destination .png (default: out.png)",
    )

    p_preview = sub.add_parser(
        "preview",
        help="Generate + convert, save the dithered output as preview.png.",
    )
    p_preview.add_argument("prompt", type=str, help="Subject text (BASE_PROMPT is prepended)")
    p_preview.add_argument(
        "--dither",
        default="atkinson",
        choices=("atkinson", "floyd-steinberg"),
        help="Dither algorithm (default: atkinson)",
    )
    p_preview.add_argument(
        "--output",
        type=Path,
        default=Path("preview.png"),
        help="Destination .png (default: preview.png)",
    )


# Test/DI hook: tests can monkeypatch this to inject a fake OpenAI client.
def _make_client():
    return None  # generate() will lazily construct a real one when client=None


def _cmd_convert(args: argparse.Namespace) -> int:
    src = Image.open(args.input)
    bmp_bytes = convert_mod.convert(src, dither=args.dither)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(bmp_bytes)
    print(f"wrote {args.output} ({len(bmp_bytes)} bytes)")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    client = _make_client()
    png_bytes = generate_mod.generate(args.prompt, client=client)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(png_bytes)
    print(f"wrote {args.output} ({len(png_bytes)} bytes)")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    client = _make_client()
    png_bytes = generate_mod.generate(args.prompt, client=client)
    bmp_bytes = convert_mod.convert(png_bytes, dither=args.dither)
    # README §6: preview writes PNG, not BMP, "so we can eyeball it before pushing".
    bmp_img = Image.open(io.BytesIO(bmp_bytes))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bmp_img.save(args.output, format="PNG")
    print(f"wrote {args.output}")
    return 0


def run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "local_command", None)
    if cmd == "convert":
        return _cmd_convert(args)
    if cmd == "generate":
        return _cmd_generate(args)
    if cmd == "preview":
        return _cmd_preview(args)
    print("usage: einkgen local {convert,generate,preview} ...", file=sys.stderr)
    return 1
