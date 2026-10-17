"""Optional one-time test run for a trusted Python repository."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from .models import CheckResult


def _repository_python(repo: Path) -> str:
    """Prefer a conventional repository virtual environment when one exists."""
    candidates = (
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "bin" / "python",
        repo / "venv" / "Scripts" / "python.exe",
        repo / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def run_pytest_once(repo: Path, timeout_seconds: int = 120) -> CheckResult:
    if not (repo / "pyproject.toml").exists() and not (repo / "pytest.ini").exists() and not (repo / "tests").is_dir():
        return CheckResult(status="unavailable", note="No obvious pytest configuration or tests/ directory was found.")
    started = time.monotonic()
    python_executable = _repository_python(repo)
    command = [python_executable, "-m", "pytest", "-q"]
    display_command = f'"{python_executable}" -m pytest -q'
    try:
        # Do not use shell=True. This still executes project tests, so only run it for trusted repositories.
        result = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        excerpt = _tail((exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        return CheckResult(status="timed_out", command=display_command,
                           duration_seconds=round(time.monotonic() - started, 2), output_excerpt=excerpt,
                           note=f"Pytest exceeded the {timeout_seconds}-second timeout.")
    except OSError as exc:
        return CheckResult(status="unavailable", command=display_command,
                           duration_seconds=round(time.monotonic() - started, 2),
                           note=f"Could not start pytest: {type(exc).__name__}.")

    combined = result.stdout + "\n" + result.stderr
    return CheckResult(
        status="passed" if result.returncode == 0 else "failed",
        command=display_command,
        exit_code=result.returncode,
        duration_seconds=round(time.monotonic() - started, 2),
        output_excerpt=_tail(combined),
        note=("Suite passed. This does not prove or disprove an individual finding."
              if result.returncode == 0 else
              "Suite failed. A failure is not automatically attributable to any individual finding."),
    )


def _tail(value: str, limit: int = 3000) -> str:
    return value[-limit:]
