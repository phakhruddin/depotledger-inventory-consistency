"""Verifier configuration, read fresh from the environment for every run."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    submission_dir: Path
    logs_dir: Path
    evidence_dir: Path
    config_path: Path
    source_submission: Path
    region: str
    endpoint_url: str
    values: dict[str, Any]

    @property
    def manifest_path(self) -> Path:
        return self.submission_dir / "manifest.json"

    @property
    def infra_dir(self) -> Path:
        return self.submission_dir / "infra"

    @property
    def prefix(self) -> str:
        return str(self.values["resource_prefix"])

    @property
    def admin_token(self) -> str:
        return str(self.values["admin_token"])

    @property
    def api_desired_count(self) -> int:
        return int(self.values["api_desired_count"])

    @property
    def snapshot_interval(self) -> int:
        return int(self.values["snapshot_interval_seconds"])

    @property
    def idempotency_ttl(self) -> int:
        return int(self.values["idempotency_ttl_seconds"])

    @property
    def retention_days(self) -> int:
        return int(self.values["snapshot_noncurrent_retention_days"])

    @property
    def legacy_table(self) -> str:
        return f"{self.prefix}-legacy-stock"

    @property
    def legacy_bucket(self) -> str:
        return f"{self.prefix}-legacy-snapshots"


def _writable_copy(source: Path, work: Path) -> Path:
    """Harbor injects the submission read-only.

    Terraform must write config.auto.tfvars.json, .terraform/ and state beside
    the submitted configuration, so the verifier exercises an exact copy it
    owns. Copying the whole tree preserves every relative path inside the
    submission, so scripts that resolve paths from their own location still
    work unchanged.
    """
    if not source.is_dir():
        return source
    shutil.copytree(source, work, dirs_exist_ok=True)
    work.chmod(work.stat().st_mode | 0o700)
    for path in work.rglob("*"):
        try:
            path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            pass
    return work


def from_environment() -> Config:
    source_submission = Path(os.getenv("DEPOTLEDGER_SUBMISSION_DIR", "/workspace/submission"))
    submission = _writable_copy(
        source_submission,
        Path(os.getenv("DEPOTLEDGER_WORK_DIR", "/tmp/depotledger-submission")),
    )
    logs = Path(os.getenv("DEPOTLEDGER_LOGS_DIR", "/logs/verifier"))
    evidence = Path(os.getenv("DEPOTLEDGER_EVIDENCE_DIR", "/workspace/evidence"))
    config_path = Path(os.getenv("DEPOTLEDGER_CONFIG", "/workspace/config/config.json"))
    backup = Path(os.getenv("DEPOTLEDGER_BACKUP_CONFIG", "/workspace/runtime-config/config.json"))

    if not config_path.is_file() and backup.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup, config_path)
    if not config_path.is_file():
        raise RuntimeError(f"runtime configuration is missing at {config_path}")

    values = json.loads(config_path.read_text(encoding="utf-8"))
    logs.mkdir(parents=True, exist_ok=True)
    return Config(
        submission_dir=submission,
        source_submission=source_submission,
        logs_dir=logs,
        evidence_dir=evidence,
        config_path=config_path,
        region=str(values["region"]),
        endpoint_url=str(values["aws_endpoint_url"]),
        values=values,
    )
