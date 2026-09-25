"""Declaration plane.

These read Terraform or OpenTofu state. They prove the submission *declared*
the required data model, bucket configuration, topology and identities. Where
the endpoint records a setting without enforcing it (IAM, security groups,
bucket encryption and lifecycle), that is all these checks claim.
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any

from .tools.errors import SubmissionFailure
from .tools.results import CheckResult, Outcome
from .tools.terraform import by_type, state
from .tools.trial import TrialContext, obligation

REQUIRED_TYPES = {
    "aws_vpc": 1,
    "aws_subnet": 4,
    "aws_lb": 1,
    "aws_lb_listener": 1,
    "aws_lb_target_group": 1,
    "aws_ecs_cluster": 1,
    "aws_ecs_service": 2,
    "aws_ecs_task_definition": 2,
    "aws_iam_role": 3,
    "aws_dynamodb_table": 2,
    "aws_s3_bucket": 1,
    "aws_s3_bucket_versioning": 1,
    "aws_cloudwatch_log_group": 2,
}

REQUIRED_PROJECTION = {"on_hand", "reserved", "available", "reorder_point", "updated_at"}


def _state(trial: TrialContext) -> dict[str, Any]:
    if "state" not in trial.facts:
        trial.facts["state"] = state(trial.config.infra_dir)
    return trial.facts["state"]


def _one(values: Any) -> dict[str, Any]:
    """Nested blocks come back as a list of one object."""
    if isinstance(values, list):
        return values[0] if values else {}
    return values or {}


def _table(state_json: dict[str, Any], name: str) -> dict[str, Any]:
    for table in by_type(state_json, "aws_dynamodb_table"):
        if table["values"].get("name") == name:
            return table["values"]
    raise SubmissionFailure(f"table {name} named in the manifest is not managed in state")


def _keys(block: dict[str, Any]) -> tuple[str | None, str | None]:
    """Hash and range key of a table or index, whichever schema form was used."""
    hash_key, range_key = block.get("hash_key") or None, block.get("range_key") or None
    for entry in block.get("key_schema") or []:
        kind = (entry.get("key_type") or "").upper()
        if kind == "HASH" and not hash_key:
            hash_key = entry.get("attribute_name")
        if kind == "RANGE" and not range_key:
            range_key = entry.get("attribute_name")
    return hash_key, range_key


@obligation("declared.managed_iac")
def test_infrastructure_is_managed(trial: TrialContext) -> CheckResult:
    """Every scored resource family is declared in Terraform or OpenTofu."""
    state_json = _state(trial)
    missing = []
    for resource_type, minimum in REQUIRED_TYPES.items():
        found = len(by_type(state_json, resource_type))
        if found < minimum:
            missing.append(f"{resource_type}: expected at least {minimum}, found {found}")
    if missing:
        raise SubmissionFailure("required resources are not managed in state: " + "; ".join(missing))

    data = trial.manifest["data"]
    _table(state_json, data["stock_table"]["name"])
    _table(state_json, data["reservations_table"]["name"])
    buckets = {b["values"].get("bucket") for b in by_type(state_json, "aws_s3_bucket")}
    if data["snapshot_bucket"]["name"] not in buckets:
        raise SubmissionFailure("the snapshot bucket named in the manifest is not managed in state")
    lbs = {lb["values"].get("arn") for lb in by_type(state_json, "aws_lb")}
    if trial.manifest["edge"]["load_balancer_arn"] not in lbs:
        raise SubmissionFailure("the load balancer named in the manifest is not managed in state")

    return CheckResult(
        "declared.managed_iac", Outcome.PASS,
        "every scored resource family is managed and the manifest resolves to state",
        details={"types": {t: len(by_type(state_json, t)) for t in REQUIRED_TYPES}},
    )


@obligation("declared.table_design")
def test_table_design(trial: TrialContext) -> CheckResult:
    """Keys, types, indexes, projections, billing, PITR and TTL as contracted."""
    state_json = _state(trial)
    stock = _table(state_json, trial.stock_table)
    reservations = _table(state_json, trial.reservations_table)
    problems: list[str] = []

    def attribute_types(table: dict[str, Any]) -> dict[str, str]:
        return {a.get("name"): a.get("type") for a in table.get("attribute") or []}

    # Stock table keys.
    if _keys(stock) != ("sku", "warehouse_id"):
        problems.append(f"stock key schema is {_keys(stock)}, expected ('sku', 'warehouse_id')")
    types = attribute_types(stock)
    for name in ("sku", "warehouse_id", "low_stock_warehouse"):
        if types.get(name) != "S":
            problems.append(f"stock attribute {name} is declared as {types.get(name)!r}, expected 'S'")
    if stock.get("billing_mode") != "PAY_PER_REQUEST":
        problems.append(f"stock billing mode is {stock.get('billing_mode')!r}")
    if not _one(stock.get("point_in_time_recovery")).get("enabled"):
        problems.append("point-in-time recovery is not enabled on the stock table")

    # Indexes.
    expected = {"by_warehouse": ("warehouse_id", "sku"), "low_stock": ("low_stock_warehouse", "sku")}
    indexes = {i.get("name"): i for i in stock.get("global_secondary_index") or []}
    for name, keys in expected.items():
        index = indexes.get(name)
        if index is None:
            problems.append(f"index {name} is not declared")
            continue
        if _keys(index) != keys:
            problems.append(f"index {name} keys are {_keys(index)}, expected {keys}")
        projection = (index.get("projection_type") or "").upper()
        if projection == "ALL":
            continue
        if projection != "INCLUDE":
            problems.append(f"index {name} projects {projection or 'nothing'}; it needs the stock attributes")
            continue
        absent = REQUIRED_PROJECTION - set(index.get("non_key_attributes") or [])
        if absent:
            problems.append(f"index {name} does not project {sorted(absent)}")

    # Reservations table.
    if _keys(reservations) != ("order_id", None):
        problems.append(f"reservations key schema is {_keys(reservations)}, expected ('order_id', None)")
    if attribute_types(reservations).get("order_id") != "S":
        problems.append("reservations attribute order_id is not declared as 'S'")
    if reservations.get("billing_mode") != "PAY_PER_REQUEST":
        problems.append(f"reservations billing mode is {reservations.get('billing_mode')!r}")
    ttl = _one(reservations.get("ttl"))
    if not ttl.get("enabled") or ttl.get("attribute_name") != "expires_at":
        problems.append(f"reservations TTL is {ttl or 'absent'}, expected enabled on expires_at")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "declared.table_design", Outcome.PASS,
        "both tables and both indexes are declared exactly as the data model requires",
        details={"indexes": sorted(indexes)},
    )


@obligation("declared.snapshot_bucket")
def test_snapshot_bucket(trial: TrialContext) -> CheckResult:
    """Versioning, public access block, encryption and noncurrent expiry."""
    state_json = _state(trial)
    bucket = trial.bucket
    problems: list[str] = []

    def for_bucket(resource_type: str) -> list[dict[str, Any]]:
        return [r["values"] for r in by_type(state_json, resource_type)
                if r["values"].get("bucket") == bucket]

    versioning = for_bucket("aws_s3_bucket_versioning")
    if not any(_one(v.get("versioning_configuration")).get("status") == "Enabled" for v in versioning):
        problems.append("versioning is not declared Enabled")

    blocks = for_bucket("aws_s3_bucket_public_access_block")
    flags = ("block_public_acls", "ignore_public_acls", "block_public_policy", "restrict_public_buckets")
    if not any(all(b.get(f) is True for f in flags) for b in blocks):
        problems.append("the public access block does not set all four flags")

    encryption = for_bucket("aws_s3_bucket_server_side_encryption_configuration")
    algorithms = {
        _one(rule.get("apply_server_side_encryption_by_default")).get("sse_algorithm")
        for config in encryption for rule in config.get("rule") or []
    }
    if not algorithms & {"AES256", "aws:kms", "aws:kms:dsse"}:
        problems.append("default encryption is not configured")

    retention = trial.config.retention_days
    matched = False
    for config in for_bucket("aws_s3_bucket_lifecycle_configuration"):
        for rule in config.get("rule") or []:
            if rule.get("status") != "Enabled":
                continue
            prefix = ""
            flt = _one(rule.get("filter"))
            if flt:
                prefix = flt.get("prefix") or _one(flt.get("and")).get("prefix") or ""
            elif rule.get("prefix"):
                prefix = rule["prefix"]
            if prefix and not "snapshots/".startswith(prefix):
                continue
            expiry = _one(rule.get("noncurrent_version_expiration"))
            if int(expiry.get("noncurrent_days") or 0) == retention:
                matched = True
    if not matched:
        problems.append(f"no enabled lifecycle rule covering snapshots/ expires noncurrent versions after {retention} days")

    if problems:
        raise SubmissionFailure("; ".join(problems))
    return CheckResult(
        "declared.snapshot_bucket", Outcome.PASS,
        f"bucket declares versioning, a full public access block, default encryption and {retention}-day noncurrent expiry",
        details={"declaration_only": ["encryption", "lifecycle"]},
    )


def _statements(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            return []
    if not isinstance(document, dict):
        return []
    statements = document.get("Statement", [])
    return statements if isinstance(statements, list) else [statements]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def _role_statements(state_json: dict[str, Any], role_arn: str) -> list[dict[str, Any]]:
    """Every Allow statement attached to a role, however it was attached."""
    roles = {r["values"].get("arn"): r["values"] for r in by_type(state_json, "aws_iam_role")}
    role = roles.get(role_arn)
    if role is None:
        raise SubmissionFailure(f"task role {role_arn} is not managed in state")
    names = {role.get("name"), role.get("id"), role.get("arn")}
    found: list[dict[str, Any]] = []
    for inline in role.get("inline_policy") or []:
        found += _statements(inline.get("policy"))
    for policy in by_type(state_json, "aws_iam_role_policy"):
        if policy["values"].get("role") in names:
            found += _statements(policy["values"].get("policy"))
    managed = {p["values"].get("arn"): p["values"].get("policy") for p in by_type(state_json, "aws_iam_policy")}
    for attachment in by_type(state_json, "aws_iam_role_policy_attachment"):
        if attachment["values"].get("role") in names:
            arn = attachment["values"].get("policy_arn")
            if arn in managed:
                found += _statements(managed[arn])
            else:
                # An AWS managed policy cannot be inspected here; treat it as broad.
                found.append({"Effect": "Allow", "Action": "*", "Resource": "*", "Sid": arn})
    for attachment in by_type(state_json, "aws_iam_role_policy_attachments_exclusive"):
        if attachment["values"].get("role_name") in names:
            for arn in attachment["values"].get("policy_arns") or []:
                found += _statements(managed.get(arn)) if arn in managed else [
                    {"Effect": "Allow", "Action": "*", "Resource": "*", "Sid": arn}]
    return [s for s in found if s.get("Effect", "Allow") == "Allow"]


def _grants(statements: list[dict[str, Any]], action: str) -> bool:
    return any(fnmatch.fnmatchcase(action.lower(), pattern.lower())
               for s in statements for pattern in _as_list(s.get("Action")))


@obligation("declared.network_and_iam")
def test_network_and_iam(trial: TrialContext) -> CheckResult:
    """Private tasks, separate task roles, least privilege as declared."""
    state_json = _state(trial)
    private = set(trial.manifest["network"]["private_subnet_ids"])
    problems: list[str] = []

    for service in by_type(state_json, "aws_ecs_service"):
        for config in service["values"].get("network_configuration") or []:
            if config.get("assign_public_ip"):
                problems.append(f"{service['address']} assigns a public IP")
            if set(config.get("subnets") or []) - private:
                problems.append(f"{service['address']} runs outside the private subnets")

    # Which role each service actually runs with comes from the live task
    # definitions; the policies attached to those roles come from state.
    task_roles: dict[str, str] = {}
    compute = trial.manifest["compute"]
    for kind in ("api", "snapshotter"):
        described = trial.cloud.ecs.describe_services(
            cluster=compute["cluster_arn"], services=[compute["services"][kind]])["services"]
        if not described:
            problems.append(f"the {kind} service named in the manifest does not exist")
            continue
        definition = trial.cloud.ecs.describe_task_definition(
            taskDefinition=described[0]["taskDefinition"])["taskDefinition"]
        task_roles[kind] = definition.get("taskRoleArn") or ""
    for kind in ("api", "snapshotter"):
        if not task_roles.get(kind):
            problems.append(f"the {kind} task definition declares no task role")
    if problems:
        raise SubmissionFailure("; ".join(problems))
    if task_roles["api"] == task_roles["snapshotter"]:
        raise SubmissionFailure("the API and snapshotter share one task role")

    api = _role_statements(state_json, task_roles["api"])
    snap = _role_statements(state_json, task_roles["snapshotter"])
    for label, statements in (("api", api), ("snapshotter", snap)):
        for statement in statements:
            actions = _as_list(statement.get("Action"))
            if "*" in actions:
                problems.append(f"the {label} role is granted Action *")
            for action in actions:
                if action.endswith(":*") and action.count(":") == 1:
                    problems.append(f"the {label} role is granted the service-wide wildcard {action}")
    for forbidden in ("s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"):
        if _grants(api, forbidden):
            problems.append(f"the api role may {forbidden}")
    for forbidden in ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                      "dynamodb:BatchWriteItem", "dynamodb:DeleteTable"):
        if _grants(snap, forbidden):
            problems.append(f"the snapshotter role may {forbidden}")

    for policy in by_type(state_json, "aws_iam_role_policy") + by_type(state_json, "aws_iam_policy"):
        for statement in _statements(policy["values"].get("policy")):
            if "*" in _as_list(statement.get("Action")) and "*" in _as_list(statement.get("Resource")):
                problems.append(f"{policy['address']} grants Action * on Resource *")

    if problems:
        raise SubmissionFailure("; ".join(sorted(set(problems))))
    return CheckResult(
        "declared.network_and_iam", Outcome.PASS,
        "tasks are private and the two task roles are separate and least-privilege as declared",
        details={"declaration_only": True},
    )
