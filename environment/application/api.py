"""DepotLedger inventory API.

Serves stock levels and reservations over HTTP. All state lives in two
DynamoDB tables; snapshots written by the snapshotter live in S3 and can be
restored through the admin surface.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import socket
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from awslite import AwsError, from_item, to_attr, to_item
from common import (LOW_STOCK_INDEX, SNAPSHOT_PREFIX, WAREHOUSE_INDEX, clients, log,
                    parse_snapshot_key, required, table_generation)

STOCK_TABLE = required("STOCK_TABLE")
RESERVATIONS_TABLE = required("RESERVATIONS_TABLE")
SNAPSHOT_BUCKET = required("SNAPSHOT_BUCKET")
ADMIN_TOKEN = required("ADMIN_TOKEN")
IDEMPOTENCY_TTL = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "86400"))
PORT = int(os.environ.get("PORT", "8080"))
REPLICA = socket.gethostname()

DDB, S3 = clients()

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
STOCK_FIELDS = ("on_hand", "reserved", "available", "reorder_point")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_id(value: str, field: str) -> str:
    if not ID_PATTERN.match(value or ""):
        raise ApiError(400, "invalid_request", f"{field} is not a valid identifier")
    return value


def public_stock(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku": item["sku"],
        "warehouse_id": item["warehouse_id"],
        "on_hand": item["on_hand"],
        "reserved": item["reserved"],
        "available": item["available"],
        "reorder_point": item["reorder_point"],
        "low_stock": item["available"] <= item["reorder_point"],
        "updated_at": item.get("updated_at"),
    }


def stock_key(sku: str, warehouse_id: str) -> dict[str, Any]:
    return {"sku": {"S": sku}, "warehouse_id": {"S": warehouse_id}}


def get_stock(sku: str, warehouse_id: str) -> dict[str, Any] | None:
    found = DDB.call("GetItem", {"TableName": STOCK_TABLE, "Key": stock_key(sku, warehouse_id),
                                 "ConsistentRead": True})
    return from_item(found["Item"]) if found.get("Item") else None


# -- stock -------------------------------------------------------------------

def put_stock(sku: str, warehouse_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    on_hand, reorder_point = body.get("on_hand"), body.get("reorder_point", 0)
    if not isinstance(on_hand, int) or isinstance(on_hand, bool) or on_hand < 0:
        raise ApiError(400, "invalid_request", "on_hand must be a non-negative integer")
    if not isinstance(reorder_point, int) or isinstance(reorder_point, bool) or reorder_point < 0:
        raise ApiError(400, "invalid_request", "reorder_point must be a non-negative integer")

    for _ in range(20):
        current = get_stock(sku, warehouse_id)
        reserved = current["reserved"] if current else 0
        if on_hand < reserved:
            raise ApiError(409, "below_reserved", "on_hand cannot drop below reserved units")
        version = (current["version"] if current else 0) + 1
        record = {
            "sku": sku, "warehouse_id": warehouse_id, "on_hand": on_hand,
            "reserved": reserved, "available": on_hand - reserved,
            "reorder_point": reorder_point, "version": version, "updated_at": now_iso(),
        }
        if record["available"] <= reorder_point:
            record["low_stock_warehouse"] = warehouse_id
        request: dict[str, Any] = {"TableName": STOCK_TABLE, "Item": to_item(record)}
        if current:
            request["ConditionExpression"] = "version = :v"
            request["ExpressionAttributeValues"] = {":v": to_attr(current["version"])}
        else:
            request["ConditionExpression"] = "attribute_not_exists(sku)"
        try:
            DDB.call("PutItem", request)
        except AwsError as exc:
            if exc.code == "ConditionalCheckFailedException":
                continue
            raise
        return (200 if current else 201), public_stock(record)
    raise ApiError(503, "write_contention", "stock record is under heavy contention")


def delete_stock(sku: str, warehouse_id: str) -> tuple[int, dict[str, Any]]:
    removed = DDB.call("DeleteItem", {"TableName": STOCK_TABLE, "Key": stock_key(sku, warehouse_id),
                                      "ReturnValues": "ALL_OLD"})
    if not removed.get("Attributes"):
        raise ApiError(404, "not_found", "no stock record for that sku and warehouse")
    return 200, {"deleted": True, "sku": sku, "warehouse_id": warehouse_id}


def stock_for_sku(sku: str) -> tuple[int, dict[str, Any]]:
    result = DDB.call("Query", {
        "TableName": STOCK_TABLE, "ConsistentRead": True,
        "KeyConditionExpression": "sku = :s",
        "ExpressionAttributeValues": {":s": {"S": sku}},
    })
    items = [public_stock(from_item(i)) for i in result.get("Items", [])]
    if not items:
        raise ApiError(404, "not_found", "unknown sku")
    return 200, {"sku": sku, "locations": sorted(items, key=lambda i: i["warehouse_id"])}


def query_index(index: str, key_name: str, value: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = None
    while True:
        request: dict[str, Any] = {
            "TableName": STOCK_TABLE, "IndexName": index,
            "KeyConditionExpression": "#k = :v",
            "ExpressionAttributeNames": {"#k": key_name},
            "ExpressionAttributeValues": {":v": {"S": value}},
        }
        if start:
            request["ExclusiveStartKey"] = start
        try:
            result = DDB.call("Query", request)
        except AwsError as exc:
            if exc.code in ("ResourceNotFoundException", "ValidationException"):
                raise ApiError(500, "index_unavailable",
                               f"index {index} cannot be queried: {exc.code}") from exc
            raise
        items.extend(from_item(i) for i in result.get("Items", []))
        start = result.get("LastEvaluatedKey")
        if not start:
            return items


def require_projection(index: str, items: list[dict[str, Any]]) -> None:
    for item in items:
        missing = [field for field in STOCK_FIELDS if field not in item]
        if missing:
            raise ApiError(500, "index_projection_incomplete",
                           f"index {index} does not project {', '.join(missing)}")


def warehouse_stock(warehouse_id: str) -> tuple[int, dict[str, Any]]:
    items = query_index(WAREHOUSE_INDEX, "warehouse_id", warehouse_id)
    require_projection(WAREHOUSE_INDEX, items)
    rows = sorted((public_stock(i) for i in items), key=lambda i: i["sku"])
    return 200, {"warehouse_id": warehouse_id, "items": rows}


def warehouse_low_stock(warehouse_id: str) -> tuple[int, dict[str, Any]]:
    items = query_index(LOW_STOCK_INDEX, "low_stock_warehouse", warehouse_id)
    require_projection(LOW_STOCK_INDEX, items)
    rows = sorted((public_stock(i) for i in items), key=lambda i: i["sku"])
    return 200, {"warehouse_id": warehouse_id, "items": rows}


# -- reservations --------------------------------------------------------------

def public_reservation(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in (
        "order_id", "reservation_id", "sku", "warehouse_id", "quantity", "status", "created_at")}


def refresh_low_stock_marker(sku: str, warehouse_id: str, item: dict[str, Any]) -> None:
    """Keep the sparse index attribute in step with the version just written.

    Conditioned on the version so a slower writer cannot overwrite the marker
    computed from a newer stock level.
    """
    low = item["available"] <= item["reorder_point"]
    expression = "SET low_stock_warehouse = :w" if low else "REMOVE low_stock_warehouse"
    values: dict[str, Any] = {":v": to_attr(item["version"])}
    if low:
        values[":w"] = {"S": warehouse_id}
    try:
        DDB.call("UpdateItem", {
            "TableName": STOCK_TABLE, "Key": stock_key(sku, warehouse_id),
            "UpdateExpression": expression, "ConditionExpression": "version = :v",
            "ExpressionAttributeValues": values,
        })
    except AwsError as exc:
        if exc.code != "ConditionalCheckFailedException":
            raise


def create_reservation(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    order_id = check_id(str(body.get("order_id", "")), "order_id")
    sku = check_id(str(body.get("sku", "")), "sku")
    warehouse_id = check_id(str(body.get("warehouse_id", "")), "warehouse_id")
    quantity = body.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 1000:
        raise ApiError(400, "invalid_request", "quantity must be an integer from 1 to 1000")

    created = int(time.time())
    record = {
        "order_id": order_id, "reservation_id": str(uuid.uuid4()), "sku": sku,
        "warehouse_id": warehouse_id, "quantity": quantity, "status": "pending",
        "created_at": now_iso(), "expires_at": created + IDEMPOTENCY_TTL,
    }
    # 1. Claim the order id. A replay finds the claim and returns the original.
    try:
        DDB.call("PutItem", {"TableName": RESERVATIONS_TABLE, "Item": to_item(record),
                             "ConditionExpression": "attribute_not_exists(order_id)"})
    except AwsError as exc:
        if exc.code != "ConditionalCheckFailedException":
            raise
        existing = DDB.call("GetItem", {"TableName": RESERVATIONS_TABLE, "ConsistentRead": True,
                                        "Key": {"order_id": {"S": order_id}}}).get("Item")
        if not existing:
            raise ApiError(409, "reservation_in_progress", "retry the request") from exc
        prior = from_item(existing)
        if (prior.get("sku"), prior.get("warehouse_id"), prior.get("quantity")) != (sku, warehouse_id, quantity):
            raise ApiError(409, "idempotency_conflict",
                           "order_id was already used for a different reservation") from exc
        if prior.get("status") != "confirmed":
            raise ApiError(409, "reservation_in_progress", "retry the request") from exc
        return 200, public_reservation(prior)

    # 2. Take the units. The condition is evaluated atomically by the table,
    # so concurrent replicas can never drive available below zero.
    try:
        updated = DDB.call("UpdateItem", {
            "TableName": STOCK_TABLE, "Key": stock_key(sku, warehouse_id),
            "UpdateExpression": "SET available = available - :q, reserved = reserved + :q, "
                                "version = version + :one, updated_at = :t",
            "ConditionExpression": "attribute_exists(sku) AND available >= :q",
            "ExpressionAttributeValues": {":q": to_attr(quantity), ":one": to_attr(1),
                                          ":t": {"S": now_iso()}},
            "ReturnValues": "ALL_NEW",
        })
    except AwsError as exc:
        DDB.call("DeleteItem", {"TableName": RESERVATIONS_TABLE, "Key": {"order_id": {"S": order_id}}})
        if exc.code == "ConditionalCheckFailedException":
            raise ApiError(409, "insufficient_stock", "not enough available units") from exc
        raise
    refresh_low_stock_marker(sku, warehouse_id, from_item(updated["Attributes"]))

    # 3. Confirm the claim.
    record["status"] = "confirmed"
    DDB.call("UpdateItem", {
        "TableName": RESERVATIONS_TABLE, "Key": {"order_id": {"S": order_id}},
        "UpdateExpression": "SET #s = :c", "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":c": {"S": "confirmed"}},
    })
    return 201, public_reservation(record)


def get_reservation(order_id: str) -> tuple[int, dict[str, Any]]:
    found = DDB.call("GetItem", {"TableName": RESERVATIONS_TABLE, "ConsistentRead": True,
                                 "Key": {"order_id": {"S": order_id}}}).get("Item")
    if not found:
        raise ApiError(404, "not_found", "unknown order_id")
    return 200, public_reservation(from_item(found))


# -- admin -------------------------------------------------------------------

def list_snapshots() -> tuple[int, dict[str, Any]]:
    current = table_generation(DDB, STOCK_TABLE)
    snapshots = []
    for key in S3.list_keys(SNAPSHOT_BUCKET, SNAPSHOT_PREFIX):
        parsed = parse_snapshot_key(key)
        if not parsed:
            continue
        generation, taken_at = parsed
        try:
            meta = S3.head_object(SNAPSHOT_BUCKET, key)
            count = int(meta.get("x-amz-meta-item-count", "-1"))
        except (AwsError, ValueError):
            count = -1
        snapshots.append({"key": key, "generation": generation,
                          "taken_at_ms": taken_at, "item_count": count})
    snapshots.sort(key=lambda s: s["taken_at_ms"], reverse=True)
    return 200, {"current_generation": current, "snapshots": snapshots}


def restore(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    key = str(body.get("snapshot_key", ""))
    parsed = parse_snapshot_key(key)
    if not parsed:
        raise ApiError(400, "invalid_request", "snapshot_key is not a snapshot object key")
    generation, _ = parsed
    current = table_generation(DDB, STOCK_TABLE)
    if current is None:
        raise ApiError(503, "table_unavailable", "the stock table does not exist")
    if generation == current:
        raise ApiError(409, "snapshot_generation_current",
                       "a snapshot of the current table generation cannot be restored into it")
    version_id = body.get("version_id")
    if version_id is not None and (not isinstance(version_id, str) or not version_id.strip()):
        raise ApiError(400, "invalid_request", "version_id must be a non-empty string")
    try:
        # Without version_id the current version is loaded. With it, exactly
        # that object version, which is how committed content is recovered
        # after the data object was overwritten.
        raw, _ = S3.get_object(SNAPSHOT_BUCKET, key, version_id)
    except AwsError as exc:
        if exc.status in (400, 404):
            raise ApiError(404, "not_found", "snapshot object or version does not exist") from exc
        raise
    restored = skipped = 0
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        try:
            # Never overwrite: anything already present is newer than the snapshot.
            DDB.call("PutItem", {"TableName": STOCK_TABLE, "Item": to_item(item),
                                 "ConditionExpression": "attribute_not_exists(sku)"})
            restored += 1
        except AwsError as exc:
            if exc.code != "ConditionalCheckFailedException":
                raise
            skipped += 1
    log("restore_completed", snapshot_key=key, version_id=version_id, restored=restored, skipped=skipped)
    return 200, {"snapshot_key": key, "version_id": version_id, "generation": generation,
                 "restored": restored, "skipped": skipped}


# -- HTTP ----------------------------------------------------------------------

def ready() -> tuple[int, dict[str, Any]]:
    for table in (STOCK_TABLE, RESERVATIONS_TABLE):
        try:
            status = DDB.call("DescribeTable", {"TableName": table})["Table"].get("TableStatus")
        except AwsError as exc:
            raise ApiError(503, "not_ready", f"{table}: {exc.code}") from exc
        if status != "ACTIVE":
            raise ApiError(503, "not_ready", f"{table} is {status}")
    return 200, {"status": "ready", "replica": REPLICA}


ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/health/live$"), "live"),
    ("GET", re.compile(r"^/health/ready$"), "ready"),
    ("PUT", re.compile(r"^/stock/(?P<sku>[^/]+)/(?P<wh>[^/]+)$"), "put_stock"),
    ("DELETE", re.compile(r"^/stock/(?P<sku>[^/]+)/(?P<wh>[^/]+)$"), "delete_stock"),
    ("GET", re.compile(r"^/stock/(?P<sku>[^/]+)$"), "stock_for_sku"),
    ("GET", re.compile(r"^/warehouses/(?P<wh>[^/]+)/stock$"), "warehouse_stock"),
    ("GET", re.compile(r"^/warehouses/(?P<wh>[^/]+)/low-stock$"), "warehouse_low_stock"),
    ("POST", re.compile(r"^/reservations$"), "create_reservation"),
    ("GET", re.compile(r"^/reservations/(?P<order>[^/]+)$"), "get_reservation"),
    ("GET", re.compile(r"^/admin/snapshots$"), "list_snapshots"),
    ("POST", re.compile(r"^/admin/restore$"), "restore"),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DepotLedger/1.0"

    def log_message(self, *_args: Any) -> None:  # structured logging below instead
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ApiError(413, "payload_too_large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ApiError(400, "invalid_json") from exc
        if not isinstance(value, dict):
            raise ApiError(400, "invalid_json", "body must be a JSON object")
        return value

    def _admin(self) -> None:
        supplied = self.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(supplied.encode(), ADMIN_TOKEN.encode()):
            raise ApiError(401, "unauthorized")

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        request_id = self.headers.get("X-Request-Id") or str(uuid.uuid4())
        path = self.path.split("?", 1)[0]
        status, payload = 404, {"code": "not_found"}
        try:
            for verb, pattern, name in ROUTES:
                match = pattern.match(path)
                if not match or verb != method:
                    continue
                args = match.groupdict()
                if name == "live":
                    status, payload = 200, {"status": "live"}
                elif name == "ready":
                    status, payload = ready()
                elif name == "put_stock":
                    status, payload = put_stock(check_id(args["sku"], "sku"),
                                                check_id(args["wh"], "warehouse_id"), self._body())
                elif name == "delete_stock":
                    status, payload = delete_stock(check_id(args["sku"], "sku"),
                                                   check_id(args["wh"], "warehouse_id"))
                elif name == "stock_for_sku":
                    status, payload = stock_for_sku(check_id(args["sku"], "sku"))
                elif name == "warehouse_stock":
                    status, payload = warehouse_stock(check_id(args["wh"], "warehouse_id"))
                elif name == "warehouse_low_stock":
                    status, payload = warehouse_low_stock(check_id(args["wh"], "warehouse_id"))
                elif name == "create_reservation":
                    status, payload = create_reservation(self._body())
                elif name == "get_reservation":
                    status, payload = get_reservation(check_id(args["order"], "order_id"))
                elif name == "list_snapshots":
                    self._admin()
                    status, payload = list_snapshots()
                elif name == "restore":
                    self._admin()
                    status, payload = restore(self._body())
                break
        except ApiError as exc:
            status, payload = exc.status, {"code": exc.code, "detail": exc.detail}
        except AwsError as exc:
            status, payload = 502, {"code": "storage_error", "detail": exc.code}
        except Exception as exc:  # noqa: BLE001 - reported, never crashes the server
            status, payload = 500, {"code": "internal_error", "detail": type(exc).__name__}

        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", request_id)
        self.send_header("X-DepotLedger-Replica", REPLICA)
        self.end_headers()
        self.wfile.write(body)
        if not path.startswith("/health/"):
            log("request", request_id=request_id, method=method, path=path, status=status,
                replica=REPLICA, duration_ms=int((time.monotonic() - started) * 1000))

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


def main() -> None:
    log("api_starting", port=PORT, replica=REPLICA, stock_table=STOCK_TABLE,
        reservations_table=RESERVATIONS_TABLE, snapshot_bucket=SNAPSHOT_BUCKET)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
