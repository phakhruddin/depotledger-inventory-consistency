"""Cloud API access for the verifier."""
from __future__ import annotations

import json
import time
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from .config import Config


class Cloud:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}

    def client(self, service: str):
        if service not in self._clients:
            extra = {}
            if service == "s3":
                extra["config"] = BotoConfig(s3={"addressing_style": "path"})
            self._clients[service] = boto3.client(
                service, region_name=self._config.region,
                endpoint_url=self._config.endpoint_url, **extra,
            )
        return self._clients[service]

    @property
    def ddb(self):
        return self.client("dynamodb")

    @property
    def s3(self):
        return self.client("s3")

    @property
    def elbv2(self):
        return self.client("elbv2")

    @property
    def ecs(self):
        return self.client("ecs")

    @property
    def ec2(self):
        return self.client("ec2")

    @property
    def logs(self):
        return self.client("logs")

    # -- helpers ------------------------------------------------------------
    def healthy_targets(self, target_group_arn: str) -> int:
        described = self.elbv2.describe_target_health(TargetGroupArn=target_group_arn)
        return sum(1 for entry in described["TargetHealthDescriptions"]
                   if entry["TargetHealth"]["State"] == "healthy")

    def table(self, name: str) -> dict[str, Any] | None:
        try:
            return self.ddb.describe_table(TableName=name)["Table"]
        except self.ddb.exceptions.ResourceNotFoundException:
            return None

    def wait_table_gone(self, name: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.table(name) is None:
                return
            time.sleep(2)

    def object_versions(self, bucket: str, key: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
        while True:
            page = self.s3.list_object_versions(**kwargs)
            found.extend(v for v in page.get("Versions", []) if v.get("Key") == key)
            if not page.get("IsTruncated"):
                return found
            kwargs["KeyMarker"] = page.get("NextKeyMarker")
            kwargs["VersionIdMarker"] = page.get("NextVersionIdMarker")

    def snapshot_keys(self, bucket: str) -> list[str]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix="snapshots/"):
            keys.extend(o["Key"] for o in page.get("Contents", [])
                        if o["Key"].endswith(".jsonl"))
        return keys

    def is_committed(self, bucket: str, key: str) -> bool:
        """A snapshot is committed when its marker exists and its sha256 matches."""
        import hashlib
        try:
            marker = json.loads(self.s3.get_object(
                Bucket=bucket, Key=key[: -len(".jsonl")] + ".committed")["Body"].read())
            body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception:  # noqa: BLE001 - absent marker or object
            return False
        return marker.get("sha256") == hashlib.sha256(body).hexdigest()

    def plant_overwritten_snapshot(self, bucket: str, generation: str, committed_rows: list[dict[str, Any]],
                                   overwritten_rows: list[dict[str, Any]]) -> tuple[str, str]:
        """Write a committed snapshot, then overwrite its data object.

        Follows the public commit protocol exactly (data object, then marker
        with the data object's sha256), then writes a second version of the
        data object with different content. Returns (key, committed version).
        """
        import hashlib
        taken = int(time.time() * 1000) + 60_000
        key = f"snapshots/{generation}/{taken}.jsonl"

        def body(rows):
            return "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode()

        committed = body(committed_rows)
        meta = {"item-count": str(len(committed_rows)), "table-generation": generation, "taken-at-ms": str(taken)}
        first = self.s3.put_object(Bucket=bucket, Key=key, Body=committed,
                                   ContentType="application/x-ndjson", Metadata=meta)
        version = first.get("VersionId")
        marker = {"key": key, "generation": generation, "item_count": len(committed_rows),
                  "taken_at_ms": taken, "sha256": hashlib.sha256(committed).hexdigest()}
        self.s3.put_object(Bucket=bucket, Key=key[: -len(".jsonl")] + ".committed",
                           Body=json.dumps(marker).encode(), ContentType="application/json")
        self.s3.put_object(Bucket=bucket, Key=key, Body=body(overwritten_rows),
                           ContentType="application/x-ndjson",
                           Metadata=dict(meta, **{"item-count": str(len(overwritten_rows))}))
        # Without versioning the committed bytes are simply gone: that is the
        # submission's defect (the contract requires versioning), not ours.
        from .errors import SubmissionFailure
        if not version or version == "null":
            raise SubmissionFailure("the snapshot bucket returned no version id; versioning is not in effect")
        stored = self.s3.get_object(Bucket=bucket, Key=key, VersionId=version)["Body"].read()
        if hashlib.sha256(stored).hexdigest() != marker["sha256"]:
            raise SubmissionFailure("the committed version was not retained after the overwrite")
        return key, version

    def read_snapshot(self, bucket: str, key: str) -> list[dict[str, Any]]:
        body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    # -- decoys and inventory ------------------------------------------------
    def create_decoys(self) -> dict[str, str]:
        """Pre-existing resources that share the prefix but are not owned."""
        table, bucket = self._config.legacy_table, self._config.legacy_bucket
        self.ddb.create_table(
            TableName=table, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "sku", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "sku", "KeyType": "HASH"}],
            Tags=[{"Key": "Owner", "Value": "legacy-platform"}],
        )
        self.ddb.put_item(TableName=table, Item={"sku": {"S": "LEGACY-1"}, "note": {"S": "do not delete"}})
        self.s3.create_bucket(Bucket=bucket)
        self.s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        for revision in range(2):
            self.s3.put_object(Bucket=bucket, Key="snapshots/LATEST.json",
                               Body=json.dumps({"legacy": revision}).encode())
        return {"table": table, "bucket": bucket}

    def decoys_intact(self) -> list[str]:
        problems = []
        table, bucket = self._config.legacy_table, self._config.legacy_bucket
        if self.table(table) is None:
            problems.append(f"table {table} was deleted")
        else:
            item = self.ddb.get_item(TableName=table, Key={"sku": {"S": "LEGACY-1"}}).get("Item")
            if not item:
                problems.append(f"table {table} lost its data")
        try:
            versions = self.object_versions(bucket, "snapshots/LATEST.json")
            if len(versions) < 2:
                problems.append(f"bucket {bucket} lost object versions")
        except Exception as exc:  # noqa: BLE001 - any failure means it is gone
            problems.append(f"bucket {bucket} is unreadable or deleted ({type(exc).__name__})")
        return problems

    def prefixed_inventory(self, prefix: str) -> dict[str, list[str]]:
        """Live resources carrying this deployment's prefix.

        Queried from the cloud rather than trusted from Terraform state.
        """
        found: dict[str, list[str]] = {}

        def record(kind: str, names) -> None:
            hits = sorted(str(n) for n in names if prefix in str(n))
            if hits:
                found[kind] = hits

        probes = {
            "tables": lambda: self.ddb.list_tables().get("TableNames", []),
            "buckets": lambda: [b["Name"] for b in self.s3.list_buckets().get("Buckets", [])],
            "load_balancers": lambda: [lb["LoadBalancerArn"] for lb in
                                       self.elbv2.describe_load_balancers()["LoadBalancers"]],
            "target_groups": lambda: [tg["TargetGroupArn"] for tg in
                                      self.elbv2.describe_target_groups()["TargetGroups"]],
            "clusters": lambda: self.ecs.list_clusters()["clusterArns"],
            "log_groups": lambda: [g["logGroupName"] for g in
                                   self.logs.describe_log_groups()["logGroups"]],
            "vpcs": lambda: [vpc["VpcId"] for vpc in self.ec2.describe_vpcs()["Vpcs"]
                             for tag in vpc.get("Tags", [])
                             if tag.get("Key") == "DepotLedgerDeployment" and prefix in tag.get("Value", "")],
        }
        for kind, probe in probes.items():
            try:
                record(kind, probe())
            except Exception:  # noqa: BLE001 - absent service means nothing to report
                pass
        # VPC ids carry no prefix; the tag probe returns bare ids.
        try:
            vpcs = [vpc["VpcId"] for vpc in self.ec2.describe_vpcs()["Vpcs"]
                    for tag in vpc.get("Tags", [])
                    if tag.get("Key") == "DepotLedgerDeployment" and tag.get("Value") == prefix]
            if vpcs:
                found["vpcs"] = sorted(vpcs)
        except Exception:  # noqa: BLE001
            pass
        return found
