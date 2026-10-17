from pathlib import Path
from types import SimpleNamespace

import pytest

from review_agent.context import build_context_bundle
from review_agent.git_diff import ChangedFile
from review_agent.evidence import apply_context_assessments
from review_agent.models import ContextAssessment, ContextReviewResponse


def finding(file="src/service.py", line=3, title="Negative amount reaches calculation"):
    return SimpleNamespace(file=file, line=line, title=title, category="input_validation", severity="high")


def test_context_expansion_adds_scope_callers_and_matching_tests(tmp_path: Path):
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text(
        "def _calculate_fee(amount):\n"
        "    return amount * 0.05\n"
        "\n"
        "def submit(amount):\n"
        "    if amount < 0:\n"
        "        raise ValueError('amount must be positive')\n"
        "    return _calculate_fee(amount)\n",
        encoding="utf-8",
    )
    test = tmp_path / "tests" / "test_service.py"
    test.parent.mkdir()
    test.write_text(
        "from src.service import submit\n\n"
        "def test_submit_rejects_negative_amount():\n"
        "    try:\n"
        "        submit(-1)\n"
        "    except ValueError:\n"
        "        return\n"
        "    assert False\n",
        encoding="utf-8",
    )
    bundle = build_context_bundle(tmp_path, [finding(line=2)])
    assert "enclosing scope: src/service.py" in bundle.text
    assert "call to _calculate_fee" in bundle.text
    assert "test test_submit_rejects_negative_amount" in bundle.text
    assert bundle.chars_used == len(bundle.text)
    assert bundle.blocks_added >= 3


def test_context_budget_is_enforced(tmp_path: Path):
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def work():\n    return 1\n", encoding="utf-8")
    bundle = build_context_bundle(tmp_path, [finding(line=2)], max_chars=500)
    assert bundle.chars_used <= 500

    with pytest.raises(ValueError, match="at least 500"):
        build_context_bundle(tmp_path, [finding(line=2)], max_chars=499)


def test_context_skips_symlink_paths(tmp_path: Path):
    external = tmp_path.parent / "outside.py"
    external.write_text("def work():\n    return 1\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this platform")
    bundle = build_context_bundle(tmp_path, [finding(file="linked.py", line=2)])
    assert "outside.py" not in bundle.text


def test_context_assessment_does_not_overstate_support():
    from review_agent.models import AcceptedFinding

    candidate = AcceptedFinding(
        finding_id="abc123", file="src/a.py", line=3, severity="high", category="logic",
        title="A concrete logic bug", explanation="A computation gives the wrong result here.",
        preconditions="A particular input is supplied to the function.",
        expected_behavior="The function should return the correct total.",
        actual_behavior="The function returns a negative total instead.",
        evidence_excerpt="return total - price", confidence=0.9, priority="P1",
    )
    response = ContextReviewResponse(assessments=[ContextAssessment(
        finding_id="abc123", decision="supported",
        reasoning="The quoted line belongs to the relevant implementation.",
        evidence_file="src/a.py",
        relevant_evidence="return total - price",
    )])
    accepted, rejected = apply_context_assessments(
        [candidate], response, context_text="[enclosing scope: src/a.py:1-3]\n 3: return total - price"
    )
    assert len(accepted) == 1 and not rejected
    assert accepted[0].context_decision == "supported"
    assert "not proven" in accepted[0].verification_status


def test_context_contradiction_rejects_a_candidate_only_with_grounded_evidence():
    from review_agent.models import AcceptedFinding

    candidate = AcceptedFinding(
        finding_id="abc123", file="src/a.py", line=3, severity="high", category="logic",
        title="A concrete logic bug", explanation="A computation gives the wrong result here.",
        preconditions="A particular input is supplied to the function.",
        expected_behavior="The function should return the correct total.",
        actual_behavior="The function returns a negative total instead.",
        evidence_excerpt="return total - price", confidence=0.9, priority="P1",
    )
    response = ContextReviewResponse(assessments=[ContextAssessment(
        finding_id="abc123", decision="contradicted",
        reasoning="A caller guard prevents this invalid input from reaching the function.",
        evidence_file="src/caller.py",
        relevant_evidence="if amount < 0:",
    )])
    accepted, rejected = apply_context_assessments(
        [candidate], response,
        context_text="[caller: src/caller.py:1-2]\nif amount < 0:\n    raise ValueError()"
    )
    assert not accepted and len(rejected) == 1
    assert "contradicts" in rejected[0].reason


