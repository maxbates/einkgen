"""Tests for the OpenAI adapter — fully mocked, no network calls."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from einkgen.core import generate as generate_mod
from einkgen.core.generate import (
    BASE_PROMPT,
    IMAGE_SIZE,
    PROMPT_LIBRARY,
    _resolve_api_key,
    generate,
    generate_from_image,
    random_prompt,
)


def test_base_prompt_mentions_panel_resolution():
    assert BASE_PROMPT, "BASE_PROMPT must be non-empty"
    assert ("1200×825" in BASE_PROMPT) or ("1200x825" in BASE_PROMPT)


def test_prompt_library_has_exactly_ten_entries():
    assert len(PROMPT_LIBRARY) == 10
    assert all(isinstance(p, str) and p.strip() for p in PROMPT_LIBRARY)


def test_random_prompt_returns_library_entry():
    p = random_prompt()
    assert p in PROMPT_LIBRARY


def _fake_client(b64: str) -> MagicMock:
    """Build a mock that mimics `client.images.generate(...).data[0].b64_json`."""
    client = MagicMock()
    response = MagicMock()
    datum = MagicMock()
    datum.b64_json = b64
    response.data = [datum]
    client.images.generate.return_value = response
    return client


def test_generate_prepends_base_prompt_and_calls_with_correct_size():
    fake_png = b"\x89PNG\r\n\x1a\nfakebody"
    b64 = base64.b64encode(fake_png).decode()
    client = _fake_client(b64)

    out = generate("a foggy cliff at dawn", client=client)

    # Returned the decoded bytes.
    assert out == fake_png

    # Called with the right size, model, and prepended BASE_PROMPT.
    client.images.generate.assert_called_once()
    call_kwargs = client.images.generate.call_args.kwargs
    assert call_kwargs["size"] == IMAGE_SIZE == "1536x1024"
    assert call_kwargs["model"] == "gpt-image-1"
    assert call_kwargs["n"] == 1
    assert call_kwargs["prompt"].startswith(BASE_PROMPT)
    assert call_kwargs["prompt"].endswith("a foggy cliff at dawn")


def test_generate_raises_when_no_b64_payload():
    client = MagicMock()
    response = MagicMock()
    datum = MagicMock(spec=[])  # no b64_json attribute, not a dict
    response.data = [datum]
    client.images.generate.return_value = response

    with pytest.raises(RuntimeError, match="b64_json"):
        generate("anything", client=client)


def test_generate_from_image_calls_edit_endpoint_with_prepended_base_prompt():
    fake_png = b"\x89PNG\r\n\x1a\nrestyled"
    b64 = base64.b64encode(fake_png).decode()
    client = MagicMock()
    response = MagicMock()
    datum = MagicMock()
    datum.b64_json = b64
    response.data = [datum]
    client.images.edit.return_value = response

    out = generate_from_image(
        "as a charcoal sketch",
        b"original-image-bytes",
        image_filename="photo.jpg",
        client=client,
    )

    assert out == fake_png
    client.images.edit.assert_called_once()
    kwargs = client.images.edit.call_args.kwargs
    assert kwargs["model"] == "gpt-image-1"
    assert kwargs["size"] == IMAGE_SIZE
    assert kwargs["n"] == 1
    assert kwargs["prompt"].startswith(BASE_PROMPT)
    assert kwargs["prompt"].endswith("as a charcoal sketch")
    # Image arg should be a BytesIO carrying the original bytes + filename hint.
    image_arg = kwargs["image"]
    assert image_arg.name == "photo.jpg"
    image_arg.seek(0)
    assert image_arg.read() == b"original-image-bytes"


def test_resolve_api_key_prefers_env_var(monkeypatch):
    """CLI / local dev path — OPENAI_API_KEY env var wins, no AWS round-trip."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-direct-value")
    monkeypatch.setenv("OPENAI_API_KEY_SECRET_NAME", "should-be-ignored")
    assert _resolve_api_key() == "sk-direct-value"


def test_resolve_api_key_returns_none_when_nothing_set(monkeypatch):
    """Both env vars missing — let OpenAI() raise its own clear error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_SECRET_NAME", raising=False)
    assert _resolve_api_key() is None
