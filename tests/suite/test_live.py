"""Realized plane.

Live cloud APIs, not state. Proves the declared data plane exists and is
healthy as the endpoint reports it: tables and indexes ACTIVE with the
contracted keys, TTL and backups on, the bucket versioned, and the services
running at their contracted counts.
"""
from __future__ import annotations

import time

from .tools.errors import SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.trial import TrialContext, obligation


def _key_schema(entries: list[dict]) -> tuple:
    by_type = {e["KeyType"]: e["AttributeName"] for e in entries}
    return by_type.get("HASH"), by_type.get("RANGE")


@obligation("realized.data_plane")
def test_data_plane_is_live(trial: TrialContext) -> CheckResult:
    """Tables, indexes, TTL, PITR, bucket versioning and services, live."""
    cloud = trial.cloud
    problems: list[str] = []

    stock = cloud.table(trial.stock_table)
    reservations = cloud.table(trial.reservations_table)
    if stock is None or reservations is None:
        raise SubmissionFailure("a table named in the manifest does not exist")
    for table in (stock, reservations):
        if table.get("TableStatus") != "ACTIVE":
            problems.append(f"{table['TableName']} is {table.get('TableStatus')}")
    if stock.get("TableArn") != trial.manifest["data"]["stock_table"]["arn"]:
        problems.append("the stock table ARN does not match the manifest")
    if _key_schema(stock["KeySchema"]) != ("sku", "warehouse_id"):
        problems.append(f"live stock key schema is {_key_schema(stock['KeySchema'])}")
    if _key_schema(reservations["KeySchema"]) != ("order_id", None):
        problems.append(f"live reservations key schema is {_key_schema(reservations['KeySchema'])}")

    live_indexes = {i["IndexName"]: i for i in stock.get("GlobalSecondaryIndexes", [])}
    expected = {"by_warehouse": ("warehouse_id", "sku"), "low_stock": ("low_stock_warehouse", "sku")}
    for name, keys in expected.items():
        index = live_indexes.get(name)
        if index is None:
            problems.append(f"index {name} does not exist")
            continue
        if _key_schema(index["KeySchema"]) != keys:
            problems.append(f"index {name} keys are {_key_schema(index['KeySchema'])}")
        if index.get("IndexStatus", "ACTIVE") != "ACTIVE":
            problems.append(f"index {name} is {index.get('IndexStatus')}")

    ttl = cloud.ddb.describe_time_to_live(TableName=trial.reservations_table)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING") or ttl.get("AttributeName") != "expires_at":
        problems.append(f"reservations TTL reports {ttl}")

    backups = cloud.ddb.describe_continuous_backups(TableName=trial.stock_table)["ContinuousBackupsDescription"]
    pitr = backups.get("PointInTimeRecoveryDescription", {}).get("PointInTimeRecoveryStatus")
    if pitr != "ENABLED":
        problems.append(f"stock point-in-time recovery reports {pitr}")

    versioning = cloud.s3.get_bucket_versioning(Bucket=trial.bucket).get("Status")
    if versioning != "Enabled":
        problems.append(f"bucket versioning reports {versioning}")

    # Capacity. Allow a short settle window after deploy returned.
    compute = trial.manifest["compute"]
    wanted = trial.config.api_desired_count
    healthy = 0
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        healthy = cloud.healthy_targets(trial.manifest["edge"]["target_group_arn"])
        if healthy >= wanted:
            break
        time.sleep(5)
    if healthy < wanted:
        problems.append(f"the API target group has {healthy} healthy target(s), expected {wanted}")

    running: dict[str, int] = {}
    for kind in ("api", "snapshotter"):
        described = cloud.ecs.describe_services(cluster=compute["cluster_arn"],
                                                services=[compute["services"][kind]])["services"]
        if not described:
            problems.append(f"service {kind} does not exist")
            continue
        running[kind] = int(described[0].get("runningCount", 0))
        awsvpc = described[0].get("networkConfiguration", {}).get("awsvpcConfiguration", {})
        if awsvpc.get("assignPublicIp") == "ENABLED":
            problems.append(f"service {kind} runs tasks with a public IP")
    if running.get("snapshotter") != 1:
        problems.append(f"{running.get('snapshotter')} snapshotter tasks run, expected exactly 1")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "realized.data_plane", Outcome.PASS,
        "tables, indexes, TTL, PITR and bucket versioning are live; services run at their contracted counts",
        details={"healthy_targets": healthy, "running": running},
    )
