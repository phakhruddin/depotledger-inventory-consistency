"""Trial orchestration.

One obligation becomes one visible pytest test. The session deploys once,
shares that deployment across checks, and writes the weighted report at the
end. A shared setup failure marks the trial invalid rather than scoring every
dependent obligation as an independent model failure.
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema
import pytest

from .aws import Cloud
from .config import Config, from_environment
from .deployment import deploy
from .api import Api
from .errors import CleanupLeak, HarnessError, Oversell, SubmissionFailure
from .results import CheckResult, Outcome
from .scoring import load_obligations, score_results

SPEC_PATH = Path(__file__).resolve().parent.parent / "obligations.yaml"

SCHEMA_PATH = Path("/workspace/contracts/schemas/manifest.schema.json")


@dataclass
class TrialContext:
    config: Config
    cloud: Cloud
    manifest: dict[str, Any] = field(default_factory=dict)
    api: Api | None = None
    baseline: dict[str, list[str]] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def stock_table(self) -> str:
        return self.manifest["data"]["stock_table"]["name"]

    @property
    def reservations_table(self) -> str:
        return self.manifest["data"]["reservations_table"]["name"]

    @property
    def bucket(self) -> str:
        return self.manifest["data"]["snapshot_bucket"]["name"]

    def reload_manifest(self) -> None:
        self.manifest = json.loads(self.config.manifest_path.read_text(encoding="utf-8"))
        self.api = Api(self.manifest["edge"]["connect_url"], self.manifest["edge"]["host_header"],
                       self.config.admin_token)


Check = Callable[[TrialContext], CheckResult]


def obligation(identifier: str) -> Callable[[Check], Callable[[Any], None]]:
    """Turn one semantic check into one visible pytest test."""

    def decorate(check: Check) -> Callable[[Any], None]:
        def test(trial) -> None:
            trial.run(identifier, check)

        test.__name__ = check.__name__
        test.__doc__ = check.__doc__
        test.obligation_id = identifier  # type: ignore[attr-defined]
        test.raw_check = check  # type: ignore[attr-defined]
        return test

    return decorate


# A severe failure caps the whole run, not just its own obligation. The cap a
# leak triggers depends on what leaked, so it is keyed by obligation.
CAP_BY_OBLIGATION = {
    "observed.no_oversell": "oversell",
    "lifecycle.destroy_clean": "cleanup_leak",
}


def _failure_result(identifier: str, exc: BaseException, duration: float) -> CheckResult:
    details = {"exception": type(exc).__name__, "message": str(exc)[:4000]}
    if isinstance(exc, HarnessError):
        # Our fault. Never counted as model difficulty.
        return CheckResult(identifier, Outcome.INVALID, f"harness error: {exc}",
                           duration_seconds=duration, details=details)
    if isinstance(exc, (SubmissionFailure, AssertionError)):
        cap = None
        if isinstance(exc, (Oversell, CleanupLeak)):
            cap = CAP_BY_OBLIGATION.get(identifier)
        return CheckResult(identifier, Outcome.FAIL, str(exc)[:600],
                           duration_seconds=duration, details=details, cap_reason=cap)
    details["traceback"] = traceback.format_exc()[-4000:]
    return CheckResult(identifier, Outcome.INVALID, f"unexpected error: {exc}",
                       duration_seconds=duration, details=details)


class TrialSession:
    def __init__(self) -> None:
        self.spec = load_obligations(SPEC_PATH)
        self.results: list[CheckResult] = []
        self._context: TrialContext | None = None
        self._setup_error: BaseException | None = None
        self._config = from_environment()

    # -- shared setup -------------------------------------------------------
    @property
    def context(self) -> TrialContext:
        if self._setup_error is not None:
            raise self._setup_error
        if self._context is None:
            try:
                self._context = self._prepare()
            except BaseException as exc:  # noqa: BLE001 - remembered, then re-raised
                # Remember it so the remaining obligations report "not
                # evaluated" instead of each re-running the failed deploy.
                self._setup_error = exc
                raise
        return self._context

    def _prepare(self) -> TrialContext:
        config = self._config
        cloud = Cloud(config)

        # Pre-existing resources that share the prefix. They must survive the
        # whole trial, destruction included.
        try:
            cloud.create_decoys()
        except Exception as exc:  # noqa: BLE001 - our fault, not the submission's
            raise HarnessError(f"could not create pre-existing resources: {exc}") from exc

        # Inventory before touching anything, so destruction can prove it
        # removed only what this deployment owns.
        baseline = cloud.prefixed_inventory(config.prefix)

        deploy(config.submission_dir, config.logs_dir)

        manifest_path = config.manifest_path
        if not manifest_path.is_file():
            raise SubmissionFailure("deploy.sh did not write manifest.json")
        if manifest_path.stat().st_size > 1024 * 1024:
            raise SubmissionFailure("manifest.json exceeds 1 MiB")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SubmissionFailure("manifest.json is not valid JSON") from exc

        if SCHEMA_PATH.is_file():
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            try:
                jsonschema.validate(manifest, schema)
            except jsonschema.ValidationError as exc:
                raise SubmissionFailure(f"manifest.json does not match the schema: {exc.message}")

        context = TrialContext(config=config, cloud=cloud, manifest=manifest, baseline=baseline)
        context.reload_manifest()
        return context

    # -- execution ----------------------------------------------------------
    def run(self, identifier: str, check: Check) -> None:
        started = time.monotonic()

        # Shared setup failed, so this obligation was never exercised. Record
        # it as not evaluated rather than as an independent failure: a dozen
        # FAILs on a run that never deployed misrepresents one fault as
        # many, and makes hard gates look breached when nothing was served.
        if self._setup_error is not None:
            self.results.append(CheckResult(
                identifier, Outcome.NOT_RUN,
                f"not evaluated: the deployment did not complete ({self._setup_error})",
                duration_seconds=time.monotonic() - started,
                details={"setup_error": type(self._setup_error).__name__},
            ))
            pytest.fail(
                f"[{identifier}] not evaluated: deployment did not complete",
                pytrace=False,
            )

        try:
            context = self.context
            result = check(context)
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            if self._setup_error is exc:
                # This call is the one that discovered the setup failure. A
                # harness fault during setup invalidates the trial; anything
                # else is the submission's and scores zero.
                self.results.append(CheckResult(
                    identifier,
                    Outcome.INVALID if isinstance(exc, HarnessError) else Outcome.NOT_RUN,
                    f"not evaluated: the deployment did not complete ({exc})",
                    duration_seconds=time.monotonic() - started,
                    details={"setup_error": type(exc).__name__},
                ))
                pytest.fail(
                    f"[{identifier}] deployment did not complete: {exc}",
                    pytrace=False,
                )
            result = _failure_result(identifier, exc, time.monotonic() - started)
            self.results.append(result)
            pytest.fail(f"[{identifier}] {result.summary}", pytrace=False)
        else:
            result.duration_seconds = time.monotonic() - started
            self.results.append(result)
            if result.outcome is not Outcome.PASS:
                pytest.fail(f"[{identifier}] {result.summary}", pytrace=False)

    # -- reporting ----------------------------------------------------------
    def finish(self) -> None:
        gate_overrides: dict[str, bool] = {}
        # A failed deployment is a legitimate zero, not an invalid trial: an
        # empty or broken submission must score 0, not be discarded. Only a
        # harness fault clears trial.integrity, and _failure_result already
        # marks those INVALID.
        report = score_results(self.spec, self.results, gate_overrides=gate_overrides)

        logs = self._config.logs_dir
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "report.json").write_text(json.dumps(report.as_dict(), indent=2) + "\n")
        # Harbor parses reward.json as a flat mapping of numbers. Keep it flat:
        # a nested object here fails validation even when every test passed.
        (logs / "reward.json").write_text(
            json.dumps({"reward": report.reward, "score": report.score}) + "\n"
        )
        (logs / "reward.txt").write_text(f"{report.reward}\n")

        summary = {
            "score": report.score,
            "raw_score": report.raw_score,
            "reward": report.reward,
            "trial_valid": report.trial_valid,
            "caps_applied": report.caps_applied,
            "breached_gates": sorted(k for k, v in report.hard_gates.items() if v is False),
            "gates_not_evaluated": sorted(
                k for k, v in report.hard_gates.items() if v == "not_evaluated"
            ),
            "deployment": {
                "completed": self._setup_error is None,
                "reason": None if self._setup_error is None else str(self._setup_error)[:600],
            },
            "categories": report.category_scores,
        }
        (logs / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
