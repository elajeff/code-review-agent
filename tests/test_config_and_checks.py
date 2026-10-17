from pathlib import Path

import pytest

from review_agent import config
from review_agent.config import _bounded_int
from review_agent.checks import run_pytest_once


@pytest.mark.parametrize("raw", ["0", "999999", "abc", "2.5"])
def test_review_limits_reject_invalid_values(monkeypatch, raw):
    monkeypatch.setenv("TEST_REVIEW_LIMIT", raw)
    with pytest.raises(ValueError):
        _bounded_int("TEST_REVIEW_LIMIT", 10, 1, 100)


def test_review_limits_accept_boundary_values(monkeypatch):
    monkeypatch.setenv("TEST_REVIEW_LIMIT", "100")
    assert _bounded_int("TEST_REVIEW_LIMIT", 10, 1, 100) == 100


def test_test_runner_does_not_run_when_no_test_setup_exists(tmp_path: Path):
    result = run_pytest_once(tmp_path)
    assert result.status == "unavailable"
    assert "No obvious pytest" in result.note


def test_provider_settings_load_from_tool_installation_not_review_target(monkeypatch):
    loaded = []
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "load_dotenv", lambda path, override=False: loaded.append(Path(path)))

    settings = config.load_settings()

    assert settings.gemini_api_key == "test-key"
    assert loaded == [Path(config.__file__).resolve().parents[1] / ".env"]


def test_provider_quota_error_is_actionable_and_does_not_echo_details():
    from review_agent.gemini_review import _provider_error

    class FakeApiError(Exception):
        code = 429

    error = _provider_error("review", FakeApiError("secret=do-not-print"))
    assert "HTTP 429" in str(error)
    assert "check provider usage before retrying" in str(error)
    assert "do-not-print" not in str(error)


def test_test_runner_uses_target_repository_virtualenv_when_available(tmp_path, monkeypatch):
    import subprocess
    from review_agent import checks

    (tmp_path / "tests").mkdir()
    target_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    target_python.parent.mkdir(parents=True)
    target_python.write_bytes(b"placeholder for mocked subprocess")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    result = checks.run_pytest_once(tmp_path)

    assert result.status == "passed"
    assert calls[0][0][0] == str(target_python)
    assert calls[0][0][1:] == ["-m", "pytest", "-q"]
    assert "python.exe" in result.command
