"""Observed plane.

Real requests through the real load balancer with fresh data for every run.
These checks assert on product results, meaning stock levels, reservation
outcomes and snapshot contents. They never assert on resource labels or the
reference layout.
"""
from __future__ import annotations

import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from .tools.errors import Oversell, SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.trial import TrialContext, obligation


def fresh(label: str) -> str:
    return f"{label}-{secrets.token_hex(4)}"


def put_stock(trial: TrialContext, sku: str, warehouse: str, on_hand: int, reorder_point: int = 0) -> dict:
    response = trial.api.put(f"/stock/{sku}/{warehouse}", {"on_hand": on_hand, "reorder_point": reorder_point})
    if response.status not in (200, 201):
        raise SubmissionFailure(
            f"PUT /stock/{sku}/{warehouse} returned {response.status}: {response.text[:300]}")
    return response.json()


def stock_row(trial: TrialContext, sku: str, warehouse: str) -> dict | None:
    response = trial.api.get(f"/stock/{sku}")
    if response.status == 404:
        return None
    if response.status != 200:
        raise SubmissionFailure(f"GET /stock/{sku} returned {response.status}: {response.text[:300]}")
    for row in response.json().get("locations", []):
        if row.get("warehouse_id") == warehouse:
            return row
    return None


def reserve(trial: TrialContext, order_id: str, sku: str, warehouse: str, quantity: int):
    return trial.api.post("/reservations", {"order_id": order_id, "sku": sku,
                                            "warehouse_id": warehouse, "quantity": quantity})


def view(trial: TrialContext, path: str) -> list[dict]:
    response = trial.api.get(path)
    if response.status != 200:
        raise SubmissionFailure(f"GET {path} returned {response.status}: {response.text[:300]}")
    return response.json().get("items", [])


@obligation("observed.stock_roundtrip")
def test_stock_roundtrip(trial: TrialContext) -> CheckResult:
    """Fresh rows read back by SKU and, from the index, by warehouse."""
    warehouse, other = fresh("wh"), fresh("wh")
    written = {}
    for index in range(3):
        sku = fresh(f"sku{index}")
        written[sku] = put_stock(trial, sku, warehouse, on_hand=40 + index, reorder_point=5)
    stray = fresh("sku")
    put_stock(trial, stray, other, on_hand=9)

    for sku, row in written.items():
        found = stock_row(trial, sku, warehouse)
        if not found or found.get("on_hand") != row["on_hand"] or found.get("available") != row["on_hand"]:
            raise SubmissionFailure(f"GET /stock/{sku} returned {found}, expected on_hand {row['on_hand']}")

    items = view(trial, f"/warehouses/{warehouse}/stock")
    seen = {item["sku"]: item for item in items}
    if set(seen) != set(written):
        raise SubmissionFailure(
            f"the warehouse view returned {sorted(seen)}, expected exactly {sorted(written)}")
    for sku, row in written.items():
        if seen[sku].get("on_hand") != row["on_hand"] or seen[sku].get("reorder_point") != 5:
            raise SubmissionFailure(f"the warehouse view returned {seen[sku]} for {sku}")

    replicas = set()
    for _ in range(12):
        replicas.add(trial.api.get(f"/stock/{next(iter(written))}").replica)
    trial.facts["roundtrip_rows"] = {"warehouse": warehouse, "skus": sorted(written)}
    return CheckResult(
        "observed.stock_roundtrip", Outcome.PASS,
        f"{len(written)} fresh rows read back by SKU and from the warehouse index",
        details={"replicas_observed": sorted(r for r in replicas if r)},
    )


