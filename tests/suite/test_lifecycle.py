"""Lifecycle plane.

Redeploy, table loss, plan stability and clean destruction. Faults are limited
to what the public contract describes: rerunning deploy, deleting the stock
table, and destroying. Each check first proves the healthy path, so a failure
here is a lifecycle failure and not a pre-existing one.
"""
from __future__ import annotations

from .test_behavior import current_generation, fresh, put_stock, reserve, stock_row, wait_for_snapshot_containing
from .tools.deployment import deploy, destroy
import json
import time

from .tools.errors import CleanupLeak, HarnessError, SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.terraform import by_type, describe_changes, disruptive_changes, plan, state
from .tools.trial import TrialContext, obligation


def _identity(trial: TrialContext) -> dict[str, str]:
    """Identifiers that must not change when durable data is kept."""
    cloud = trial.cloud
    stock = cloud.table(trial.stock_table) or {}
    reservations = cloud.table(trial.reservations_table) or {}
    return {
        "stock": f"{stock.get('TableArn')}@{stock.get('CreationDateTime')}",
        "reservations": f"{reservations.get('TableArn')}@{reservations.get('CreationDateTime')}",
        "load_balancer": trial.manifest["edge"]["load_balancer_arn"],
        "bucket": trial.bucket,
    }


def _wait_generation(trial: TrialContext, rows: set[tuple[str, str]]) -> tuple[str, str]:
    generation = current_generation(trial)
    if not generation:
        raise SubmissionFailure("the API reports no current stock table generation")
    window = trial.config.snapshot_interval * 4 + 15
    key = wait_for_snapshot_containing(trial, generation, rows, window)
    if key is None:
        raise SubmissionFailure(f"no snapshot captured the seeded rows within {window}s")
    return generation, key


@obligation("lifecycle.redeploy_preserves_data")
def test_redeploy_preserves_data(trial: TrialContext) -> CheckResult:
    """A routine redeploy replaces nothing, resurrects nothing, loses nothing."""
    warehouse = fresh("wh")
    keep, doomed, later = fresh("keep"), fresh("doomed"), fresh("later")
    put_stock(trial, keep, warehouse, on_hand=11)
    put_stock(trial, doomed, warehouse, on_hand=12)
    generation, key = _wait_generation(trial, {(keep, warehouse), (doomed, warehouse)})
    before = _identity(trial)

    removed = trial.api.delete(f"/stock/{doomed}/{warehouse}")
    if removed.status != 200:
        raise SubmissionFailure(f"DELETE returned {removed.status}: {removed.text[:300]}")
    put_stock(trial, later, warehouse, on_hand=13)

    deploy(trial.config.submission_dir, trial.config.logs_dir, label="deploy-redeploy")
    trial.reload_manifest()

    after = _identity(trial)
    changed = [name for name in before if before[name] != after[name]]
    if changed:
        raise SubmissionFailure(f"a routine redeploy replaced: {changed}")
    if current_generation(trial) != generation:
        raise SubmissionFailure("a routine redeploy produced a new stock table generation")
    if stock_row(trial, doomed, warehouse) is not None:
        raise SubmissionFailure(f"a routine redeploy resurrected a row deleted after {key}")
    for sku, units in ((keep, 11), (later, 13)):
        row = stock_row(trial, sku, warehouse)
        if not row or row.get("on_hand") != units:
            raise SubmissionFailure(f"after a routine redeploy {sku} is {row}, expected on_hand {units}")

    return CheckResult(
        "lifecycle.redeploy_preserves_data", Outcome.PASS,
        "redeploy kept every durable identity, did not resurrect a deleted row and kept a newer one",
    )


