"""Tests for `einkgen status`."""

from __future__ import annotations

import json
import time

from einkgen.cli import main


def _put_status(client, bucket: str, device_id: str, payload: dict) -> None:
    client.put_object(
        Bucket=bucket,
        Key=f"status/device-{device_id}.json",
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )


def test_status_prints_latest_report(s3_bucket, capsys):
    older = {
        "battery_v": 3.6,
        "battery_pct": 50,
        "rssi": -70,
        "last_seen": "2026-05-13T10:00:00Z",
        "current_hash": "aaaa",
        "fw_version": "0.1.0",
    }
    _put_status(s3_bucket, "einkgen-test", "01OLD", older)

    # Ensure the second object sorts later by LastModified.
    time.sleep(0.01)

    newer = {
        "battery_v": 3.9,
        "battery_pct": 88,
        "rssi": -55,
        "last_seen": "2026-05-13T14:00:00Z",
        "current_hash": "bbbb",
        "fw_version": "0.1.1",
    }
    _put_status(s3_bucket, "einkgen-test", "01NEW", newer)

    rc = main(["status"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "status/device-01NEW.json" in out
    assert "3.9" in out
    assert "88" in out
    assert "-55" in out
    assert "2026-05-13T14:00:00Z" in out
    assert "bbbb" in out
    assert "0.1.1" in out


def test_status_when_no_reports(s3_bucket, capsys):
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No device status reports found." in out
