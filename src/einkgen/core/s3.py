"""Thin boto3 wrapper for the einkgen bucket.

All bucket I/O goes through here so tests can patch a single seam
(`get_client`) and the rest of the code stays bucket-agnostic.
"""

from __future__ import annotations

import os
from typing import Any

import boto3

_client = None


def get_client():
    """Return a cached boto3 S3 client.

    Tests should patch this module's `_client` attribute (or call
    `reset_client()`) to swap in a moto-backed client.
    """
    global _client
    if _client is None:
        _client = boto3.client("s3")
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests between fixtures."""
    global _client
    _client = None


def _bucket() -> str:
    bucket = os.environ.get("EINKGEN_BUCKET")
    if not bucket:
        raise RuntimeError(
            "EINKGEN_BUCKET environment variable is not set; "
            "see .env.example for the expected value."
        )
    return bucket


def get_object(key: str) -> bytes:
    """Fetch the full body of an S3 object as bytes."""
    resp = get_client().get_object(Bucket=_bucket(), Key=key)
    return resp["Body"].read()


def put_object(
    key: str,
    body: bytes,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload bytes to S3 with an explicit Content-Type."""
    get_client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=body,
        ContentType=content_type,
    )


def delete_object(key: str) -> None:
    """Delete a single object."""
    get_client().delete_object(Bucket=_bucket(), Key=key)


def list_objects(prefix: str) -> list[dict[str, Any]]:
    """List every object under `prefix`, paginating transparently.

    Returns a list of dicts with at least `Key`, `LastModified`, `Size`.
    """
    client = get_client()
    paginator = client.get_paginator("list_objects_v2")
    out: list[dict[str, Any]] = []
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for item in page.get("Contents", []) or []:
            out.append(
                {
                    "Key": item["Key"],
                    "LastModified": item["LastModified"],
                    "Size": item["Size"],
                }
            )
    return out


def head_object(key: str) -> dict[str, Any] | None:
    """Return object metadata, or None if the object does not exist."""
    client = get_client()
    try:
        return client.head_object(Bucket=_bucket(), Key=key)
    except client.exceptions.ClientError as exc:  # pragma: no cover - defensive
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
