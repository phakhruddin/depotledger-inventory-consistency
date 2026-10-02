"""Failure taxonomy.

The distinction that matters: a SubmissionFailure or a plain assertion is the
agent's fault and scores zero for that obligation. A HarnessError is our fault
and invalidates the trial instead of being recorded as model difficulty.
"""
from __future__ import annotations


class HarnessError(RuntimeError):
    """The verifier or environment failed. Not the submission's fault."""


class SubmissionFailure(AssertionError):
    """The submission did not meet the public contract."""


class DeadlineExceeded(SubmissionFailure):
    """The submission did not reach the required state within its window."""


class Oversell(SubmissionFailure):
    """More units were reserved than existed."""


class CleanupLeak(SubmissionFailure):
    """Resources owned by this deployment survived destruction."""
