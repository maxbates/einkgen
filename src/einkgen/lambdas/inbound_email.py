"""``einkgen-inbound-email`` Lambda — turns an inbound email into a queue item.

Trigger
-------
S3 ObjectCreated on ``inbound/*``. The SES receipt rule for the configured
domain has a single S3 action that writes the raw RFC 5322 message there.
We don't take a direct SES → Lambda invocation because that path caps the
email payload at ~150 KB, which is too small for the photos people will
send from a phone.

Flow
----
1. Read the raw email from S3.
2. Parse the message; require SES Authentication-Results to show
   ``spf=pass`` or ``dkim=pass`` aligned with the From: domain. Without
   that, From: is forgeable and the allowlist would be bypassable.
3. Check the verified sender against ``config/email_allowlist.txt``.
   - Not on the allowlist → reply with a friendly "set up your own" note
     (without revealing who *is* on the list) and stop.
4. On match: stage the image to ``queue/staged/`` (if present) and
   ``enqueue()`` the appropriate kind.
5. Reply confirming the queue id.
6. Delete the inbound object so the bucket doesn't grow unboundedly.

Security
--------
Every external-facing branch (reject, accept, malformed) goes through the
sender-auth gate first. The Lambda has scoped IAM (read inbound/, read
config/email_allowlist.txt, write queue/* and queue/staged/*, ses:SendEmail).
It never reads the allowlist contents into a response — rejection emails are
generic.

Environment
-----------
- ``EINKGEN_BUCKET``     — bucket name (set by CDK).
- ``EINKGEN_INBOUND_PREFIX`` — defaults to ``inbound/``.
- ``EINKGEN_REPLY_FROM`` — the verified SES identity we send replies as
  (e.g. ``einkgen@submit.example.com``). Without it, replies are skipped
  but enqueue still proceeds.
- ``EINKGEN_PROJECT_URL`` — included in rejection replies as the "run your
  own" pointer. Optional.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any
from urllib.parse import unquote_plus

import boto3

from einkgen.core import email_allowlist, email_parse, queue
from einkgen.core import s3 as s3mod

log = logging.getLogger(__name__)

DEFAULT_INBOUND_PREFIX = "inbound/"

_ses_client = None


def _get_ses_client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("ses")
    return _ses_client


def _reset_clients() -> None:
    """Drop cached SES client. Used by tests."""
    global _ses_client
    _ses_client = None


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    processed = 0
    for record in event.get("Records", []) or []:
        bucket = (record.get("s3", {}).get("bucket") or {}).get("name")
        key = unquote_plus((record.get("s3", {}).get("object") or {}).get("key", ""))
        if not bucket or not key:
            continue
        prefix = os.environ.get("EINKGEN_INBOUND_PREFIX", DEFAULT_INBOUND_PREFIX)
        if not key.startswith(prefix):
            log.warning("inbound_email: ignoring key outside %s: %s", prefix, key)
            continue
        try:
            _process_one(key)
            processed += 1
        except Exception:
            # Don't let one bad message poison the rest of the batch — and
            # don't fail the invocation either; S3 retries would just keep
            # bouncing on the same broken email forever.
            log.exception("inbound_email: failed to process %s", key)
            _safe_delete(key)
    return {"processed": processed}


def _process_one(key: str) -> None:
    raw = s3mod.get_object(key)
    parsed = email_parse.parse_message(raw)

    if parsed.reject_reason is not None:
        # Sender authentication didn't pass — we can't trust From:, so we
        # don't even try to bounce. SES already absorbed the message; log
        # and drop. Include the raw Authentication-Results header so an
        # operator can diagnose why a legitimate sender bounced.
        auth_headers = _extract_auth_headers(raw)
        log.info(
            "inbound_email: dropping unauthenticated message %s "
            "(from=%s reason=%s auth=%r)",
            key, parsed.sender, parsed.reject_reason, auth_headers,
        )
        _safe_delete(key)
        return

    sender = parsed.sender
    if not email_allowlist.is_allowed(sender):
        log.info("inbound_email: rejecting non-allowlisted sender %s", sender)
        _send_rejection(sender)
        _safe_delete(key)
        return

    item = _enqueue(parsed, sender=sender)
    log.info(
        "inbound_email: enqueued %s (kind=%s, sender=%s)",
        item.id, item.kind, sender,
    )
    _send_confirmation(sender, item.id, item.kind)
    _safe_delete(key)


def _enqueue(parsed: email_parse.ParsedEmail, *, sender: str):
    if parsed.image_bytes:
        staged_key = _stage_image(parsed.image_bytes, parsed.image_filename or "image")
        prompt = parsed.prompt or None
        return queue.enqueue(
            "image",
            image_s3_key=staged_key,
            prompt=prompt,
            source="email",
        )
    if parsed.prompt:
        return queue.enqueue("prompt", prompt=parsed.prompt, source="email")
    # Neither prompt nor image — treat as a no-op. SES forwarded a content-less
    # message (e.g. an empty body with a stripped subject). Raise so the caller
    # logs + deletes; we don't enqueue a random in this branch because that'd
    # bill the operator for an empty submit.
    raise ValueError("email contained neither a prompt nor an image attachment")


def _stage_image(data: bytes, filename: str) -> str:
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    safe_name = _safe_filename(filename)
    staged_key = f"{queue.STAGED_PREFIX}{sha8}-{safe_name}"
    s3mod.put_object(staged_key, data)
    return staged_key


def _safe_filename(name: str) -> str:
    # Strip any path components and keep a safe charset for S3 keys.
    base = os.path.basename(name) or "image"
    out = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base)
    return out[:80]  # keep keys tidy


def _extract_auth_headers(raw: bytes) -> list[str]:
    """Pull every ``Authentication-Results`` header for debug logging."""
    import email as _email

    msg = _email.message_from_bytes(raw)
    return list(msg.get_all("Authentication-Results") or [])


def _safe_delete(key: str) -> None:
    try:
        s3mod.delete_object(key)
    except Exception:  # pragma: no cover - best-effort cleanup
        log.warning("inbound_email: failed to delete %s", key)


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


def _reply_from() -> str | None:
    return os.environ.get("EINKGEN_REPLY_FROM") or None


def _project_url() -> str | None:
    return os.environ.get("EINKGEN_PROJECT_URL") or None


def _send_rejection(to: str | None) -> None:
    if not to:
        return
    reply_from = _reply_from()
    if not reply_from:
        log.info("inbound_email: EINKGEN_REPLY_FROM unset; skipping rejection reply")
        return
    project_url = _project_url()
    setup_pointer = (
        f"You can run your own einkgen and have it accept your address: {project_url}"
        if project_url
        else "You can run your own einkgen and have it accept your address."
    )
    body = (
        "Hi,\n\n"
        "Your email address isn't authorised to submit prompts or images to "
        "this einkgen device, so the message was dropped.\n\n"
        f"{setup_pointer}\n\n"
        "— einkgen\n"
    )
    _send_email(reply_from, to, "einkgen: submission not accepted", body)


def _send_confirmation(to: str, item_id: str, kind: str) -> None:
    reply_from = _reply_from()
    if not reply_from:
        return
    detail = {
        "prompt": "Queued your prompt for generation.",
        "image": "Queued your image for processing.",
    }.get(kind, f"Queued ({kind}).")
    body = f"{detail}\n\nQueue id: {item_id}\n\n— einkgen\n"
    _send_email(reply_from, to, "einkgen: submission queued", body)


def _send_email(source: str, to: str, subject: str, body: str) -> None:
    try:
        _get_ses_client().send_email(
            Source=source,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
    except Exception:  # pragma: no cover - delivery is best-effort
        log.exception("inbound_email: failed to send reply to %s", to)
