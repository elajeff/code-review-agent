from review_agent.evaluation import build_case_diff, finding_matches_label, load_cases, summarize_results
from review_agent.git_diff import parse_diff
from review_agent.models import CandidateFinding, ContextAssessment, ContextReviewResponse


def candidate(**overrides):
    data = {
        "file": "src/wallet.py", "line": 2, "severity": "high", "category": "logic",
        "title": "Withdrawal comparison is reversed",
        "explanation": "The reversed comparison allows a withdrawal above the balance.",
        "preconditions": "The caller requests more than the available balance.",
        "expected_behavior": "Withdrawals above the balance should be rejected.",
        "actual_behavior": "The reversed comparison rejects valid amounts and permits an overdraw.",
        "evidence_excerpt": "if amount < balance:", "confidence": 0.9,
    }
    data.update(overrides)
    return CandidateFinding(**data)


def test_benchmark_contains_buggy_and_clean_cases():
    cases = load_cases()
    assert any(case["kind"] == "bug" for case in cases)
    assert any(case["kind"] == "clean" for case in cases)


def test_labeled_finding_requires_expected_line_category_and_concept():
    case = next(item for item in load_cases() if item["id"] == "withdrawal-comparison-reversed")
    assert finding_matches_label(candidate(), case)
    assert not finding_matches_label(candidate(line=3), case)
    assert not finding_matches_label(candidate(category="security"), case)
    weak = candidate(
        title="Unclear concern",
        explanation="Something might be wrong here.",
        preconditions="Some condition occurs.",
        expected_behavior="The expected result should happen.",
        actual_behavior="The actual result might differ.",
    )
    assert not finding_matches_label(weak, case)


def test_every_benchmark_fixture_tracks_its_expected_changed_line():
    for case in load_cases():
        parsed = parse_diff(build_case_diff(case))
        assert parsed, f"{case['id']} should produce a Python diff"
        assert any(item.path == case["path"] for item in parsed), case["id"]
        matching_file = next(item for item in parsed if item.path == case["path"])
        assert int(case["expected_line"]) in matching_file.changed_lines, case["id"]


def test_evaluation_diff_tracks_changed_line():
    case = next(item for item in load_cases() if item["id"] == "withdrawal-comparison-reversed")
    diff = build_case_diff(case)
    assert "+++ b/src/wallet.py" in diff
    assert "-    if amount > balance:" in diff
    assert "+    if amount < balance:" in diff
    changed = parse_diff(diff)
    assert changed[0].path == case["path"]
    assert case["expected_line"] in changed[0].changed_lines


def test_summary_calculates_recall_precision_and_clean_false_positive_rate():
    results = [
        {"kind": "bug", "expected_bug_detected": True, "true_positive_finding_count": 1, "false_positive_count": 0, "true_negative": None},
        {"kind": "bug", "expected_bug_detected": False, "false_positive_count": 0, "true_negative": None},
        {"kind": "clean", "expected_bug_detected": None, "false_positive_count": 1, "true_negative": False},
        {"kind": "clean", "expected_bug_detected": None, "false_positive_count": 0, "true_negative": True},
    ]
    metrics = summarize_results(results)
    assert metrics["bug_case_recall"] == 0.5
    assert metrics["candidate_precision"] == 0.5
    assert metrics["clean_case_false_positive_rate"] == 0.5


def test_evaluation_uses_validator_and_scores_fixture_without_api_call(tmp_path):
    from review_agent.evaluation import run_evaluation
    from review_agent.models import ReviewResponse

    case = next(item for item in load_cases() if item["id"] == "withdrawal-comparison-reversed")
    calls = []

    def review_call(api_key, model, diff_text, max_findings, draft_repros):
        calls.append((model, max_findings, draft_repros))
        assert "if amount < balance:" in diff_text
        return ReviewResponse(summary="One likely logic bug.", findings=[candidate()])

    def context_call(api_key, model, candidates, context_text):
        assert candidates[0]["finding_id"]
        assert "enclosing scope" in context_text
        return ContextReviewResponse(assessments=[ContextAssessment(
            finding_id=candidates[0]["finding_id"], decision="uncertain",
            reasoning="The limited context does not establish whether the claim is correct.",
            evidence_file="src/wallet.py",
            relevant_evidence="if amount < balance:",
        )])

    result = run_evaluation(
        "not-a-real-key", "test-model", [case], 3,
        review_call=review_call, context_call=context_call,
    )

    assert calls == [("test-model", 3, False)]
    assert result["baseline_metrics"]["true_positive_cases"] == 1
    assert result["baseline_cases"][0]["accepted_finding_count"] == 1
    assert result["metrics"]["true_positive_cases"] == 1
    assert result["metrics"]["bug_case_recall"] == 1.0
    assert result["cases"][0]["accepted_finding_count"] == 1
    assert result["cases"][0]["context_pass_removed_findings"] == 0
    assert result["cases"][0]["findings"][0]["context_decision"] == "uncertain"
    assert result["model_requests"] == 2


def test_context_provider_failure_keeps_first_pass_candidate_and_marks_check_incomplete(tmp_path):
    from review_agent.evaluation import run_evaluation, write_evaluation
    from review_agent.models import ReviewResponse

    case = next(item for item in load_cases() if item["id"] == "withdrawal-comparison-reversed")

    def review_call(api_key, model, diff_text, max_findings, draft_repros):
        return ReviewResponse(summary="Possible validation bug", findings=[candidate()])

    def context_call(api_key, model, candidates, context_text):
        raise RuntimeError("Gemini context-check request failed (ServerError, HTTP 500).")

    result = run_evaluation(
        "not-a-real-key", "test-model", [case], 3,
        review_call=review_call, context_call=context_call,
    )

    assert result["model_requests"] == 2
    assert result["context_requests_failed"] == 1
    assert result["context_comparison_complete"] is False
    assert result["cases"][0]["context_status"] == "failed"
    assert result["cases"][0]["accepted_finding_count"] == 1
    finding = result["cases"][0]["findings"][0]
    assert finding["context_decision"] == "uncertain"
    assert "not proven" in finding["verification_status"]

    json_path, markdown_path = write_evaluation(result, tmp_path / "evaluation.json")
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Context-check failures:** 1" in markdown
    assert "Context check:** failed" in markdown
    assert json_path.exists()


def test_initial_provider_failure_saves_partial_results_and_stops_calls(tmp_path):
    from review_agent.evaluation import run_evaluation, write_evaluation
    from review_agent.models import ReviewResponse

    cases = load_cases()[:3]
    calls = []

    def review_call(api_key, model, diff_text, max_findings, draft_repros):
        calls.append(diff_text)
        if len(calls) == 2:
            raise RuntimeError("Gemini review request failed (HTTP 429: rate limit or quota reached).")
        return ReviewResponse(summary="One candidate", findings=[candidate()])

    def context_call(*args, **kwargs):
        raise AssertionError("--no-context mode must not call the context provider")

    result = run_evaluation(
        "not-a-real-key", "test-model", cases, 3,
        review_call=review_call, context_call=context_call, use_context=False,
    )

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["model_requests"] == 2
    assert result["reviewed_cases"] == 1
    assert len(result["cases"]) == 1
    assert result["failure"]["stage"] == "initial_review"
    assert result["failure"]["case_id"] == cases[1]["id"]
    assert result["unprocessed_case_ids"] == [cases[1]["id"], cases[2]["id"]]

    json_path, markdown_path = write_evaluation(result, tmp_path / "evaluation.json")
    assert json_path.exists() and markdown_path.exists()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "INCOMPLETE" in markdown
    assert "HTTP 429" in markdown
    assert "Not run" in markdown