@obligation("observed.low_stock_index")
def test_low_stock_index(trial: TrialContext) -> CheckResult:
    """The sparse index follows rows across the reorder point."""
    warehouse, other = fresh("wh"), fresh("wh")
    sku, steady = fresh("sku"), fresh("sku")
    put_stock(trial, sku, warehouse, on_hand=10, reorder_point=3)
    put_stock(trial, steady, warehouse, on_hand=50, reorder_point=3)
    put_stock(trial, fresh("sku"), other, on_hand=1, reorder_point=5)  # low, but elsewhere

    def low_skus() -> set[str]:
        return {item["sku"] for item in view(trial, f"/warehouses/{warehouse}/low-stock")}

    if low_skus():
        raise SubmissionFailure(f"the low-stock view listed {sorted(low_skus())} before any row was low")

    response = reserve(trial, fresh("order"), sku, warehouse, 7)
    if response.status != 201:
        raise SubmissionFailure(f"reservation returned {response.status}: {response.text[:300]}")
    low = low_skus()
    if low != {sku}:
        raise SubmissionFailure(f"after dropping to the reorder point the low-stock view was {sorted(low)}, expected [{sku}]")
    rows = view(trial, f"/warehouses/{warehouse}/low-stock")
    if rows[0].get("available") != 3 or rows[0].get("reserved") != 7:
        raise SubmissionFailure(f"the low-stock view returned {rows[0]}")

    put_stock(trial, sku, warehouse, on_hand=30, reorder_point=3)
    if low_skus():
        raise SubmissionFailure(f"after restocking the low-stock view still listed {sorted(low_skus())}")

    return CheckResult(
        "observed.low_stock_index", Outcome.PASS,
        "the low-stock view gained the row at its reorder point and dropped it after restock",
    )


@obligation("observed.no_oversell")
def test_no_oversell(trial: TrialContext) -> CheckResult:
    """Concurrent reservations never take more units than exist."""
    warehouse, sku = fresh("wh"), fresh("sku")
    units, attempts = 10, 30
    put_stock(trial, sku, warehouse, on_hand=units)

    def attempt(index: int):
        response = reserve(trial, f"{sku}-order-{index}", sku, warehouse, 1)
        return response.status, response.code(), response.replica

    with ThreadPoolExecutor(max_workers=15) as pool:
        outcomes = list(pool.map(attempt, range(attempts)))

    accepted = sum(1 for status, _, _ in outcomes if status == 201)
    refused = sum(1 for status, code, _ in outcomes if status == 409 and code == "insufficient_stock")
    other = [(s, c) for s, c, _ in outcomes if not (s == 201 or (s == 409 and c == "insufficient_stock"))]
    row = stock_row(trial, sku, warehouse) or {}

    if accepted > units or (row.get("available") or 0) < 0 or (row.get("reserved") or 0) > units:
        raise Oversell(f"{accepted} reservations succeeded for {units} units; the row ended as {row}")
    if other:
        raise SubmissionFailure(f"{len(other)} reservations failed unexpectedly: {other[:5]}")
    if accepted != units or refused != attempts - units:
        raise SubmissionFailure(f"{accepted} accepted and {refused} refused, expected {units} and {attempts - units}")
    if row.get("available") != 0 or row.get("reserved") != units:
        raise SubmissionFailure(f"the row ended as {row}, expected available 0 and reserved {units}")

    return CheckResult(
        "observed.no_oversell", Outcome.PASS,
        f"{attempts} concurrent reservations took exactly {units} units",
        details={"replicas": sorted({r for _, _, r in outcomes if r})},
    )


@obligation("observed.idempotent_reservation")
def test_idempotent_reservation(trial: TrialContext) -> CheckResult:
    """Replays return the original and take nothing more."""
    warehouse, sku, order = fresh("wh"), fresh("sku"), fresh("order")
    put_stock(trial, sku, warehouse, on_hand=20)

    started = int(time.time())
    first = reserve(trial, order, sku, warehouse, 2)
    if first.status != 201:
        raise SubmissionFailure(f"the first reservation returned {first.status}: {first.text[:300]}")
    reservation_id = first.json().get("reservation_id")

    replay = reserve(trial, order, sku, warehouse, 2)
    if replay.status != 200 or replay.json().get("reservation_id") != reservation_id:
        raise SubmissionFailure(f"an identical replay returned {replay.status}: {replay.text[:300]}")
    conflict = reserve(trial, order, sku, warehouse, 3)
    if conflict.status != 409 or conflict.code() != "idempotency_conflict":
        raise SubmissionFailure(f"a conflicting replay returned {conflict.status}: {conflict.text[:300]}")

    row = stock_row(trial, sku, warehouse) or {}
    if row.get("reserved") != 2 or row.get("available") != 18:
        raise SubmissionFailure(f"after one reservation and two replays the row is {row}")

    item = trial.cloud.ddb.get_item(TableName=trial.reservations_table,
                                    Key={"order_id": {"S": order}}, ConsistentRead=True).get("Item")
    if not item or "expires_at" not in item:
        raise SubmissionFailure("the reservation claim is not stored in the reservations table")
    expires = int(item["expires_at"]["N"])
    ttl = trial.config.idempotency_ttl
    if not started + ttl - 120 <= expires <= int(time.time()) + ttl + 120:
        raise SubmissionFailure(f"the claim expires at {expires}, not about {ttl}s after creation")

    trial.facts["replay_order"] = {"order_id": order, "sku": sku, "warehouse": warehouse,
                                   "reservation_id": reservation_id}
    return CheckResult(
        "observed.idempotent_reservation", Outcome.PASS,
        "an identical replay returned the original reservation and took no stock; a conflicting one was refused",
    )


