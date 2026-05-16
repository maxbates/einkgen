"""Inbound-email MIME parsing for the einkgen submission flow.

Pure-stdlib (uses :mod:`email`). The Lambda fetches the raw RFC 5322 message
from S3 (SES's S3 action writes the email there) and hands the bytes to
:func:`parse_message`, which returns:

  - the verified sender (after SES Authentication-Results check)
  - the prompt text, if any
  - the first image attachment, if any

A submission with an image attachment maps to ``kind="image"`` (optionally
with the prompt as a restyle hint); otherwise to ``kind="prompt"``.

SES adds an ``Authentication-Results`` header listing the outcomes of SPF,
DKIM, DMARC, and DMARC-verdict. We treat the From header as authentic only
when at least one of SPF / DKIM passed for that domain — otherwise the
sender field is unverifiable and we refuse to act on the message regardless
of allowlist membership (a forged From: would otherwise bypass the gate).
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass
from email.message import Message
from email.utils import parseaddr

# Image content types we accept as the "input image". We accept anything
# starting with image/* but explicitly skip GIFs (animated; we'd publish a
# single frame anyway and the user might be surprised) and SVGs (vector;
# Pillow rasterises poorly without a renderer).
ACCEPTED_IMAGE_PREFIX = "image/"
REJECTED_IMAGE_SUBTYPES = frozenset({"image/gif", "image/svg+xml"})

MAX_PROMPT_CHARS = 1000

# Strip common subject-prefixes a phone mail client might add.
SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(re|fwd?|aw|sv)\s*:\s*", flags=re.IGNORECASE
)

# Signature markers — drop everything from this line on. Conservative list;
# we'd rather pass a few extra lines through than truncate a legitimate
# prompt. The "-- " (two dashes + space) form is the RFC 3676 standard.
SIGNATURE_LINES = (
    "-- ",
    "Sent from my iPhone",
    "Sent from my Android",
    "Sent from my mobile",
    "Get Outlook for iOS",
    "Get Outlook for Android",
)


@dataclass(frozen=True)
class ParsedEmail:
    sender: str | None  # the verified From: address, lowercased
    prompt: str | None  # cleaned subject/body, or None if neither produced one
    image_bytes: bytes | None  # decoded body of the first acceptable image
    image_filename: str | None  # attachment filename if present, else None
    reject_reason: str | None = None  # set when sender auth couldn't be verified


def parse_message(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw)
    sender = _verified_sender(msg)
    if sender is None:
        return ParsedEmail(
            sender=_unverified_from(msg),
            prompt=None,
            image_bytes=None,
            image_filename=None,
            reject_reason="sender authentication (SPF/DKIM) did not pass",
        )

    subject = (msg.get("Subject") or "").strip()
    subject = SUBJECT_PREFIX_RE.sub("", subject).strip()

    body_text = _first_text_part(msg)
    body_prompt = _first_meaningful_line(body_text) if body_text else ""

    prompt = subject or body_prompt
    prompt = prompt[:MAX_PROMPT_CHARS] if prompt else None

    image_bytes, image_filename = _first_image_attachment(msg)

    return ParsedEmail(
        sender=sender,
        prompt=prompt,
        image_bytes=image_bytes,
        image_filename=image_filename,
    )


def _unverified_from(msg: Message) -> str | None:
    """Return the From: address without auth verification (display only)."""
    raw_from = msg.get("From") or ""
    _, addr = parseaddr(raw_from)
    return addr.lower() or None


def _verified_sender(msg: Message) -> str | None:
    """Return the From: address iff SES says SPF or DKIM passed for it.

    SES emits one ``Authentication-Results`` header per inbound message. The
    string typically looks like::

        amazonses.com; spf=pass smtp.mailfrom=foo@example.com; dkim=pass
        header.d=example.com; dmarc=pass header.from=example.com

    We accept the message if either SPF or DKIM is ``pass`` AND the relevant
    identity matches the From: domain. Anything else (none, fail, neutral,
    softfail, temperror, permerror, missing header) → unverified.
    """
    raw_from = msg.get("From") or ""
    _, from_addr = parseaddr(raw_from)
    from_addr = from_addr.lower()
    if "@" not in from_addr:
        return None
    from_domain = from_addr.split("@", 1)[1]

    # SES may emit multiple Authentication-Results headers if the message
    # passed through additional authservers. We accept on first hit.
    for header in msg.get_all("Authentication-Results") or []:
        if _auth_header_passes(header, from_addr, from_domain):
            return from_addr
    return None


def _auth_header_passes(header: str, from_addr: str, from_domain: str) -> bool:
    """Inspect one Authentication-Results header for SPF / DKIM / DMARC pass.

    Real SES headers from Gmail look like::

        Authentication-Results: amazonses.com;
          spf=pass (spfCheck: domain of gmail.com designates 209.85.220.41 as
                    permitted sender) smtp.mailfrom=user@gmail.com;
          dkim=pass header.i=@gmail.com;
          dmarc=pass (p=NONE sp=QUARANTINE) header.from=gmail.com

    Note ``header.i=@gmail.com`` (with the ``@``), not ``header.d=``. We
    accept any of three signals: DMARC pass aligned with header.from
    (strongest), SPF pass aligned with the envelope sender, or DKIM pass
    aligned with ``header.i`` or ``header.d``. DMARC=pass is checked first
    because it requires alignment by definition — if DMARC passed, identity
    is real.
    """
    lower = header.lower()

    # DMARC=pass is the strongest signal: by definition it means SPF or
    # DKIM passed AND aligned with the From: domain. Trust it directly.
    dmarc_match = re.search(r"dmarc=(\w+)", lower)
    if dmarc_match and dmarc_match.group(1) == "pass":
        dmarc_from = re.search(r"header\.from=([^\s;]+)", lower)
        if dmarc_from and from_domain == dmarc_from.group(1).strip():
            return True

    spf_match = re.search(r"spf=(\w+)", lower)
    if spf_match and spf_match.group(1) == "pass":
        # SPF identity is the envelope sender (smtp.mailfrom); accept it as
        # a proxy for sender authenticity even though it differs from the
        # header From: in forwarding cases.
        spf_id = re.search(r"smtp\.mailfrom=([^\s;]+)", lower)
        if spf_id and from_domain in spf_id.group(1):
            return True
        # Fall through and require DKIM if SPF identity doesn't align.

    dkim_match = re.search(r"dkim=(\w+)", lower)
    if dkim_match and dkim_match.group(1) == "pass":
        # header.d=DOMAIN (signing domain) — used by SES's own DKIM.
        dkim_d = re.search(r"header\.d=([^\s;]+)", lower)
        if dkim_d and from_domain == dkim_d.group(1).strip():
            return True
        # header.i=@DOMAIN (signing identity, with leading "@") — Gmail and
        # most major mail systems emit this through SES.
        dkim_i = re.search(r"header\.i=@?([^\s;]+)", lower)
        if dkim_i and from_domain == dkim_i.group(1).strip():
            return True

    return False


def _first_text_part(msg: Message) -> str:
    """Return the first text/plain part as a decoded string, or ''.

    Falls back to text/html stripped of tags if no plain part exists — rare
    in iOS Mail, common in some Android mail clients.
    """
    if not msg.is_multipart():
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            return _decode_text(msg)
        if ctype == "text/html":
            return _strip_html(_decode_text(msg))
        return ""

    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        # Skip attachments — they have a filename or a non-inline disposition.
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_text(part)
        elif ctype == "text/html" and html is None:
            html = _decode_text(part)
    if plain is not None:
        return plain
    if html is not None:
        return _strip_html(html)
    return ""


def _decode_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        # Unknown encoding — best-effort utf-8.
        return payload.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    # Minimal — just enough to extract a prompt. We don't render anything.
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _first_meaningful_line(text: str) -> str:
    """Pluck the first prose line, ignoring quoted replies and signatures."""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith(">"):  # quoted reply
            continue
        if line in SIGNATURE_LINES or any(line.startswith(s) for s in SIGNATURE_LINES):
            return ""  # signature hit before any prose
        return line.strip()
    return ""


def _first_image_attachment(msg: Message) -> tuple[bytes | None, str | None]:
    """Return (bytes, filename) for the first acceptable image part."""
    if not msg.is_multipart():
        # A single-part message with image content type — rare but valid.
        ctype = msg.get_content_type()
        if ctype.startswith(ACCEPTED_IMAGE_PREFIX) and ctype not in REJECTED_IMAGE_SUBTYPES:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload, msg.get_filename() or "image"
        return None, None

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if not ctype.startswith(ACCEPTED_IMAGE_PREFIX):
            continue
        if ctype in REJECTED_IMAGE_SUBTYPES:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        filename = part.get_filename() or _filename_from_content_type(ctype)
        return payload, filename
    return None, None


def _filename_from_content_type(ctype: str) -> str:
    subtype = ctype.split("/", 1)[1] if "/" in ctype else "bin"
    return f"image.{subtype}"
