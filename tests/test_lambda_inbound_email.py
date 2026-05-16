"""Tests for the inbound-email Lambda handler.

Uses the shared moto-backed S3 fixture; an SES identity is created in the
same `mock_aws` context so SendEmail succeeds for replies. Replies are
asserted by patching the module's SES client to a MagicMock — moto's SES
support doesn't track sent messages in a way that's nice to assert on, and
the contract we care about is "did the Lambda call SendEmail with the
right shape".
"""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest

from einkgen.core import email_allowlist, queue
from einkgen.lambdas import inbound_email
from tests.conftest import TEST_BUCKET


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def reset_state():
    email_allowlist._reset_cache()
    inbound_email._reset_clients()
    yield
    email_allowlist._reset_cache()
    inbound_email._reset_clients()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EINKGEN_REPLY_FROM", "einkgen@submit.example.com")
    monkeypatch.setenv("EINKGEN_PROJECT_URL", "https://example.com/einkgen")
    monkeypatch.setenv("EINKGEN_INBOUND_PREFIX", "inbound/")
    yield


@pytest.fixture
def ses_mock(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(inbound_email, "_get_ses_client", lambda: client)
    return client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _build_email(
    *,
    from_addr: str = "me@example.com",
    subject: str = "",
    body: str = "",
    auth_pass: bool = True,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    image_name: str = "cat.jpg",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "einkgen@submit.example.com"
    msg["Subject"] = subject
    if auth_pass:
        domain = from_addr.split("@", 1)[1]
        msg["Authentication-Results"] = (
            f"amazonses.com; spf=pass smtp.mailfrom={from_addr}; "
            f"dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
        )
    else:
        msg["Authentication-Results"] = (
            "amazonses.com; spf=none; dkim=none; dmarc=none"
        )
    msg.set_content(body or "")
    if image_bytes is not None:
        maintype, subtype = image_mime.split("/", 1)
        msg.add_attachment(
            image_bytes, maintype=maintype, subtype=subtype, filename=image_name
        )
    return msg.as_bytes()


def _drop_email(s3_bucket, raw: bytes, key: str = "inbound/msg-001.eml") -> dict:
    s3_bucket.put_object(Bucket=TEST_BUCKET, Key=key, Body=raw)
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": TEST_BUCKET},
                    "object": {"key": key},
                }
            }
        ]
    }


def _seed_allowlist(s3_bucket, emails: list[str]) -> None:
    body = "\n".join(emails) + "\n"
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=email_allowlist.ALLOWLIST_KEY, Body=body.encode()
    )


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


def test_allowlisted_prompt_email_enqueues_prompt(s3_bucket, env, reset_state, ses_mock):
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(subject="Mountains at sunset")
    event = _drop_email(s3_bucket, raw)

    result = inbound_email.handler(event)
    assert result == {"processed": 1}

    items = queue.list()
    assert len(items) == 1
    item = items[0]
    assert item.kind == "prompt"
    assert item.prompt == "Mountains at sunset"
    assert item.source == "email"

    # Confirmation reply sent; rejection NOT sent.
    sends = ses_mock.send_email.call_args_list
    assert len(sends) == 1
    kwargs = sends[0].kwargs
    assert kwargs["Source"] == "einkgen@submit.example.com"
    assert kwargs["Destination"]["ToAddresses"] == ["me@example.com"]
    assert "queued" in kwargs["Message"]["Subject"]["Data"].lower()


def test_allowlisted_image_email_enqueues_image(s3_bucket, env, reset_state, ses_mock):
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(image_bytes=b"\xff\xd8\xffjpeg-bytes", image_name="cat.jpg")
    event = _drop_email(s3_bucket, raw)

    inbound_email.handler(event)

    items = queue.list()
    assert len(items) == 1
    item = items[0]
    assert item.kind == "image"
    assert item.prompt is None
    assert item.image_s3_key is not None
    assert item.image_s3_key.startswith(queue.STAGED_PREFIX)
    # Staged image bytes match.
    staged = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=item.image_s3_key)
    assert staged["Body"].read() == b"\xff\xd8\xffjpeg-bytes"


def test_allowlisted_image_plus_prompt_keeps_both(s3_bucket, env, reset_state, ses_mock):
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(
        subject="watercolor style",
        image_bytes=b"png-bytes",
        image_mime="image/png",
        image_name="src.png",
    )
    event = _drop_email(s3_bucket, raw)

    inbound_email.handler(event)

    items = queue.list()
    assert len(items) == 1
    item = items[0]
    assert item.kind == "image"
    assert item.prompt == "watercolor style"
    assert item.image_s3_key is not None


def test_non_allowlisted_sender_is_rejected_with_reply(s3_bucket, env, reset_state, ses_mock):
    _seed_allowlist(s3_bucket, ["someone-else@example.com"])
    raw = _build_email(from_addr="stranger@example.com", subject="hi")
    event = _drop_email(s3_bucket, raw)

    inbound_email.handler(event)

    # Nothing enqueued.
    assert queue.list() == []

    # Rejection reply sent — and it must NOT leak the allowlist.
    assert ses_mock.send_email.call_count == 1
    kwargs = ses_mock.send_email.call_args.kwargs
    assert kwargs["Destination"]["ToAddresses"] == ["stranger@example.com"]
    body = kwargs["Message"]["Body"]["Text"]["Data"]
    assert "authorised" in body or "authorized" in body
    assert "https://example.com/einkgen" in body
    # The legitimate user's address must not appear anywhere in the reply.
    assert "someone-else@example.com" not in body
    assert "someone-else" not in kwargs["Message"]["Subject"]["Data"]


def test_unauthenticated_email_is_dropped_silently(s3_bucket, env, reset_state, ses_mock):
    """If SPF/DKIM didn't pass we can't trust From: → drop without replying.

    Replying would let an attacker turn the Lambda into a backscatter cannon
    by forging From: to a victim's address. The message is just dropped.
    """
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(auth_pass=False)
    event = _drop_email(s3_bucket, raw)

    inbound_email.handler(event)

    assert queue.list() == []
    assert ses_mock.send_email.call_count == 0


def test_inbound_object_is_deleted_after_processing(s3_bucket, env, reset_state, ses_mock):
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(subject="hello")
    event = _drop_email(s3_bucket, raw, key="inbound/abc.eml")

    inbound_email.handler(event)

    listing = s3_bucket.list_objects_v2(Bucket=TEST_BUCKET, Prefix="inbound/")
    assert "Contents" not in listing


def test_object_outside_inbound_prefix_is_ignored(s3_bucket, env, reset_state, ses_mock):
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(subject="hello")
    # Drop in queue/ instead of inbound/. Handler must ignore.
    event = _drop_email(s3_bucket, raw, key="queue/staged/weird.eml")

    result = inbound_email.handler(event)
    assert result == {"processed": 0}
    assert queue.list() == []


def test_empty_email_is_a_noop(s3_bucket, env, reset_state, ses_mock):
    """No prompt + no image: drop without enqueueing or replying."""
    _seed_allowlist(s3_bucket, ["me@example.com"])
    raw = _build_email(subject="", body="")
    event = _drop_email(s3_bucket, raw)

    inbound_email.handler(event)

    assert queue.list() == []
    # The inbound object should still be deleted to keep the prefix tidy.
    listing = s3_bucket.list_objects_v2(Bucket=TEST_BUCKET, Prefix="inbound/")
    assert "Contents" not in listing