def current_generation(trial: TrialContext) -> str | None:
    """The live stock table's generation, as the contract defines it.

    Generation = the table's CreationDateTime in epoch milliseconds, read with
    DescribeTable. The snapshotter derives the same value from the raw JSON
    timestamp; a generation directory within one millisecond of the computed
    value is taken as the snapshotter's spelling of it (float rounding only).
    """
    table = trial.cloud.table(trial.stock_table)
    if table is None:
        return None
    created = table.get("CreationDateTime")
    if created is None:
        raise SubmissionFailure("DescribeTable returned no CreationDateTime for the stock table")
    stamp = created.timestamp() if hasattr(created, "timestamp") else float(created)
    computed = int(round(stamp * 1000))
    for key in trial.cloud.snapshot_keys(trial.bucket):
        parts = key.split("/")
        if len(parts) >= 3 and parts[1].isdigit() and abs(int(parts[1]) - computed) <= 1:
            return parts[1]
    return str(computed)


def wait_for_snapshot_containing(trial: TrialContext, generation: str, rows: set[tuple[str, str]],
                                 timeout: float) -> str | None:
    """Newest snapshot of this generation that holds every given row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        keys = sorted((k for k in trial.cloud.snapshot_keys(trial.bucket)
                       if k.startswith(f"snapshots/{generation}/")), reverse=True)
        # Only committed snapshots count: the supplied snapshotter writes the
        # data object first and its commit marker second.
        committed = [k for k in keys if trial.cloud.is_committed(trial.bucket, k)]
        if committed:
            content = {(r.get("sku"), r.get("warehouse_id")) for r in trial.cloud.read_snapshot(trial.bucket, committed[0])}
            if rows <= content:
                return committed[0]
        time.sleep(3)
    return None


@obligation("observed.snapshots_versioned")
def test_snapshots_versioned(trial: TrialContext) -> CheckResult:
    """Fresh writes reach a snapshot, and LATEST.json keeps its history."""
    warehouse, sku = fresh("wh"), fresh("sku")
    put_stock(trial, sku, warehouse, on_hand=5)
    generation = current_generation(trial)
    if not generation:
        raise SubmissionFailure("the stock table does not exist, so it has no generation")

    window = trial.config.snapshot_interval * 4 + 15
    key = wait_for_snapshot_containing(trial, generation, {(sku, warehouse)}, window)
    if key is None:
        raise SubmissionFailure(f"no snapshot of generation {generation} contained a fresh row within {window}s")

    import json
    latest = json.loads(trial.cloud.s3.get_object(Bucket=trial.bucket, Key="snapshots/LATEST.json")["Body"].read())
    if latest.get("generation") != generation:
        raise SubmissionFailure(f"LATEST.json points at generation {latest.get('generation')}, expected {generation}")
    versions = trial.cloud.object_versions(trial.bucket, "snapshots/LATEST.json")
    real_versions = [v for v in versions if v.get("VersionId") not in (None, "", "null")]
    if len(real_versions) < 2:
        raise SubmissionFailure(f"LATEST.json has {len(real_versions)} retained version(s); versioning is not in effect")

    return CheckResult(
        "observed.snapshots_versioned", Outcome.PASS,
        f"a fresh row reached {key} and LATEST.json retains {len(real_versions)} versions",
    )
