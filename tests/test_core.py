import os
from pathlib import Path
import subprocess

import pytest

from review_agent.git_diff import get_python_diff, parse_diff
from review_agent.models import CandidateFinding
from review_agent.evidence import assign_priority, validate_findings


def candidate(**overrides):
    values = {
        "file": "src/wallet.py",
        "line": 4,
        "severity": "high",
        "category": "input_validation",
        "title": "Negative amount increases balance",
        "explanation": "A negative amount is accepted and subtracting it increases the balance.",
        "preconditions": "A caller passes amount=-50 to this function.",
        "expected_behavior": "The method rejects non-positive withdrawal amounts.",
        "actual_behavior": "The method subtracts a negative amount from the balance.",
        "evidence_excerpt": "return balance - amount",
        "confidence": 0.9,
        "reproduction_hint": "Call withdraw(100, -50) and inspect the result.",
        "reproduction_test": "",
    }
    values.update(overrides)
    return CandidateFinding(**values)


def test_diff_parser_tracks_only_added_lines_in_new_file():
    diff = """diff --git a/src/wallet.py b/src/wallet.py
index 111..222 100644
--- a/src/wallet.py
+++ b/src/wallet.py
@@ -1,2 +1,3 @@
 def withdraw(balance, amount):
+    if amount > balance:
+        raise ValueError('no funds')
     return balance - amount
"""
    files = parse_diff(diff)
    assert len(files) == 1
    assert files[0].path == "src/wallet.py"
    assert files[0].changed_lines == {2, 3}


def test_diff_parser_ignores_deleted_only_files():
    diff = """diff --git a/src/old.py b/src/old.py
deleted file mode 100644
--- a/src/old.py
+++ /dev/null
@@ -1 +0,0 @@
-print('old')
"""
    assert parse_diff(diff) == []


@pytest.fixture
def repo(tmp_path: Path):
    source = tmp_path / "src" / "wallet.py"
    source.parent.mkdir()
    source.write_text(
        "def withdraw(balance, amount):\n"
        "    if amount > balance:\n"
        "        raise ValueError('no funds')\n"
        "    return balance - amount\n",
        encoding="utf-8",
    )
    return tmp_path


def changed_file():
    from review_agent.git_diff import ChangedFile
    return ChangedFile(path="src/wallet.py", diff="", changed_lines={4})


def test_l1_accepts_finding_with_changed_line_and_matching_evidence(repo):
    accepted, rejected = validate_findings(repo, [changed_file()], [candidate()])
    assert len(accepted) == 1
    assert accepted[0].verification_level == "L1_STATIC"
    assert "not proven" in accepted[0].verification_status
    assert rejected == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"line": 3}, "not an added/changed line"),
        ({"file": "../secrets.py"}, "safe repository-relative"),
        ({"file": "C:\\secrets.py"}, "safe repository-relative"),
        ({"file": "src/./wallet.py"}, "safe repository-relative"),
        ({"file": "src/\x00wallet.py"}, "safe repository-relative"),
        ({"file": "src/missing.py"}, "not a changed Python file"),
        ({"evidence_excerpt": "unrelated source"}, "does not match source"),
        ({"expected_behavior": "same text", "actual_behavior": "same text"}, "concrete condition"),
    ],
)
def test_l1_rejects_weak_or_unlocated_findings(repo, overrides, reason):
    accepted, rejected = validate_findings(repo, [changed_file()], [candidate(**overrides)])
    assert accepted == []
    assert len(rejected) == 1
    assert reason in rejected[0].reason


def test_priority_maps_severity_without_additive_confidence_score():
    high = candidate(severity="high", category="security", confidence=0.35)
    same_severity_high_confidence = candidate(severity="high", category="other", confidence=0.99)
    medium = candidate(severity="medium", category="other", confidence=0.99)
    low = candidate(severity="low", category="other", confidence=0.99)
    assert assign_priority(high) == "P1"
    assert assign_priority(same_severity_high_confidence) == "P1"
    assert assign_priority(medium) == "P2"
    assert assign_priority(low) == "P3"


def test_low_self_reported_confidence_is_not_treated_as_disproof(repo):
    accepted, rejected = validate_findings(
        repo, [changed_file()], [candidate(confidence=0.1)]
    )
    assert len(accepted) == 1
    assert rejected == []


def test_duplicate_finding_is_not_reported_twice(repo):
    one = candidate()
    two = candidate(title="Negative amount increases balance")
    accepted, rejected = validate_findings(repo, [changed_file()], [one, two])
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert "Duplicate" in rejected[0].reason


def test_evidence_for_very_long_changed_line_can_use_a_distinctive_excerpt(tmp_path):
    from review_agent.git_diff import ChangedFile

    long_line = '    return "' + ("x" * 600) + '"'
    source = tmp_path / "src" / "wallet.py"
    source.parent.mkdir()
    source.write_text(
        "def withdraw(balance, amount):\n"
        "    if amount > balance:\n"
        "        raise ValueError('no funds')\n"
        + long_line + "\n",
        encoding="utf-8",
    )
    distinctive = long_line[30:100]
    item = changed_file()
    accepted, rejected = validate_findings(
        tmp_path, [item], [candidate(evidence_excerpt=distinctive)]
    )
    assert len(accepted) == 1
    assert rejected == []


def test_git_diff_is_rejected_as_soon_as_the_configured_limit_is_exceeded(tmp_path):
    def git(*args):
        environment = os.environ.copy()
        environment.update({
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        })
        subprocess.run(
            ["git", *args], cwd=tmp_path, env=environment,
            check=True, capture_output=True, text=True,
        )

    git("init", "-b", "main")
    source = tmp_path / "example.py"
    source.write_text("value = 0\n", encoding="utf-8")
    git("add", "example.py")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")

    source.write_text("\n".join(f"value_{index} = {index}" for index in range(5000)) + "\n", encoding="utf-8")
    git("add", "example.py")
    git("commit", "-m", "large change")

    diff_text, changed_files = get_python_diff(tmp_path, "main", max_chars=1_000_000)
    assert "value_4999 = 4999" in diff_text
    assert len(changed_files) == 1
    assert changed_files[0].path == "example.py"

    with pytest.raises(ValueError, match="character limit"):
        get_python_diff(tmp_path, "main", max_chars=200)


def test_context_payload_contains_only_fields_needed_for_second_pass(repo):
    accepted, rejected = validate_findings(repo, [changed_file()], [candidate()])
    assert len(accepted) == 1 and not rejected

    payload = accepted[0].context_candidate_payload()
    assert set(payload) == {
        "finding_id", "file", "line", "severity", "category", "title",
        "explanation", "preconditions", "expected_behavior", "actual_behavior",
        "evidence_excerpt",
    }
    assert "confidence" not in payload
    assert "reproduction_test" not in payload
