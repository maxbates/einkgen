"""`einkgen status` — show the latest device status report."""

from __future__ import annotations

import argparse
import json

from einkgen.core import s3

STATUS_PREFIX = "status/"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("status", help="Show the latest device status report.")
    p.set_defaults(func=run)


def run(_args: argparse.Namespace) -> int:
    objects = s3.list_objects(STATUS_PREFIX)
    # Only consider device-*.json files; ignore any stray non-JSON keys.
    candidates = [
        o for o in objects
        if o["Key"].startswith("status/device-") and o["Key"].endswith(".json")
    ]
    if not candidates:
        print("No device status reports found.")
        return 0

    latest = max(candidates, key=lambda o: o["LastModified"])
    body = s3.get_object(latest["Key"])
    report = json.loads(body)

    print(f"Device:        {latest['Key']}")
    print(f"Last seen:     {report.get('last_seen', '?')}")
    print(f"Battery (V):   {report.get('battery_v', '?')}")
    print(f"Battery (%):   {report.get('battery_pct', '?')}")
    print(f"RSSI:          {report.get('rssi', '?')}")
    print(f"Current hash:  {report.get('current_hash', '?')}")
    print(f"FW version:    {report.get('fw_version', '?')}")
    return 0
