"""Runs the submission's lifecycle scripts."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import DeadlineExceeded, SubmissionFailure

DEPLOY_TIMEOUT = 720
DESTROY_TIMEOUT = 900
MAX_OUTPUT = 8 * 1024 * 1024


def _run(script: Path, cwd: Path, timeout: int, label: str, logs_dir: Path) -> str:
    if not script.is_file():
        raise SubmissionFailure(f"{script.name} is missing")
    try:
        result = subprocess.run(
            [str(script)], cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except PermissionError as exc:
        raise SubmissionFailure(f"{script.name} is not executable") from exc
    except subprocess.TimeoutExpired as exc:
        _capture(logs_dir, label, exc.stdout or b"")
        raise DeadlineExceeded(f"{script.name} exceeded its {timeout}s budget") from exc

    _capture(logs_dir, label, result.stdout or b"")
    if len(result.stdout or b"") > MAX_OUTPUT:
        raise SubmissionFailure(f"{script.name} produced more than 8 MiB of output")
    if result.returncode != 0:
        tail = (result.stdout or b"")[-4000:].decode("utf-8", "replace")
        raise SubmissionFailure(f"{script.name} exited {result.returncode}\n{tail}")
    return (result.stdout or b"").decode("utf-8", "replace")


def _capture(logs_dir: Path, label: str, output: bytes) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"{label}.txt").write_bytes(output[-MAX_OUTPUT:])


def deploy(submission: Path, logs_dir: Path, label: str = "deploy") -> str:
    return _run(submission / "deploy.sh", submission, DEPLOY_TIMEOUT, label, logs_dir)


def destroy(submission: Path, logs_dir: Path, label: str = "destroy") -> str:
    return _run(submission / "destroy.sh", submission, DESTROY_TIMEOUT, label, logs_dir)
