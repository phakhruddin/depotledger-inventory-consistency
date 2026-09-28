"""Lifecycle plane.

Redeploy, table loss, plan stability and clean destruction. Faults are limited
to what the public contract describes: rerunning deploy, deleting the stock
table, and destroying. Each check first proves the healthy path, so a failure
here is a lifecycle failure and not a pre-existing one.
"""
from __future__ import annotations

from .test_behavior import current_generation, fresh, put_stock, reserve, stock_row, wait_for_snapshot_containing
from .tools.deployment import deploy, destroy
from .tools.errors import CleanupLeak, SubmissionFailure
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
    old_generation, key = _wait_generation(trial, {(sku, warehouse) for sku in seeded})
    expected = cloud.read_snapshot(trial.bucket, key)
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

    missing = []
    for row in expected:
        served = stock_row(trial, row["sku"], row["warehouse_id"])
        if not served or served.get("on_hand") != row.get("on_hand") or served.get("reserved") != row.get("reserved"):
            missing.append(f"{row['sku']}/{row['warehouse_id']}")
    if missing:
        raise SubmissionFailure(
            f"when deploy returned, {len(missing)} of {len(expected)} rows from {key} were not served: {missing[:6]}")

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
        f"after the stock table was deleted, deploy restored all {len(expected)} rows of {key}",
        details={"old_generation": old_generation, "new_generation": new_generation},
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
