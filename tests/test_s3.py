"""Tests for the thin S3 wrapper."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from einkgen.core import s3


def test_put_get_list_delete_round_trip(s3_bucket):
    s3.put_object("foo/bar.txt", b"hello", content_type="text/plain")

    body = s3.get_object("foo/bar.txt")
    assert body == b"hello"

    listing = s3.list_objects("foo/")
    assert len(listing) == 1
    assert listing[0]["Key"] == "foo/bar.txt"
    assert listing[0]["Size"] == 5
    assert "LastModified" in listing[0]

    head = s3.head_object("foo/bar.txt")
    assert head is not None
    assert head["ContentLength"] == 5

    s3.delete_object("foo/bar.txt")
    assert s3.list_objects("foo/") == []


def test_head_object_missing_returns_none(s3_bucket):
    assert s3.head_object("does/not/exist.json") is None


def test_list_objects_paginates_across_pages(aws_env):
    """We don't want to materialise 1000+ objects in moto — instead
    confirm that `list_objects` walks every page the paginator yields."""
    fake_client = MagicMock()
    fake_paginator = MagicMock()
    fake_client.get_paginator.return_value = fake_paginator

    page_1 = {
        "Contents": [
            {
                "Key": f"queue/item-{i:04d}.json",
                "LastModified": datetime(2026, 5, 13, tzinfo=timezone.utc),
                "Size": 100 + i,
            }
            for i in range(1000)
        ]
    }
    page_2 = {
        "Contents": [
            {
                "Key": f"queue/item-{i:04d}.json",
                "LastModified": datetime(2026, 5, 13, tzinfo=timezone.utc),
                "Size": 100 + i,
            }
            for i in range(1000, 1500)
        ]
    }
    fake_paginator.paginate.return_value = iter([page_1, page_2])

    with patch.object(s3, "get_client", return_value=fake_client):
        results = s3.list_objects("queue/")

    assert len(results) == 1500
    assert results[0]["Key"] == "queue/item-0000.json"
    assert results[-1]["Key"] == "queue/item-1499.json"
    fake_client.get_paginator.assert_called_once_with("list_objects_v2")
    fake_paginator.paginate.assert_called_once()
