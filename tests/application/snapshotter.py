"""DepotLedger snapshotter.

Writes a complete JSON Lines snapshot of the stock table to S3 on a fixed
interval, plus a baseline snapshot the moment it first sees a table
generation. Every snapshot is written in two steps: the data object, then a
commit marker carrying the data object's sha256 and row count. Only a
snapshot with a matching marker is complete. After committing, it overwrites
snapshots/LATEST.json to point at the newest committed snapshot; the bucket's
version history is what keeps older pointers.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from awslite import AwsError, from_item
from common import LATEST_KEY, clients, commit_key, log, required, snapshot_key, table_generation

STOCK_TABLE = required("STOCK_TABLE")
SNAPSHOT_BUCKET = required("SNAPSHOT_BUCKET")
INTERVAL = max(5, int(os.environ.get("SNAPSHOT_INTERVAL_SECONDS", "15")))
POLL_SECONDS = 1.0

DDB, S3 = clients()


def scan_all() -> list[dict]:
    items: list[dict] = []
    start = None
    while True:
        request = {"TableName": STOCK_TABLE, "ConsistentRead": True}
        if start:
            request["ExclusiveStartKey"] = start
        result = DDB.call("Scan", request)
        items.extend(from_item(i) for i in result.get("Items", []))
        start = result.get("LastEvaluatedKey")
        if not start:
            return sorted(items, key=lambda i: (i.get("sku", ""), i.get("warehouse_id", "")))


def take_snapshot(generation: str, reason: str) -> None:
    items = scan_all()
    taken_at = int(time.time() * 1000)
    key = snapshot_key(generation, taken_at)
    body = "".join(json.dumps(item, sort_keys=True) + "\n" for item in items).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    # Step 1: the data object. A crash here leaves an uncommitted snapshot.
    S3.put_object(SNAPSHOT_BUCKET, key, body, content_type="application/x-ndjson", metadata={
        "item-count": str(len(items)),
        "table-generation": generation,
        "taken-at-ms": str(taken_at),
    })
    # Step 2: the commit marker. Only now is the snapshot restorable.
    pointer = {"key": key, "generation": generation, "item_count": len(items),
               "taken_at_ms": taken_at, "sha256": digest}
    S3.put_object(SNAPSHOT_BUCKET, commit_key(key), json.dumps(pointer).encode("utf-8"),
                  content_type="application/json")
    S3.put_object(SNAPSHOT_BUCKET, LATEST_KEY, json.dumps(pointer).encode("utf-8"),
                  content_type="application/json")
    log("snapshot_written", key=key, generation=generation, item_count=len(items), reason=reason)


def main() -> None:
    log("snapshotter_starting", stock_table=STOCK_TABLE, bucket=SNAPSHOT_BUCKET, interval=INTERVAL)
    last_generation: str | None = None
    last_snapshot = 0.0
    while True:
        try:
            generation = table_generation(DDB, STOCK_TABLE)
            if generation is None:
                if last_generation is not None:
                    log("table_missing", stock_table=STOCK_TABLE)
                last_generation = None
            elif generation != last_generation:
                take_snapshot(generation, "baseline")
                last_generation, last_snapshot = generation, time.monotonic()
            elif time.monotonic() - last_snapshot >= INTERVAL:
                take_snapshot(generation, "interval")
                last_snapshot = time.monotonic()
        except AwsError as exc:
            log("snapshot_failed", error=exc.code, status=exc.status)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log("snapshot_failed", error=type(exc).__name__)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
