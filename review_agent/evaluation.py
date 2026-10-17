"""Small, opt-in evaluation set for comparing review findings against labels.

Each case uses one initial provider request and optionally one batched context
request. Partial results are retained if a provider fails, especially on quota errors.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from .context import ContextBundle, build_context_bundle
from .evidence import apply_context_assessments, validate_findings
from .git_diff import parse_diff
from .gemini_review import review_candidates_with_context, review_diff
from .models import ContextReviewResponse, ReviewResponse

CASE_FILE = Path(__file__).resolve().parent / "data" / "evaluation_cases.json"


def load_cases(limit: int | None = None) -> list[dict[str, Any]]:
    try:
        cases = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load evaluation cases: {type(exc).__name__}.") from exc
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("Evaluation case file must contain a non-empty list.")
    if limit is not None and (limit < 1 or limit > len(cases)):
        raise ValueError(f"--limit must be between 1 and {len(cases)}.")
    return cases if limit is None else cases[:limit]


def build_case_diff(case: dict[str, Any]) -> str:
    lines = difflib.unified_diff(
        str(case["before"]).splitlines(),
        str(case["after"]).splitlines(),
        fromfile=f"a/{case['path']}",
        tofile=f"b/{case['path']}",
        n=3,
        lineterm="",
    )
    return "\n".join(lines) + "\n"


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def finding_matches_label(finding, case: dict[str, Any]) -> bool:
    """Apply simple human-written labels; this is not an independent judge."""
    if finding.line != int(case["expected_line"]):
        return False
    if finding.category not in case.get("expected_categories", []):
        return False
    combined = _normal(" ".join([
        finding.title,
        finding.explanation,
        finding.preconditions,
        finding.expected_behavior,
        finding.actual_behavior,
    ]))
    required = [_normal(term) for term in case.get("required_terms", [])]
    alternatives = [_normal(term) for term in case.get("any_terms", [])]
    return all(term in combined for term in required) and (
        not alternatives or any(term in combined for term in alternatives)
    )


def score_case(case: dict[str, Any], accepted: list[Any]) -> dict[str, Any]:
    bug_expected = case["kind"] == "bug"
    matching = [finding for finding in accepted if finding_matches_label(finding, case)] if bug_expected else []
    detected = bool(matching)
    # Count at most one finding per labeled defect as a true positive. Additional
    # claims still count against precision until deduplicated or manually reviewed.
    false_positives = len(accepted) - (1 if detected else 0)
    return {
        "id": case["id"],
        "kind": case["kind"],
        "expected_bug_detected": detected if bug_expected else None,
        "accepted_finding_count": len(accepted),
        "matching_finding_id": matching[0].finding_id if matching else None,
        "true_positive_finding_count": 1 if matching else 0,
        "false_positive_count": false_positives if bug_expected else len(accepted),
        "true_negative": (not accepted) if not bug_expected else None,
        "findings": [finding.model_dump(mode="json") for finding in accepted],
    }


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    bugs = [item for item in results if item["kind"] == "bug"]
    clean = [item for item in results if item["kind"] == "clean"]
    true_positive_cases = sum(bool(item["expected_bug_detected"]) for item in bugs)
    false_negative_cases = len(bugs) - true_positive_cases
    true_positive_findings = sum(int(item.get("true_positive_finding_count", 0)) for item in results)
    false_positive_findings = sum(int(item["false_positive_count"]) for item in results)
    true_negative_cases = sum(bool(item["true_negative"]) for item in clean)
    clean_fp_cases = sum(not bool(item["true_negative"]) for item in clean)
    precision_denominator = true_positive_findings + false_positive_findings
    return {
        "cases_run": len(results),
        "bug_cases": len(bugs),
        "clean_cases": len(clean),
        "true_positive_cases": true_positive_cases,
        "true_positive_findings": true_positive_findings,
        "false_negative_cases": false_negative_cases,
        "false_positive_findings": false_positive_findings,
        "true_negative_cases": true_negative_cases,
        "clean_case_false_positive_rate": clean_fp_cases / len(clean) if clean else None,
        "candidate_precision": true_positive_findings / precision_denominator if precision_denominator else None,
        "bug_case_recall": true_positive_cases / len(bugs) if bugs else None,
        "interpretation": (
            "Small development benchmark only. Metrics depend on the human-written labels and simple matcher; "
            "manually inspect findings. These results do not establish general review accuracy."
        ),
    }


def run_evaluation(
    api_key: str,
    model: str,
    cases: list[dict[str, Any]],
    max_findings: int,
    review_call: Callable[..., ReviewResponse] = review_diff,
    context_call: Callable[..., ContextReviewResponse] = review_candidates_with_context,
    use_context: bool = True,
) -> dict[str, Any]:
    """Run cases sequentially; on provider failure, retain completed work and stop."""
    results: list[dict[str, Any]] = []
    baseline_results: list[dict[str, Any]] = []
    model_requests = 0
    context_requests_failed = 0
    failure: dict[str, str] | None = None

    for case in cases:
        diff_text = build_case_diff(case)
        changed_files = parse_diff(diff_text)
        if not changed_files:
            raise RuntimeError(f"Evaluation case '{case['id']}' did not produce a parsed diff.")

        with tempfile.TemporaryDirectory(prefix="code-review-eval-") as temp:
            repo = Path(temp)
            after_path = repo / str(case["path"])
            after_path.parent.mkdir(parents=True, exist_ok=True)
            after_path.write_text(str(case["after"]), encoding="utf-8")

            model_requests += 1
            try:
                response = review_call(api_key, model, diff_text, max_findings, False)
            except RuntimeError as exc:
                failure = {"case_id": str(case["id"]), "stage": "initial_review", "message": str(exc)}
                break

            accepted, rejected = validate_findings(repo, changed_files, response.findings)
            baseline_case = score_case(case, accepted)
            baseline_results.append(baseline_case)

            bundle = ContextBundle("", 0, 0, False, 0)
            context_status = "disabled" if not use_context else "not_needed"
            context_rejected = []
            stop_after_case = False

            if use_context and accepted:
                bundle = build_context_bundle(repo, accepted, changed_files=changed_files)
                if bundle.text:
                    model_requests += 1
                    try:
                        context_candidates = [finding.context_candidate_payload() for finding in accepted]
                        context_result = context_call(api_key, model, context_candidates, bundle.text)
                        context_status = "completed"
                        accepted, context_rejected = apply_context_assessments(
                            accepted, context_result, context_text=bundle.text
                        )
                    except RuntimeError as exc:
                        # Keep first-pass findings, make the incomplete check explicit, and
                        # stop rather than issuing more requests after a likely quota error.
                        context_status = "failed"
                        context_requests_failed += 1
                        failure = {"case_id": str(case["id"]), "stage": "context_check", "message": str(exc)}
                        accepted = [finding.model_copy(update={
                            "context_decision": "uncertain",
                            "context_notes": "Context check failed; initial candidate retained, unverified.",
                            "verification_status": "Context check failed; defect not proven",
                        }) for finding in accepted]
                        stop_after_case = True
                else:
                    context_status = "unavailable"

            evaluated_case = score_case(case, accepted)
            evaluated_case["context_status"] = context_status
            evaluated_case["one_pass_accepted_finding_count"] = baseline_case["accepted_finding_count"]
            evaluated_case["context_pass_removed_findings"] = max(
                0, baseline_case["accepted_finding_count"] - evaluated_case["accepted_finding_count"]
            )
            evaluated_case["context_chars"] = bundle.chars_used
            evaluated_case["context_blocks"] = bundle.blocks_added
            evaluated_case["rejected_candidate_count"] = len(rejected) + len(context_rejected)
            evaluated_case["rejected_reasons"] = [item.reason for item in rejected + context_rejected]
            results.append(evaluated_case)

            if stop_after_case:
                break

    complete = failure is None and len(results) == len(cases)
    unprocessed_ids: list[str] = []
    if failure is not None:
        failed_index = next((i for i, item in enumerate(cases) if str(item["id"]) == failure["case_id"]), len(cases))
        next_index = failed_index + (1 if failure["stage"] == "context_check" else 0)
        unprocessed_ids = [str(item["id"]) for item in cases[next_index:]]

    return {
        "model": model,
        "status": "completed" if complete else "incomplete",
        "complete": complete,
        "requested_cases": len(cases),
        "reviewed_cases": len(baseline_results),
        "unprocessed_case_ids": unprocessed_ids,
        "failure": failure,
        "model_requests": model_requests,
        "context_enabled": use_context,
        "context_requests_failed": context_requests_failed,
        "context_comparison_complete": bool(use_context and complete and context_requests_failed == 0),
        "cases": results,
        "baseline_cases": baseline_results,
        "baseline_metrics": summarize_results(baseline_results),
        "metrics": summarize_results(results),
    }


def write_evaluation(result: dict[str, Any], output_path: Path) -> tuple[Path, Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path = output_path.with_suffix(".md")
    metrics = result["metrics"]
    status_note = "completed" if result.get("complete", True) else "INCOMPLETE — partial results saved"
    lines = [
        "# Code Review Agent evaluation", "",
        f"- **Status:** {status_note}",
        f"- **Model:** `{result['model']}`",
        f"- **Cases requested / reviewed:** {result.get('requested_cases', metrics['cases_run'])} / {result.get('reviewed_cases', metrics['cases_run'])}",
        f"- **Provider requests attempted:** {result.get('model_requests', 'not recorded')} (SDK automatic retries disabled)",
        f"- **Context enabled:** {'yes' if result.get('context_enabled', True) else 'no'}",
        f"- **Context-check failures:** {result.get('context_requests_failed', 0)}",
        f"- **Context comparison complete:** {'yes' if result.get('context_comparison_complete', True) else 'no'}",
        f"- **Detected bug cases:** {metrics['true_positive_cases']}/{metrics['bug_cases']}",
        f"- **Matched findings:** {metrics['true_positive_findings']}",
        f"- **Missed bug cases:** {metrics['false_negative_cases']}",
        f"- **False-positive findings:** {metrics['false_positive_findings']}",
        f"- **Clean-case false-positive rate:** {_format_percent(metrics['clean_case_false_positive_rate'])}",
        f"- **Candidate precision:** {_format_percent(metrics['candidate_precision'])}",
        f"- **Bug-case recall:** {_format_percent(metrics['bug_case_recall'])}",
        "", f"> {metrics['interpretation']}", "",
        "## One-pass baseline vs. context pass", "",
        f"- **One-pass recall:** {_format_percent(result.get('baseline_metrics', {}).get('bug_case_recall'))}",
        f"- **Context-pass recall:** {_format_percent(metrics['bug_case_recall'])}",
        f"- **One-pass precision:** {_format_percent(result.get('baseline_metrics', {}).get('candidate_precision'))}",
        f"- **Context-pass precision:** {_format_percent(metrics['candidate_precision'])}",
        f"- **One-pass clean-case false-positive rate:** {_format_percent(result.get('baseline_metrics', {}).get('clean_case_false_positive_rate'))}",
        f"- **Context-pass clean-case false-positive rate:** {_format_percent(metrics['clean_case_false_positive_rate'])}",
        "", "Both measurements use the same initial model findings. The context pass is optional and is not independent proof.",
    ]

    failure = result.get("failure")
    if failure:
        lines.extend([
            "", "## Why the evaluation stopped", "",
            f"- **Case:** `{failure['case_id']}`",
            f"- **Stage:** {failure['stage']}",
            f"- **Provider error:** {failure['message']}",
            "- Completed cases and their results were retained. No later cases were requested.",
        ])
    if result.get("unprocessed_case_ids"):
        pending = ", ".join(f"`{item}`" for item in result["unprocessed_case_ids"])
        lines.extend(["", f"**Not run:** {pending}"])

    lines.extend(["", "## Case results", ""])
    for item in result["cases"]:
        if item["kind"] == "bug":
            label = "DETECTED" if item["expected_bug_detected"] else "MISSED"
        else:
            label = "CLEAN" if item["true_negative"] else "FALSE POSITIVE"
        lines.extend([
            f"### {item['id']} — {label}", "",
            f"- **Case type:** {item['kind']}",
            f"- **One-pass accepted findings:** {item.get('one_pass_accepted_finding_count', item['accepted_finding_count'])}",
            f"- **After-context accepted findings:** {item['accepted_finding_count']}",
            f"- **Context-pass removed findings:** {item.get('context_pass_removed_findings', 0)}",
            f"- **Rejected candidates:** {item.get('rejected_candidate_count', 0)}",
            f"- **Context check:** {item.get('context_status', 'not recorded')}",
        ])
        for finding in item["findings"]:
            lines.append(f"- **{finding['priority']} `{finding['file']}:{finding['line']}`:** {finding['title']}")
        for reason in item.get("rejected_reasons", []):
            lines.append(f"- Rejected candidate: {reason}")
        lines.append("")

    lines.extend([
        "## Limitations", "",
        "- This is a small, human-labeled development fixture set, not a general accuracy benchmark.",
        "- Label matching is deliberately simple; inspect the findings and labels manually.",
        "- If the provider fails, the report is marked incomplete and saved with results from completed cases.",
        "- Each provider request is attempted once; retrying an evaluation is a user decision after checking quota/status.",
        "- The context pass uses the same model; agreement is not independent proof.",
    ])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path, markdown_path


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"
