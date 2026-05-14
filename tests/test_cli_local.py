"""Tests for `einkgen local {convert,generate,preview}`."""

from __future__ import annotations

import base64
import io
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from einkgen.cli import main
from einkgen.cli import local as local_cli
from einkgen.core import convert as convert_mod


FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_PATH = FIXTURE_DIR / "sample.png"


def _make_sample_gradient(path: Path, w: int = 1500, h: int = 1000) -> None:
    """Programmatic 1500x1000 RGB diagonal gradient. README §6 calls for at
    least panel-sized input so the convert step exercises center-crop."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            r = int(255 * x / (w - 1))
            g = int(255 * y / (h - 1))
            b = int((r + g) / 2)
            px[x, y] = (r, g, b)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


@pytest.fixture(scope="module", autouse=True)
def _ensure_sample_fixture():
    if not SAMPLE_PATH.exists():
        _make_sample_gradient(SAMPLE_PATH)
    yield


@pytest.fixture
def tiny_panel(monkeypatch):
    """Shrink the panel so the dither loop runs in reasonable time under test."""
    monkeypatch.setattr(convert_mod, "PANEL_WIDTH", 60)
    monkeypatch.setattr(convert_mod, "PANEL_HEIGHT", 40)


def _png_bytes(w: int = 1536, h: int = 1024) -> bytes:
    """A simple PNG payload that the convert step can ingest."""
    img = Image.new("RGB", (w, h), (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _patch_openai_client(monkeypatch, png_bytes: bytes) -> MagicMock:
    """Make the generate adapter return a mock client that yields `png_bytes`."""
    b64 = base64.b64encode(png_bytes).decode()
    client = MagicMock()
    response = MagicMock()
    datum = MagicMock()
    datum.b64_json = b64
    response.data = [datum]
    client.images.generate.return_value = response
    monkeypatch.setattr(local_cli, "_make_client", lambda: client)
    return client


def test_help_lists_local_subcommand(capsys):
    """`python -m einkgen --help` should mention `local`."""
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "local" in out


def test_convert_subcommand_writes_bmp(tmp_path, tiny_panel):
    out_path = tmp_path / "out.bmp"
    rc = main(["local", "convert", str(SAMPLE_PATH), str(out_path)])
    assert rc == 0
    assert out_path.exists()
    data = out_path.read_bytes()
    assert data[:2] == b"BM", "output must be a BMP"
    bpp = struct.unpack_from("<H", data, 28)[0]
    assert bpp == 8, "expected 8-bit indexed BMP"


def test_generate_subcommand_writes_png(tmp_path, monkeypatch):
    fake_png = _png_bytes(64, 64)
    client = _patch_openai_client(monkeypatch, fake_png)
    out_path = tmp_path / "out.png"

    rc = main(["local", "generate", "a foggy cliff at dawn", str(out_path)])
    assert rc == 0
    assert out_path.read_bytes() == fake_png
    client.images.generate.assert_called_once()
    # Sanity-check it really hit gpt-image-1 with 1536x1024.
    kwargs = client.images.generate.call_args.kwargs
    assert kwargs["size"] == "1536x1024"
    assert kwargs["model"] == "gpt-image-1"


def test_preview_subcommand_writes_png(tmp_path, monkeypatch, tiny_panel):
    fake_png = _png_bytes(120, 80)  # > tiny_panel, so center-crop path runs
    _patch_openai_client(monkeypatch, fake_png)
    out_path = tmp_path / "preview.png"

    rc = main(["local", "preview", "geometric shapes", "--output", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    # Round-trip as PIL to make sure it's a real PNG.
    img = Image.open(out_path)
    img.verify()
