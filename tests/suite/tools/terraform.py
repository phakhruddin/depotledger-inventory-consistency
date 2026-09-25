"""Terraform state and plan access.

State is read for declaration checks. The plan is generated directly against
the submission's infra directory, without going through deploy.sh, so a
variable supplied only by a -var flag inside deploy.sh is caught as the
lifecycle bug it is.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterator

from .errors import HarnessError, SubmissionFailure


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # noqa: PERF203
        raise SubmissionFailure(f"{' '.join(args[:3])} timed out after {timeout}s") from exc
    except OSError as exc:
        raise HarnessError(f"could not execute {args[0]}: {exc}") from exc


def state(infra_dir: Path, *, timeout: int = 120) -> dict[str, Any]:
    result = _run(["terraform", f"-chdir={infra_dir}", "show", "-json"], timeout=timeout)
    if result.returncode != 0:
        raise SubmissionFailure(f"terraform show failed: {result.stderr.strip()[:800]}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise SubmissionFailure("terraform show did not return JSON state") from exc


def managed_resources(state_json: dict[str, Any]) -> Iterator[dict[str, Any]]:
    root = state_json.get("values", {}).get("root_module", {})

    def walk(module: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield from module.get("resources", [])
        for child in module.get("child_modules", []):
            yield from walk(child)

    yield from walk(root)


def by_type(state_json: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [r for r in managed_resources(state_json) if r.get("type") == resource_type]


def plan(infra_dir: Path, *, timeout: int = 600, artifact: Path | None = None) -> dict[str, Any]:
    """Plan without refresh, exactly as the public contract describes."""
    result = _run(
        ["terraform", f"-chdir={infra_dir}", "plan", "-refresh=false",
         "-input=false", "-no-color", "-detailed-exitcode", "-out=/tmp/depotledger.tfplan"],
        timeout=timeout,
    )
    if result.returncode not in (0, 2):
        raise SubmissionFailure(
            "standalone terraform plan failed. Dynamic inputs must be persisted "
            "to an auto-loaded var file so a plan run outside deploy.sh resolves "
            f"them: {result.stderr.strip()[:800]}"
        )
    shown = _run(
        ["terraform", f"-chdir={infra_dir}", "show", "-json", "/tmp/depotledger.tfplan"],
        timeout=120,
    )
    if shown.returncode != 0:
        raise SubmissionFailure(f"terraform show of the plan failed: {shown.stderr.strip()[:800]}")
    if artifact is not None:
        try:
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(shown.stdout, encoding="utf-8")
        except OSError:
            pass
    return json.loads(shown.stdout)


def _attribute_drift(change: dict[str, Any]) -> list[str]:
    """Attributes whose planned value differs from state.

    Terraform records replace_paths when an attribute forces replacement; that
    is the authoritative answer. Fall back to a before/after key diff when the
    plan format does not carry it.
    """
    detail = change.get("change", {})
    forced = [
        ".".join(str(part) for part in path)
        for path in detail.get("replace_paths", []) or []
    ]
    if forced:
        return sorted(set(forced))
    before, after = detail.get("before") or {}, detail.get("after") or {}
    unknown = detail.get("after_unknown") or {}
    return sorted(
        key for key in set(before) | set(after)
        if before.get(key) != after.get(key) and not unknown.get(key)
    )


def describe_changes(changes: list[dict[str, Any]]) -> str:
    """One readable line per change, naming what actually differs."""
    lines = []
    for item in changes:
        drift = item.get("attributes") or []
        reason = item.get("reason") or ""
        lines.append(
            f"{item['address']} {item['actions']}"
            + (f" reason={reason}" if reason else "")
            + (f" attributes={drift[:6]}" if drift else "")
        )
    return "; ".join(lines)


def disruptive_changes(plan_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Creates, deletes and replacements.

    Update-only differences are tolerated: this endpoint does not return every
    field, so a refresh can report drift that reflects no real change. The
    public runtime contract says as much. Creating or deleting a resource is
    a real lifecycle bug and is not tolerated.
    """
    disruptive = {"create", "delete"}
    return [
        {
            "address": change.get("address", "<unknown>"),
            "type": change.get("type", "<unknown>"),
            "actions": change.get("change", {}).get("actions", []),
            "reason": change.get("action_reason"),
            "attributes": _attribute_drift(change),
        }
        for change in plan_json.get("resource_changes", [])
        if disruptive & set(change.get("change", {}).get("actions", []))
    ]
