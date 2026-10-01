"""Runs the submission's lifecycle scripts."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

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


def deploy_until(submission: Path, logs_dir: Path, trigger: Callable[[], bool], label: str,
                 poll_seconds: float = 0.1) -> tuple[bool, float]:
    """Run deploy.sh and SIGKILL its whole process group when trigger() holds.

    The script runs in its own session, so the kill reaches every process it
    started that did not deliberately leave the session (terraform, providers,
    the AWS CLI, Python helpers). Returns (interrupted, seconds_elapsed).
    If deploy.sh exits on its own before the trigger fires, a non-zero exit is
    a SubmissionFailure and a zero exit returns interrupted=False.
    """
    script = submission / "deploy.sh"
    if not script.is_file():
        raise SubmissionFailure("deploy.sh is missing")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{label}.txt"
    started = time.monotonic()
    with open(log_path, "wb") as sink:
        try:
            proc = subprocess.Popen([str(script)], cwd=submission, stdout=sink,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except PermissionError as exc:
            raise SubmissionFailure("deploy.sh is not executable") from exc
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    if code != 0:
                        tail = log_path.read_bytes()[-4000:].decode("utf-8", "replace")
                        raise SubmissionFailure(f"deploy.sh exited {code}\n{tail}")
                    return False, time.monotonic() - started
                if time.monotonic() - started > DEPLOY_TIMEOUT:
                    raise DeadlineExceeded(f"deploy.sh exceeded its {DEPLOY_TIMEOUT}s budget")
                if trigger():
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=60)
                    return True, time.monotonic() - started
                time.sleep(poll_seconds)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=60)
