"""Shared helpers for the DepotLedger images."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from awslite import AwsError, DynamoDB, S3

WAREHOUSE_INDEX = "by_warehouse"
LOW_STOCK_INDEX = "low_stock"
SNAPSHOT_PREFIX = "snapshots/"
LATEST_KEY = "snapshots/LATEST.json"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log("config_missing", variable=name)
        sys.exit(f"environment variable {name} is required")
    return value


def log(event: str, **fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    record.update(fields)
    print(json.dumps(record, default=str), flush=True)


def clients() -> tuple[DynamoDB, S3]:
    endpoint = required("AWS_ENDPOINT_URL")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return DynamoDB(endpoint, region), S3(endpoint, region)


def table_generation(ddb: DynamoDB, table: str) -> str | None:
    """Identifies one incarnation of a table.

    A table that is deleted and created again under the same name is a new
    generation. The value is the table's creation time in epoch milliseconds.
    Returns None when the table does not exist.
    """
    try:
        described = ddb.call("DescribeTable", {"TableName": table})["Table"]
    except AwsError as exc:
        if exc.code == "ResourceNotFoundException":
            return None
        raise
    created = described.get("CreationDateTime")
    if created is None:
        return described.get("TableId") or described.get("TableArn")
    return str(int(round(float(created) * 1000)))


def snapshot_key(generation: str, taken_at_ms: int) -> str:
    return f"{SNAPSHOT_PREFIX}{generation}/{taken_at_ms}.jsonl"


def commit_key(data_key: str) -> str:
    """The commit marker that makes a snapshot restorable."""
    return data_key[: -len(".jsonl")] + ".committed"


def parse_snapshot_key(key: str) -> tuple[str, int] | None:
    if not key.startswith(SNAPSHOT_PREFIX) or not key.endswith(".jsonl"):
        return None
    parts = key[len(SNAPSHOT_PREFIX):-len(".jsonl")].split("/")
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1])
