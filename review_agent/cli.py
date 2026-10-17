"""CLI entry point for reviewing the current branch's Python diff."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
from pathlib import Path

from .checks import run_pytest_once
from .context import build_context_bundle
from .config import load_settings
from .evidence import apply_context_assessments, validate_findings
from .evaluation import load_cases, run_evaluation, write_evaluation
from .gemini_review import review_candidates_with_context, review_diff
from .git_diff import get_python_diff
from .models import CheckResult, ReviewReport
from .report import write_report

EVIDENCE_POLICY = {
    "Rejected": "Invalid path/line, evidence that does not match the source or cited changed line, duplicate candidate, or insufficiently concrete expected-versus-actual behavior.",
    "Static candidate": "Changed-line and source checks pass and a concrete failure scenario is supplied. This is plausible, not proven.",
    "Context checked": "A bounded second pass inspects the diff hunk, enclosing scope, imports, up to three call sites and up to two matching tests. Any suppressing contradiction must cite a specific context file and quote text present in that file's supplied block.",
    "Context limitation": "The second pass uses the same model; a supported finding is still a candidate, not a confirmed defect.",
    "Existing suite": "Run only after explicit confirmation. A pass does not prove a finding false; a failure is not automatically attributable to a candidate.",
    "Reproduction": "Generated tests are drafts and are never executed automatically. Inspect them and run only in an appropriate isolated environment.",
    "Review order": "P1/P2/P3 maps directly from model-estimated severity (critical/high, medium, low); severity and evidence strength remain separate.",
}

LIMITATIONS = [
    "This version reviews Python diffs and prioritizes issues on added/changed lines.",
    "Context expansion is deterministic and bounded: enclosing scopes first, then up to three caller examples and two matching tests. It stops looking for more callers once that limit is reached.",
    "The contextual pass uses the same model and is not an independent oracle. 'Supported' means no clear contradiction was found, not that the bug is proven.",
    "Severity is model-estimated. Review order is not a calibrated probability of correctness or impact.",
    "Repository tests are never executed by default. Running a project's tests executes its code; only do this for repositories you trust.",
    "Generated reproduction tests are drafts and are not sandboxed. Inspect them before running; never run them against untrusted code or with secrets exposed.",
    "Oversized diffs are rejected instead of being split into chunks. Narrow the comparison or raise REVIEW_MAX_DIFF_CHARS cautiously.",
    "An empty report does not prove the change is bug-free. Reviewers can miss defects, especially across languages or complex runtime behavior.",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-review-agent", description="Evidence-based review of a Python Git diff.")
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="Review the current branch against a base branch.")
    review.add_argument("--repo", default=".", help="Path to the Git repository (default: current directory).")
    review.add_argument("--base", default="main", help="Base branch/ref, e.g. main or origin/main.")
    review.add_argument("--output", default=".code-review-output", help="Directory for Markdown/JSON report.")
    review.add_argument("--run-tests", action="store_true", help="Ask for confirmation, then run the trusted repo's pytest suite once.")
    review.add_argument("--draft-repros", action="store_true", help="Ask Gemini for focused pytest drafts for only suitable findings. Drafts are not run.")

    evaluate = sub.add_parser("evaluate", help="Run a small labeled bug/clean diff set to estimate false positives and missed bugs.")
    evaluate.add_argument("--limit", type=int, default=4, help="Number of labeled cases to run (default: 4).")
    evaluate.add_argument("--no-context", action="store_true", help="Use one Gemini request per case; skip the optional context comparison.")
    evaluate.add_argument("--output", default=".code-review-output/evaluation.json", help="JSON output path; a Markdown report is also written.")
    return parser


def _run_evaluation(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
        cases = load_cases(args.limit)
        request_limit = len(cases) * (1 if args.no_context else 2)
        mode = "one-pass only" if args.no_context else "one pass plus at most one context request per candidate case"
        print(f"Evaluating {len(cases)} labeled cases with {settings.gemini_model} ({mode}).")
        print(f"Maximum provider requests for this run: {request_limit}; automatic SDK retries are disabled.")
        result = run_evaluation(
            settings.gemini_api_key,
            settings.gemini_model,
            cases,
            settings.max_findings,
            use_context=not args.no_context,
        )
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = (Path.cwd() / output).resolve()
        json_path, md_path = write_evaluation(result, output)
        baseline = result.get("baseline_metrics", {})
        metrics = result["metrics"]
        print(f"\nStatus: {result['status']}")
        print(
            f"One-pass baseline: {baseline.get('true_positive_cases', 0)}/"
            f"{baseline.get('bug_cases', 0)} bug cases detected; "
            f"{baseline.get('false_positive_findings', 0)} false-positive findings."
        )
        if not args.no_context:
            print(
                f"After context: {metrics['true_positive_cases']}/{metrics['bug_cases']} "
                f"bug cases detected; {metrics['false_positive_findings']} false-positive findings."
            )
        print(f"Provider requests attempted: {result.get('model_requests', 0)}")
        if result.get("failure"):
            failure = result["failure"]
            print(
                f"Stopped at {failure['case_id']} ({failure['stage']}): {failure['message']}",
                file=sys.stderr,
            )
            if result.get("unprocessed_case_ids"):
                print("Not run: " + ", ".join(result["unprocessed_case_ids"]), file=sys.stderr)
        print(f"Reports saved to:\n  {md_path}\n  {json_path}")
        print("Small labeled samples are sanity checks, not claims of general accuracy.")
        return 0 if result["complete"] else 1
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _run_review(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    output_dir = Path(args.output).expanduser()
    if not output_dir.is_absolute():
        output_dir = (repo / output_dir).resolve()

    try:
        settings = load_settings()
        diff_text, changed_files = get_python_diff(repo, args.base, max_chars=settings.max_diff_chars)
        if not diff_text.strip() or not changed_files:
            print("No changed Python lines found for this comparison. Try another --base or check your Git branch.")
            return 0

        print(f"Reviewing {len(changed_files)} changed Python file(s) against {args.base} with {settings.gemini_model}...")
        model_response = review_diff(
            settings.gemini_api_key, settings.gemini_model, diff_text,
            settings.max_findings, args.draft_repros,
        )
        findings, rejected = validate_findings(repo, changed_files, model_response.findings)
        context_bundle = build_context_bundle(
            repo, findings, changed_files=changed_files, max_chars=settings.max_context_chars
        )
        if context_bundle.text:
            budget_note = (
                "Some source was omitted due to a configured limit."
                if context_bundle.truncated
                else "No context budget limit was reached."
            )
            context_summary = (
                f"Retrieved {context_bundle.chars_used:,} characters across "
                f"{context_bundle.blocks_added} bounded context block(s); examined "
                f"{context_bundle.files_scanned} Python source file(s). "
                f"Budget: {settings.max_context_chars:,} characters. {budget_note}"
            )
        else:
            context_summary = "No contextual source was available; findings remain unverified candidates."

        if not findings:
            context_summary = "No candidate passed the initial deterministic checks, so no contextual model request was made."

        if findings and context_bundle.text:
            print(
                f"Context check: {context_bundle.blocks_added} bounded source block(s), "
                f"{context_bundle.chars_used:,} characters from up to {context_bundle.files_scanned} Python files scanned."
            )
            context_candidates = [finding.context_candidate_payload() for finding in findings]
            try:
                context_response = review_candidates_with_context(
                    settings.gemini_api_key, settings.gemini_model, context_candidates, context_bundle.text,
                )
                findings, context_rejected = apply_context_assessments(
                    findings, context_response, context_text=context_bundle.text
                )
                rejected.extend(context_rejected)
            except RuntimeError as exc:
                # Preserve first-pass candidates if the optional context request fails.
                print(f"Warning: context check unavailable: {exc}; retaining unverified candidates.", file=sys.stderr)
                context_summary += f" The contextual model request failed ({exc}); static candidates were retained as unverified."
                findings = [
                    finding.model_copy(update={
                        "context_decision": "uncertain",
                        "context_notes": "Context request failed; first-pass candidate remains unverified.",
                        "verification_status": "Context request failed; defect not proven",
                    })
                    for finding in findings
                ]
        elif findings:
            findings = [
                finding.model_copy(update={
                    "context_decision": "uncertain",
                    "context_notes": "No additional context could be retrieved within the current policy; static candidate remains unverified.",
                })
                for finding in findings
            ]

        checks = CheckResult(note="Not run. Use --run-tests to execute pytest once after explicit confirmation.")
        if args.run_tests:
            print("\nWARNING: this will execute code in the repository via pytest. Only continue for a repository you trust.")
            try:
                confirmation = input("Type RUN TESTS to continue: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirmation = ""
            if confirmation == "RUN TESTS":
                checks = run_pytest_once(repo)
            else:
                checks = CheckResult(note="Not run because explicit confirmation was not supplied.")

        report = ReviewReport(
            repository=str(repo),
            base_ref=args.base,
            created_at=datetime.now(timezone.utc).isoformat(),
            model=settings.gemini_model,
            summary=model_response.summary,
            findings=findings,
            rejected_findings=rejected,
            repository_checks=checks,
            evidence_policy=EVIDENCE_POLICY,
            limitations=LIMITATIONS,
            context_summary=context_summary,
        )
        json_path, md_path = write_report(report, output_dir, draft_repros=args.draft_repros)
        print(f"\nAccepted candidates: {len(findings)}")
        print(f"Rejected by evidence filters: {len(rejected)}")
        for finding in findings:
            print(
                f"{finding.priority} {finding.file}:{finding.line} {finding.title} "
                f"(context: {finding.context_decision}; not confirmed)"
            )
        print(f"\nReports written to:\n  {md_path}\n  {json_path}")
        if args.draft_repros:
            print("Any generated reproduction tests are drafts only; inspect before running.")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "evaluate":
        return _run_evaluation(args)
    if args.command == "review":
        return _run_review(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