def test_ungrounded_context_contradiction_does_not_suppress_finding():
    from review_agent.models import AcceptedFinding

    candidate = AcceptedFinding(
        finding_id="abc123", file="src/a.py", line=3, severity="high", category="logic",
        title="A concrete logic bug", explanation="A computation gives the wrong result here.",
        preconditions="A particular input is supplied to the function.",
        expected_behavior="The function should return the correct total.",
        actual_behavior="The function returns a negative total instead.",
        evidence_excerpt="return total - price", confidence=0.9, priority="P1",
    )
    response = ContextReviewResponse(assessments=[ContextAssessment(
        finding_id="abc123", decision="contradicted",
        reasoning="The caller prevents the condition.",
        evidence_file="src/caller.py",
        relevant_evidence="there is validation elsewhere",
    )])
    accepted, rejected = apply_context_assessments(
        [candidate], response, context_text="def calculate_total(price):\n    return total - price"
    )
    assert len(accepted) == 1 and not rejected
    assert accepted[0].context_decision == "uncertain"
    assert "verifiable excerpt" in accepted[0].context_notes


def test_missing_context_assessment_keeps_candidate_unverified():
    from review_agent.models import AcceptedFinding

    candidate = AcceptedFinding(
        finding_id="abc123", file="src/a.py", line=3, severity="medium", category="logic",
        title="A concrete logic bug", explanation="A computation gives the wrong result here.",
        preconditions="A particular input is supplied to the function.",
        expected_behavior="The function should return the correct total.",
        actual_behavior="The function returns a negative total instead.",
        evidence_excerpt="return total - price", confidence=0.9, priority="P2",
    )
    accepted, rejected = apply_context_assessments([candidate], ContextReviewResponse(assessments=[]))
    assert len(accepted) == 1 and not rejected
    assert accepted[0].context_decision == "uncertain"
    assert "No matching contextual assessment" in accepted[0].context_notes


def test_context_bundle_includes_before_and_after_diff_hunk(tmp_path: Path):
    source = tmp_path / "src" / "wallet.py"
    source.parent.mkdir()
    source.write_text(
        "def withdraw(balance, amount):\n"
        "    if amount < balance:\n"
        "        raise ValueError('Insufficient funds')\n"
        "    return balance - amount\n",
        encoding="utf-8",
    )
    diff = (
        "+++ b/src/wallet.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def withdraw(balance, amount):\n"
        "-    if amount > balance:\n"
        "+    if amount < balance:\n"
        "         raise ValueError('Insufficient funds')\n"
        "     return balance - amount\n"
    )
    changed = ChangedFile(path="src/wallet.py", diff=diff, changed_lines={2})
    bundle = build_context_bundle(tmp_path, [finding(file="src/wallet.py", line=2)], changed_files=[changed])
    assert "before/after diff hunk" in bundle.text
    assert "-    if amount > balance:" in bundle.text
    assert "+    if amount < balance:" in bundle.text


def test_context_quote_in_wrong_file_does_not_suppress_candidate():
    from review_agent.models import AcceptedFinding

    candidate_finding = AcceptedFinding(
        finding_id="abc123", file="src/a.py", line=3, severity="high", category="logic",
        title="A concrete logic bug", explanation="A computation gives the wrong result here.",
        preconditions="A particular input is supplied to the function.",
        expected_behavior="The function should return the correct total.",
        actual_behavior="The function returns a negative total instead.",
        evidence_excerpt="return total - price", confidence=0.9, priority="P1",
    )
    response = ContextReviewResponse(assessments=[ContextAssessment(
        finding_id="abc123", decision="contradicted",
        reasoning="A caller guard prevents this input.",
        evidence_file="src/caller.py",
        relevant_evidence="if amount < 0:",
    )])
    context = (
        "[enclosing scope: src/a.py:1-3]\nreturn total - price\n\n"
        "[caller: src/other.py:1-2]\nif amount < 0:\n    raise ValueError()"
    )
    accepted, rejected = apply_context_assessments([candidate_finding], response, context)

    assert len(accepted) == 1 and not rejected
    assert accepted[0].context_decision == "uncertain"