@obligation("lifecycle.table_loss_restore")
def test_table_loss_restore(trial: TrialContext) -> CheckResult:
    """A deleted stock table comes back with the last snapshot's rows."""
    cloud = trial.cloud
    warehouse = fresh("wh")
    seeded = {fresh("sku"): n for n in (21, 22, 23)}
    for sku, units in seeded.items():
        put_stock(trial, sku, warehouse, on_hand=units, reorder_point=2)
    old_generation, natural_key = _wait_generation(trial, {(sku, warehouse) for sku in seeded})
    natural = cloud.read_snapshot(trial.bucket, natural_key)

    # The newest committed snapshot of the generation, whose data object was
    # then overwritten by a misbehaving writer. The commit marker still
    # describes the committed bytes, and the versioned bucket still holds
    # them as an older object version. Its committed content is the natural
    # snapshot plus one late row, so restoring the natural snapshot instead
    # (skipping the overwritten one) is detectably different from restoring
    # the committed content.
    late = fresh("late")
    committed_rows = sorted(natural + [{
        "sku": late, "warehouse_id": warehouse, "on_hand": 31, "reserved": 0, "available": 31,
        "reorder_point": 2, "version": 1, "updated_at": "2026-01-01T00:00:00Z"}],
        key=lambda r: (r.get("sku", ""), r.get("warehouse_id", "")))
    dropped = next(iter(seeded))
    poison = fresh("poison")
    overwritten_rows = [r for r in natural if r.get("sku") != dropped] + [{
        "sku": poison, "warehouse_id": warehouse, "on_hand": 99, "reserved": 0, "available": 99,
        "reorder_point": 0, "version": 1, "updated_at": "2026-01-01T00:00:00Z"}]
    key, committed_version = cloud.plant_overwritten_snapshot(
        trial.bucket, old_generation, committed_rows, overwritten_rows)
    expected = committed_rows
    before = _identity(trial)
    pointer_versions = len(cloud.object_versions(trial.bucket, "snapshots/LATEST.json"))

    # The fault.
    cloud.ddb.delete_table(TableName=trial.stock_table)
    cloud.wait_table_gone(trial.stock_table)

    deploy(trial.config.submission_dir, trial.config.logs_dir, label="deploy-restore")
    trial.reload_manifest()

    new_generation = current_generation(trial)
    if not new_generation or new_generation == old_generation:
        raise SubmissionFailure("the stock table was not recreated as a new generation")
    trial.facts["first_loss_old_generation"] = old_generation

    if stock_row(trial, poison, warehouse) is not None:
        raise SubmissionFailure(
            f"a row that exists only in the overwritten current version of {key} was restored; "
            f"the committed content is version {committed_version}")
    missing = []
    for row in expected:
        served = stock_row(trial, row["sku"], row["warehouse_id"])
        if not served or served.get("on_hand") != row.get("on_hand") or served.get("reserved") != row.get("reserved"):
            missing.append(f"{row['sku']}/{row['warehouse_id']}")
    if missing:
        raise SubmissionFailure(
            f"when deploy returned, {len(missing)} of {len(expected)} rows of the committed content of {key} "
            f"(version {committed_version}) were not served: {missing[:6]}")

    after = _identity(trial)
    for name in ("reservations", "load_balancer", "bucket"):
        if before[name] != after[name]:
            raise SubmissionFailure(f"recovering the stock table replaced the {name}")

    replay = trial.facts.get("replay_order")
    if replay:
        response = reserve(trial, replay["order_id"], replay["sku"], replay["warehouse"], 2)
        if response.status != 200 or response.json().get("reservation_id") != replay["reservation_id"]:
            raise SubmissionFailure(
                f"after recovery an earlier order no longer replays: {response.status} {response.text[:200]}")

    try:
        cloud.s3.head_object(Bucket=trial.bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        raise SubmissionFailure(f"the snapshot {key} is gone from the bucket") from exc
    if len(cloud.object_versions(trial.bucket, "snapshots/LATEST.json")) < pointer_versions:
        raise SubmissionFailure("recovery lost earlier versions of snapshots/LATEST.json")

    return CheckResult(
        "lifecycle.table_loss_restore", Outcome.PASS,
        f"after the stock table was deleted, deploy restored all {len(expected)} committed rows of {key} "
        f"(version {committed_version}) despite the overwritten current version",
        details={"old_generation": old_generation, "new_generation": new_generation,
                 "committed_version": committed_version},
    )


def _lifecycle_rule_days(cloud, bucket: str) -> int | None:
    """Noncurrent expiry of an enabled rule covering snapshots/, or None."""
    try:
        rules = cloud.s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except Exception:  # noqa: BLE001 - no configuration at all
        return None
    for rule in rules:
        if rule.get("Status") != "Enabled":
            continue
        flt = rule.get("Filter") or {}
        prefix = flt.get("Prefix", (flt.get("And") or {}).get("Prefix", rule.get("Prefix", ""))) or ""
        if prefix and not "snapshots/".startswith(prefix):
            continue
        days = (rule.get("NoncurrentVersionExpiration") or {}).get("NoncurrentDays")
        if days is not None:
            return int(days)
    return None


def _bucket_created(cloud, bucket: str):
    for entry in cloud.s3.list_buckets().get("Buckets", []):
        if entry["Name"] == bucket:
            return entry.get("CreationDate")
    return None


@obligation("lifecycle.bucket_drift_repair")
def test_bucket_drift_repair(trial: TrialContext) -> CheckResult:
    """Suspended versioning and a deleted lifecycle rule are repaired in place."""
    cloud, bucket = trial.cloud, trial.bucket
    retention = trial.config.retention_days

    # Healthy first.
    if cloud.s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise SubmissionFailure("bucket versioning was not Enabled before the fault")
    if _lifecycle_rule_days(cloud, bucket) != retention:
        raise SubmissionFailure("the noncurrent-version lifecycle rule was not in place before the fault")
    history = [v["VersionId"] for v in cloud.object_versions(bucket, "snapshots/LATEST.json")
               if v.get("VersionId") not in (None, "", "null")]
    if len(history) < 2:
        raise SubmissionFailure("LATEST.json had no version history before the fault")
    created = _bucket_created(cloud, bucket)

    # The fault: drift on the durable controls, outside Terraform.
    cloud.s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Suspended"})
    cloud.s3.delete_bucket_lifecycle(Bucket=bucket)
    if cloud.s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Suspended" \
            or _lifecycle_rule_days(cloud, bucket) is not None:
        raise HarnessError("the endpoint did not apply the drift fault")

    deploy(trial.config.submission_dir, trial.config.logs_dir, label="deploy-drift")
    trial.reload_manifest()

    problems = []
    if trial.bucket != bucket:
        problems.append(f"the manifest now names bucket {trial.bucket}, not {bucket}")
    if _bucket_created(cloud, bucket) != created:
        problems.append("the snapshot bucket was deleted and recreated")
    status = cloud.s3.get_bucket_versioning(Bucket=bucket).get("Status")
    if status != "Enabled":
        problems.append(f"versioning is {status} after deploy, expected Enabled")
    days = _lifecycle_rule_days(cloud, bucket)
    if days != retention:
        problems.append(f"noncurrent expiry is {days} after deploy, expected {retention} days")
    lost = []
    for version_id in history:
        try:
            cloud.s3.head_object(Bucket=bucket, Key="snapshots/LATEST.json", VersionId=version_id)
        except Exception:  # noqa: BLE001
            lost.append(version_id)
    if lost:
        problems.append(f"{len(lost)} of {len(history)} earlier LATEST.json versions were lost")
    if problems:
        raise SubmissionFailure("; ".join(problems))

    # Versioning must be live again, not just reported: new pointer versions accrue.
    window = trial.config.snapshot_interval * 3 + 15
    deadline = time.monotonic() + window
    fresh_versions = []
    while time.monotonic() < deadline:
        current = [v["VersionId"] for v in cloud.object_versions(bucket, "snapshots/LATEST.json")
                   if v.get("VersionId") not in (None, "", "null")]
        fresh_versions = [v for v in current if v not in history]
        if len(fresh_versions) >= 2:
            break
        time.sleep(3)
    if len(fresh_versions) < 2:
        raise SubmissionFailure(f"LATEST.json gained {len(fresh_versions)} new version(s) in {window}s after repair")

    return CheckResult(
        "lifecycle.bucket_drift_repair", Outcome.PASS,
        f"deploy re-enabled versioning and restored the {retention}-day rule on the same bucket, keeping "
        f"all {len(history)} earlier pointer versions",
    )


@obligation("lifecycle.second_loss_committed")
def test_second_loss_committed(trial: TrialContext) -> CheckResult:
    """A second loss restores the lost generation's newest COMMITTED snapshot."""
    cloud, bucket = trial.cloud, trial.bucket
    warehouse = fresh("wh")
    kept, changed, doomed, added = fresh("kept"), fresh("changed"), fresh("doomed"), fresh("added")
    put_stock(trial, kept, warehouse, on_hand=41)
    put_stock(trial, changed, warehouse, on_hand=42)
    put_stock(trial, doomed, warehouse, on_hand=43)
    live_generation, _ = _wait_generation(trial, {(kept, warehouse), (changed, warehouse), (doomed, warehouse)})

    # Writes between the losses: the live generation diverges from its past.
    if trial.api.delete(f"/stock/{doomed}/{warehouse}").status != 200:
        raise SubmissionFailure("could not delete a row through the API")
    put_stock(trial, changed, warehouse, on_hand=7)
    put_stock(trial, added, warehouse, on_hand=44)

    deadline = time.monotonic() + trial.config.snapshot_interval * 4 + 15
    key = None
    while time.monotonic() < deadline:
        keys = sorted((k for k in cloud.snapshot_keys(bucket) if k.startswith(f"snapshots/{live_generation}/")),
                      reverse=True)
        for candidate in keys:
            if not cloud.is_committed(bucket, candidate):
                continue
            content = {(r["sku"], r["warehouse_id"]): r for r in cloud.read_snapshot(bucket, candidate)}
            if (added, warehouse) in content and (doomed, warehouse) not in content \
                    and content.get((changed, warehouse), {}).get("on_hand") == 7:
                key = candidate
            break
        if key:
            break
        time.sleep(3)
    if key is None:
        raise SubmissionFailure("no committed snapshot of the live generation captured the writes between losses")
    expected = cloud.read_snapshot(bucket, key)

    # A snapshotter crash between writing the data object and its commit
    # marker: the newest object of the live generation, with stale and
    # foreign rows, and no marker. The contract says it is not restorable.
    poison = fresh("poison")
    stale = [
        {"sku": s_, "warehouse_id": warehouse, "on_hand": n, "reserved": 0, "available": n,
         "reorder_point": 0, "version": 1, "updated_at": "2026-01-01T00:00:00Z"}
        for s_, n in ((doomed, 43), (changed, 42), (poison, 99))
    ]
    torn_ms = int(time.time() * 1000) + 60_000
    torn_key = f"snapshots/{live_generation}/{torn_ms}.jsonl"
    cloud.s3.put_object(
        Bucket=bucket, Key=torn_key, ContentType="application/x-ndjson",
        Body="".join(json.dumps(r, sort_keys=True) + "\n" for r in stale).encode(),
        Metadata={"item-count": str(len(stale)), "table-generation": live_generation, "taken-at-ms": str(torn_ms)},
    )
    if cloud.is_committed(bucket, torn_key):
        raise HarnessError("the uncommitted snapshot unexpectedly has a valid marker")
    before = _identity(trial)

    cloud.ddb.delete_table(TableName=trial.stock_table)
    cloud.wait_table_gone(trial.stock_table)
    deploy(trial.config.submission_dir, trial.config.logs_dir, label="deploy-second-loss")
    trial.reload_manifest()

    new_generation = current_generation(trial)
    if not new_generation or new_generation == live_generation:
        raise SubmissionFailure("the stock table was not recreated as a new generation")
    problems = []
    if stock_row(trial, poison, warehouse) is not None:
        problems.append(f"a row that only exists in the uncommitted {torn_key} was restored")
    if stock_row(trial, doomed, warehouse) is not None:
        problems.append("a row deleted before the second loss was resurrected")
    row = stock_row(trial, changed, warehouse)
    if not row or row.get("on_hand") != 7:
        problems.append(f"{changed} came back as {row}, expected its pre-loss value on_hand 7")
    missing = [f"{r['sku']}/{r['warehouse_id']}" for r in expected
               if (stock_row(trial, r["sku"], r["warehouse_id"]) or {}).get("on_hand") != r.get("on_hand")]
    if missing:
        problems.append(f"{len(missing)} of {len(expected)} rows from {key} were not served: {missing[:6]}")
    after = _identity(trial)
    for name in ("reservations", "load_balancer", "bucket"):
        if before[name] != after[name]:
            problems.append(f"recovering the stock table replaced the {name}")
    if problems:
        raise SubmissionFailure("; ".join(problems))

    return CheckResult(
        "lifecycle.second_loss_committed", Outcome.PASS,
        f"after a second loss deploy restored {key}, the newest committed snapshot of generation "
        f"{live_generation}, and ignored the uncommitted {torn_key}",
        details={"restored_generation": live_generation, "new_generation": new_generation, "torn_key": torn_key},
    )


@obligation("lifecycle.reapply_stable")
def test_reapply_is_stable(trial: TrialContext) -> CheckResult:
    """A plan run directly against infra/ shows nothing to create or delete."""
    plan_json = plan(trial.config.infra_dir, artifact=trial.config.logs_dir / "standalone-plan.json")
    changes = disruptive_changes(plan_json)
    if changes:
        raise SubmissionFailure(
            f"a standalone plan wants to create or delete {len(changes)} resource(s): "
            + describe_changes(changes[:8]))
    return CheckResult(
        "lifecycle.reapply_stable", Outcome.PASS,
        "a standalone plan resolved every variable and shows nothing to create or delete",
        details={"resource_changes": len(plan_json.get("resource_changes", []))},
    )


@obligation("lifecycle.destroy_clean")
def test_destroy_is_clean(trial: TrialContext) -> CheckResult:
    """Destroy removes what this deployment owns and nothing else."""
    config = trial.config

    # This endpoint's ECS also writes every task's output to a log group it
    # creates itself, `/ecs/<task definition family>`, whatever awslogs-group
    # the task definition names. Those groups are the emulator's, not the
    # submission's, and they can reappear while tasks drain. They are only
    # excused when the submission did not declare that name itself; a
    # declared log group that survives destroy is still a leak.
    try:
        state_json = state(config.infra_dir)
    except Exception:  # noqa: BLE001 - no readable state means nothing to excuse
        state_json = {}
    declared_groups = {r["values"].get("name") for r in by_type(state_json, "aws_cloudwatch_log_group")}
    families = {r["values"].get("family") for r in by_type(state_json, "aws_ecs_task_definition")}
    emulator_groups = {f"/ecs/{family}" for family in families if family} - declared_groups

    destroy(config.submission_dir, config.logs_dir)

    damaged = trial.cloud.decoys_intact()
    if damaged:
        raise CleanupLeak(f"destroy modified pre-existing resources: {damaged}")

    remaining = trial.cloud.prefixed_inventory(config.prefix)
    excused = sorted(set(remaining.get("log_groups", [])) & emulator_groups)
    if excused:
        remaining["log_groups"] = sorted(set(remaining["log_groups"]) - emulator_groups)
        if not remaining["log_groups"]:
            remaining.pop("log_groups")
    leaked = {kind: sorted(set(items) - set(trial.baseline.get(kind, []))) for kind, items in remaining.items()}
    leaked = {kind: items for kind, items in leaked.items() if items}
    if leaked:
        raise CleanupLeak(f"resources carrying this deployment's prefix remain: {leaked}")

    destroyed = {kind: sorted(set(items) - set(remaining.get(kind, []))) for kind, items in trial.baseline.items()}
    destroyed = {k: v for k, v in destroyed.items() if v}
    if destroyed:
        raise CleanupLeak(f"destroy removed resources it did not own: {destroyed}")

    return CheckResult(
        "lifecycle.destroy_clean", Outcome.PASS,
        "no resource carrying this deployment's prefix remains and the legacy resources are intact",
        details={"baseline_kinds": sorted(trial.baseline), "emulator_log_groups_excused": excused},
    )
