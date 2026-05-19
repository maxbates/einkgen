"""Tests for the OpenAI adapter — fully mocked, no network calls."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from einkgen.core import generate as generate_mod
from einkgen.core import prompt_library as prompt_library_mod
from einkgen.core.generate import (
    BASE_PROMPT,
    EXPAND_TOPIC_SYSTEM_PROMPT,
    IMAGE_SIZE,
    PROMPT_LIBRARY,
    TEXT_MODEL,
    _resolve_api_key,
    expand_topic,
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


def test_random_prompt_returns_library_entry(s3_bucket):
    # No prompt_library.txt on S3 → falls back to PROMPT_LIBRARY (= DEFAULTS).
    prompt_library_mod._reset_cache()
    p = random_prompt()
    assert p in PROMPT_LIBRARY
    prompt_library_mod._reset_cache()


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
    assert call_kwargs["size"] == IMAGE_SIZE == "1200x832"
    assert call_kwargs["model"] == "gpt-image-2"
    assert call_kwargs["quality"] == "medium"
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
    assert kwargs["model"] == "gpt-image-2"
    assert kwargs["quality"] == "medium"
    assert kwargs["size"] == IMAGE_SIZE
    assert kwargs["n"] == 1
    assert kwargs["prompt"].startswith(BASE_PROMPT)
    assert kwargs["prompt"].endswith("as a charcoal sketch")
    # Image arg should be a BytesIO carrying the original bytes + filename hint.
    image_arg = kwargs["image"]
    assert image_arg.name == "photo.jpg"
    image_arg.seek(0)
    assert image_arg.read() == b"original-image-bytes"


def _fake_text_client(content: str) -> MagicMock:
    """Mimic ``client.chat.completions.create(...).choices[0].message.content``."""
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    response.choices = [choice]
    client.chat.completions.create.return_value = response
    return client


def test_expand_topic_calls_text_model_and_returns_stripped_content():
    client = _fake_text_client("  A single rusty bicycle leans on a "
                               "whitewashed wall.\n")
    out = expand_topic("urban still life", client=client)

    assert out == "A single rusty bicycle leans on a whitewashed wall."

    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == TEXT_MODEL
    messages = kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == EXPAND_TOPIC_SYSTEM_PROMPT
    assert messages[1]["role"] == "user"
    # Without angles/avoid the user message is just the TOPIC line — no
    # empty ANGLES / AVOID sections leaked into the prompt.
    assert messages[1]["content"] == "TOPIC: urban still life"


def test_expand_topic_injects_angles_into_user_message():
    """ANGLES (steering hints) appear in the per-call user message."""
    client = _fake_text_client("A rusty bicycle.")
    expand_topic(
        "urban still life",
        client=client,
        angles=["Hokkaido", "predawn blue hour"],
    )
    user_content = client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert "TOPIC: urban still life" in user_content
    assert "ANGLES" in user_content
    assert "Hokkaido" in user_content
    assert "predawn blue hour" in user_content
    # System prompt isn't mutated per-call.
    sys_content = client.chat.completions.create.call_args.kwargs["messages"][0][
        "content"
    ]
    assert sys_content == EXPAND_TOPIC_SYSTEM_PROMPT


def test_expand_topic_injects_avoid_list_into_user_message():
    """AVOID list appears as bulleted lines so the LLM can scan them."""
    client = _fake_text_client("A new scene.")
    expand_topic(
        "world landmark",
        client=client,
        avoid=[
            "Machu Picchu in golden hour, bird's-eye composition.",
            "Petra carved facade at noon, head-on.",
        ],
    )
    user_content = client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert "AVOID" in user_content
    # Each avoid entry is a bullet line — easy to scan, easy to grow.
    assert "- Machu Picchu in golden hour, bird's-eye composition." in user_content
    assert "- Petra carved facade at noon, head-on." in user_content


def test_expand_topic_omits_empty_steering_sections():
    """Passing all-empty angles/avoid still produces a clean TOPIC-only message."""
    client = _fake_text_client("anything.")
    expand_topic(
        "minimal",
        client=client,
        angles=["", "  "],
        avoid=["", None],  # type: ignore[list-item]
    )
    user_content = client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert user_content == "TOPIC: minimal"
    assert "ANGLES" not in user_content
    assert "AVOID" not in user_content


def test_expand_topic_rejects_empty_topic():
    with pytest.raises(ValueError):
        expand_topic("   ")


def test_expand_topic_raises_when_response_has_no_content():
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = ""
    response.choices = [choice]
    client.chat.completions.create.return_value = response
    with pytest.raises(RuntimeError, match="missing content"):
        expand_topic("anything", client=client)


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
