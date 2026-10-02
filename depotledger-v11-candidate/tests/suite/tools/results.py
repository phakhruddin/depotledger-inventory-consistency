from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INVALID = "invalid"
    NOT_RUN = "not_run"


@dataclass
class CheckResult:
    obligation_id: str
    outcome: Outcome
    summary: str
    duration_seconds: float = 0.0
    evidence: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    hard_gate: str | None = None
    cap_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value


@dataclass
class ScoreReport:
    score: int
    raw_score: int
    total: int
    threshold: int
    reward: float
    trial_valid: bool
    hard_gates: dict[str, bool]
    plane_scores: dict[str, dict[str, int]]
    category_scores: dict[str, dict[str, Any]]
    obligation_scores: dict[str, dict[str, Any]]
    results: list[CheckResult]
    caps_applied: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "raw_score": self.raw_score,
            "total": self.total,
            "threshold": self.threshold,
            "reward": self.reward,
            "trial_valid": self.trial_valid,
            "hard_gates": self.hard_gates,
            "plane_scores": self.plane_scores,
            "category_scores": self.category_scores,
            "obligation_scores": self.obligation_scores,
            "caps_applied": self.caps_applied,
            "results": [result.as_dict() for result in self.results],
        }
