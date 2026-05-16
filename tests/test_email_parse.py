"""Tests for ``core/email_parse.py``."""

from __future__ import annotations

import base64
from email.message import EmailMessage

from einkgen.core import email_parse


def _make_email(
    *,
    from_addr: str = "me@example.com",
    subject: str = "hello",
    body: str = "",
    auth_results: str | None = (
        "amazonses.com; spf=pass smtp.mailfrom=me@example.com; "
        "dkim=pass header.d=example.com; dmarc=pass header.from=example.com"
    ),
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    image_name: str = "photo.jpg",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["To"] = "einkgen@submit.example.com"
    if auth_results is not None:
        msg["Authentication-Results"] = auth_results
    msg.set_content(body or "")
    if image_bytes is not None:
        maintype, subtype = image_mime.split("/", 1)
        msg.add_attachment(
            image_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=image_name,
        )
    return msg.as_bytes()


def test_text_only_email_yields_prompt_kind():
    raw = _make_email(subject="Mountains at sunset", body="")
    parsed = email_parse.parse_message(raw)
    assert parsed.sender == "me@example.com"
    assert parsed.prompt == "Mountains at sunset"
    assert parsed.image_bytes is None
    assert parsed.reject_reason is None


def test_image_attachment_is_extracted():
    img = b"\xff\xd8\xff\xe0fake-jpeg"
    raw = _make_email(subject="", body="", image_bytes=img, image_name="cat.jpg")
    parsed = email_parse.parse_message(raw)
    assert parsed.sender == "me@example.com"
    assert parsed.image_bytes == img
    assert parsed.image_filename == "cat.jpg"
    assert parsed.prompt is None


def test_image_plus_prompt_keeps_both():
    img = b"PNG-bytes"
    raw = _make_email(
        subject="render as a woodcut",
        body="",
        image_bytes=img,
        image_mime="image/png",
        image_name="src.png",
    )
    parsed = email_parse.parse_message(raw)
    assert parsed.prompt == "render as a woodcut"
    assert parsed.image_bytes == img
    assert parsed.image_filename == "src.png"


def test_subject_re_and_fwd_prefixes_are_stripped():
    raw = _make_email(subject="Re: Mountains at sunset", body="")
    assert email_parse.parse_message(raw).prompt == "Mountains at sunset"
    raw = _make_email(subject="Fwd: Snow", body="")
    assert email_parse.parse_message(raw).prompt == "Snow"


def test_body_used_when_subject_is_empty():
    raw = _make_email(subject="", body="A foggy cliff at dawn\n\nMore text")
    assert email_parse.parse_message(raw).prompt == "A foggy cliff at dawn"


def test_subject_and_body_are_concatenated_when_both_present():
    raw = _make_email(
        subject="watercolor",
        body="of a mountain at dawn\n\nignored second line",
    )
    parsed = email_parse.parse_message(raw)
    assert parsed.prompt == "watercolor\n\nof a mountain at dawn"


def test_signature_lines_are_not_used_as_prompt():
    raw = _make_email(subject="", body="Sent from my iPhone")
    # First non-empty line is a signature marker → no prompt.
    assert email_parse.parse_message(raw).prompt is None


def test_unauthenticated_message_is_rejected():
    raw = _make_email(auth_results="amazonses.com; spf=none; dkim=none; dmarc=none")
    parsed = email_parse.parse_message(raw)
    assert parsed.reject_reason is not None
    assert parsed.sender == "me@example.com"  # display only
    assert parsed.image_bytes is None
    assert parsed.prompt is None


def test_missing_auth_header_is_rejected():
    raw = _make_email(auth_results=None)
    parsed = email_parse.parse_message(raw)
    assert parsed.reject_reason is not None


def test_dkim_pass_on_mismatched_domain_is_rejected():
    # DKIM passed but for a different domain than From: — could be a
    # forwarder's signature, not the sender's. We require alignment.
    raw = _make_email(
        from_addr="me@example.com",
        auth_results=(
            "amazonses.com; spf=none; "
            "dkim=pass header.d=mailer.com; dmarc=fail"
        ),
    )
    assert email_parse.parse_message(raw).reject_reason is not None


def test_dkim_pass_with_header_i_at_domain_is_accepted():
    """Gmail-through-SES emits ``dkim=pass header.i=@gmail.com`` (with the
    leading ``@``), not ``header.d=``. We must accept both formats."""
    raw = _make_email(
        from_addr="user@gmail.com",
        auth_results=(
            "amazonses.com; spf=none; "
            "dkim=pass header.i=@gmail.com; dmarc=none"
        ),
    )
    parsed = email_parse.parse_message(raw)
    assert parsed.reject_reason is None
    assert parsed.sender == "user@gmail.com"


def test_dmarc_pass_aligned_is_accepted():
    """DMARC=pass with aligned header.from is the strongest signal — by
    definition it means SPF or DKIM passed AND aligned with From:."""
    raw = _make_email(
        from_addr="user@gmail.com",
        auth_results=(
            "amazonses.com; spf=none; dkim=none; "
            "dmarc=pass header.from=gmail.com"
        ),
    )
    parsed = email_parse.parse_message(raw)
    assert parsed.reject_reason is None
    assert parsed.sender == "user@gmail.com"


def test_dmarc_pass_misaligned_is_rejected():
    raw = _make_email(
        from_addr="user@gmail.com",
        auth_results=(
            "amazonses.com; spf=none; dkim=none; "
            "dmarc=pass header.from=mailer.com"  # not gmail.com
        ),
    )
    assert email_parse.parse_message(raw).reject_reason is not None


def test_spf_pass_on_aligned_mailfrom_is_accepted():
    raw = _make_email(
        from_addr="me@example.com",
        auth_results=(
            "amazonses.com; spf=pass smtp.mailfrom=bounce@example.com; "
            "dkim=none"
        ),
    )
    parsed = email_parse.parse_message(raw)
    assert parsed.reject_reason is None
    assert parsed.sender == "me@example.com"


def test_gif_attachments_are_ignored():
    """Animated GIFs would publish only one frame anyway."""
    raw = _make_email(image_bytes=b"GIF89a-fake", image_mime="image/gif")
    parsed = email_parse.parse_message(raw)
    assert parsed.image_bytes is None


def test_prompt_is_truncated_to_max_chars():
    long = "x" * (email_parse.MAX_PROMPT_CHARS + 100)
    raw = _make_email(subject=long, body="")
    parsed = email_parse.parse_message(raw)
    assert parsed.prompt is not None
    assert len(parsed.prompt) == email_parse.MAX_PROMPT_CHARS
