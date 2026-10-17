"""Save a review as JSON and human-readable Markdown."""

from __future__ import annotations

from pathlib import Path

from .models import ReviewReport


def write_report(report: ReviewReport, output_dir: Path, draft_repros: bool = False) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "review.json"
    md_path = output_dir / "review.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    lines = [
        "# Code review report", "", f"- **Repository:** `{report.repository}`",
        f"- **Base:** `{report.base_ref}`", f"- **Model:** `{report.model}`",
        f"- **Generated:** {report.created_at}", "", "## Summary", "",
        report.summary or "No summary provided.", "",
        "## Context retrieved", "",
        report.context_summary or "No context retrieval summary was recorded.", "",
        f"## Findings ({len(report.findings)})", "",
    ]
    if not report.findings:
        lines.append("No findings passed the deterministic evidence checks. This does not prove the change is bug-free.")
    for index, finding in enumerate(report.findings, 1):
        lines.extend([
            f"### {index}. [{finding.priority}] {finding.title}", "",
            f"- **Location:** `{finding.file}:{finding.line}`",
            f"- **Model-estimated severity:** {finding.severity} · **Review order:** {finding.priority} · **Category:** {finding.category}",
            f"- **Static evidence:** {finding.verification_level} — {finding.verification_status}",
            f"- **Context assessment:** {finding.context_decision} — {finding.context_notes}",
            f"- **Model confidence:** {finding.confidence:.0%} (self-estimate, not proof; not used as a calibrated probability)", "",
            finding.explanation, "", f"**When it occurs:** {finding.preconditions}",
            f"**Expected:** {finding.expected_behavior}", f"**Actual:** {finding.actual_behavior}",
            "", "Evidence excerpt:", "", "```python", finding.evidence_excerpt, "```", "",
        ])
        if finding.reproduction_hint:
            lines.extend([f"**Suggested reproduction:** {finding.reproduction_hint}", ""])
        if draft_repros and finding.reproduction_test.strip():
            repro_dir = output_dir / "reproductions"
            repro_dir.mkdir(parents=True, exist_ok=True)
            repro_path = repro_dir / f"test_{finding.finding_id}.py"
            banner = (
                '"""AI-generated reproduction draft; review before running. Not a security sandbox."""\n'
                "# Only use this test on a repository you trust. Review imports and side effects first.\n\n"
            )
            repro_path.write_text(banner + finding.reproduction_test.strip() + "\n", encoding="utf-8")
            lines.extend([f"**Reproduction test draft:** `{repro_path.relative_to(output_dir).as_posix()}` (NOT RUN)", ""])
    lines.extend(["## Repository test run", "", f"- **Status:** {report.repository_checks.status}",
                  f"- **Duration:** {report.repository_checks.duration_seconds:.2f}s", f"- **Note:** {report.repository_checks.note}", ""])
    if report.repository_checks.output_excerpt:
        lines.extend(["```text", report.repository_checks.output_excerpt, "```", ""])
    lines.extend(["## Evidence policy", ""])
    for level, meaning in report.evidence_policy.items():
        lines.append(f"- **{level}:** {meaning}")
    lines.extend(["", "## Limitations", ""])
    lines.extend([f"- {item}" for item in report.limitations])
    lines.extend(["", "## Rejected candidates", ""])
    if report.rejected_findings:
        for rejected in report.rejected_findings:
            lines.append(f"- `{rejected.file}:{rejected.line or '?'}` **{rejected.title}** — {rejected.reason}")
    else:
        lines.append("None.")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
