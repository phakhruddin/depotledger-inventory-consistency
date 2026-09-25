"""Weighted, per-category scoring.

The obligation file is the single source of truth for what this task is worth.
load_obligations refuses to run on a file whose weights do not add up, so the
published score table in instruction.md and reasoning.md cannot silently drift
away from what the verifier actually awards.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .results import CheckResult, Outcome, ScoreReport


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate obligation-contract key: {key!r}")
        value[key] = item
    return value


def load_obligations(path: Path) -> dict[str, Any]:
    # JSON is a strict subset of YAML; keeping the file JSON-compatible avoids
    # a PyYAML dependency in the disposable verifier image.
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)

    obligations = data.get("obligations", [])
    identifiers = [item["id"] for item in obligations]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("obligation ids must be unique")
    if sum(int(item["weight"]) for item in obligations) != int(data["total_points"]):
        raise ValueError("obligation weights do not equal total_points")

    categories = data.get("categories", [])
    category_ids = [item["id"] for item in categories]
    if len(set(category_ids)) != len(category_ids):
        raise ValueError("score category ids must be unique")
    if sum(int(item["weight"]) for item in categories) != int(data["total_points"]):
        raise ValueError("score category weights do not equal total_points")

    assigned = [identifier for category in categories for identifier in category["obligations"]]
    if len(set(assigned)) != len(assigned):
        raise ValueError("each obligation must belong to exactly one score category")
    if set(assigned) != set(identifiers):
        raise ValueError("score categories must contain every obligation exactly once")

    weights = {item["id"]: int(item["weight"]) for item in obligations}
    for category in categories:
        actual = sum(weights[identifier] for identifier in category["obligations"])
        if actual != int(category["weight"]):
            raise ValueError(
                f"score category {category['id']!r}: expected {category['weight']}, found {actual}"
            )

    for plane, expected in data["planes"].items():
        actual = sum(int(item["weight"]) for item in obligations if item["plane"] == plane)
        if actual != int(expected):
            raise ValueError(f"plane {plane!r}: expected {expected}, found {actual}")

    return data


def score_results(
    spec: dict[str, Any],
    results: Iterable[CheckResult],
    *,
    gate_overrides: dict[str, bool] | None = None,
) -> ScoreReport:
    result_list = list(results)
    by_id = {result.obligation_id: result for result in result_list}
    required = {item["id"] for item in spec["obligations"]}
    unknown = set(by_id) - required
    if unknown:
        raise ValueError(f"unknown obligation results: {sorted(unknown)}")

    trial_valid = True
    raw_score = 0
    plane_scores = {
        plane: {"earned": 0, "possible": int(weight)}
        for plane, weight in spec["planes"].items()
    }
    category_scores = {
        item["id"]: {"label": item["label"], "earned": 0, "possible": int(item["weight"])}
        for item in spec["categories"]
    }
    category_by_obligation = {
        identifier: category["id"]
        for category in spec["categories"]
        for identifier in category["obligations"]
    }
    obligation_scores: dict[str, dict[str, Any]] = {}
    hard_gates = {gate: True for gate in spec.get("hard_gates", [])}
    # Integrity is established by the orchestrator and has no weighted row.
    hard_gates["trial.integrity"] = True
    hard_gates.update(gate_overrides or {})
    caps: list[str] = []

    for item in spec["obligations"]:
        result = by_id.get(item["id"])
        category = category_by_obligation[item["id"]]
        earned = 0
        if result is None:
            trial_valid = False
            outcome = "missing"
        else:
            outcome = result.outcome.value
            # INVALID means the verifier or environment broke: the trial cannot
            # be scored at all. NOT_RUN means the obligation was never reached
            # because shared setup failed -- that is a legitimate zero, not an
            # invalid trial, so an empty or broken submission still scores 0
            # rather than being discarded.
            if result.outcome is Outcome.INVALID:
                trial_valid = False
            if result.outcome is Outcome.PASS:
                earned = int(item["weight"])
                raw_score += earned
                plane_scores[item["plane"]]["earned"] += earned
                category_scores[category]["earned"] += earned

        obligation_scores[item["id"]] = {
            "category": category,
            "plane": item["plane"],
            "earned": earned,
            "possible": int(item["weight"]),
            "outcome": outcome,
            "experiments": list(item["then"]),
        }

        # Tri-state. A gate is breached only when its obligation actually ran
        # and failed. One that was never reached is "not_evaluated", never a
        # breach: reporting "internal_path_blocked FAILED" on a run that never
        # deployed would tell a reviewer a private path leaked when nothing
        # was ever served.
        gate = (result.hard_gate if result is not None else None) or item.get("hard_gate")
        if gate:
            if result is not None and result.outcome is Outcome.FAIL:
                hard_gates[gate] = False
            elif result is None or result.outcome is not Outcome.PASS:
                hard_gates[gate] = "not_evaluated"
        if result is not None and result.cap_reason:
            caps.append(result.cap_reason)

    score = raw_score
    caps_config = spec.get("score_caps", {})
    for reason in set(caps):
        if reason in caps_config:
            score = min(score, int(caps_config[reason]))

    total = int(spec["total_points"])
    # Harbor rewards are normalized to [0, 1]. Partial credit stays visible:
    # 8 of 100 is reported as 0.08, never collapsed to 0.
    reward = score / total if trial_valid and hard_gates["trial.integrity"] else 0.0

    return ScoreReport(
        score=score,
        raw_score=raw_score,
        total=total,
        threshold=int(spec["pass_threshold"]),
        reward=reward,
        trial_valid=trial_valid,
        hard_gates=hard_gates,
        plane_scores=plane_scores,
        category_scores=category_scores,
        obligation_scores=obligation_scores,
        results=result_list,
        caps_applied=sorted(set(caps)),
    )
