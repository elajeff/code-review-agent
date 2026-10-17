"""Deterministic checks for findings proposed by the language model."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from .git_diff import ChangedFile
from .models import AcceptedFinding, CandidateFinding, ContextReviewResponse, Priority, RejectedFinding
from .source import get_head_commit, read_reviewed_source


def assign_priority(finding: CandidateFinding) -> Priority:
    """Map the model-estimated severity to a review order, without scoring it."""
    if finding.severity in {"critical", "high"}:
        return "P1"
    if finding.severity == "medium":
        return "P2"
    return "P3"


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _safe_relative_path(raw: str) -> str | None:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    if PureWindowsPath(raw).drive:
        return None
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    parsed = PurePosixPath(normalized)
    if normalized.startswith("/") or parsed.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in parts):
        return None
    if parsed.suffix != ".py":
        return None
    return normalized


def validate_findings(
    repo: Path,
    changed_files: list[ChangedFile],
    candidates: list[CandidateFinding],
) -> tuple[list[AcceptedFinding], list[RejectedFinding]]:
    changed_by_path = {item.path: item for item in changed_files}
    accepted: list[AcceptedFinding] = []
    rejected: list[RejectedFinding] = []
    seen: set[tuple[str, int, str]] = set()
    head_commit = get_head_commit(repo)

    for candidate in candidates:
        path = _safe_relative_path(candidate.file)
        if path is None:
            rejected.append(_reject(candidate, "Path is not a safe repository-relative Python file."))
            continue
        changed = changed_by_path.get(path)
        if changed is None:
            rejected.append(_reject(candidate, "File is not a changed Python file in this diff."))
            continue
        if candidate.line not in changed.changed_lines:
            rejected.append(_reject(candidate, "Line is not an added/changed line in the new diff."))
            continue

        source_text = read_reviewed_source(repo, path, head_commit=head_commit)
        if source_text is None:
            rejected.append(_reject(candidate, "Referenced source file is unavailable, unsafe, too large, or not valid UTF-8."))
            continue
        lines = source_text.splitlines()
        if not 1 <= candidate.line <= len(lines):
            rejected.append(_reject(candidate, "Line number is outside the current file."))
            continue

        quote = _normalize(candidate.evidence_excerpt)
        cited_line = _normalize(lines[candidate.line - 1])
        nearby_lines = lines[max(0, candidate.line - 5):min(len(lines), candidate.line + 4)]
        nearby = _normalize("\n".join(nearby_lines))
        if not quote or quote not in nearby:
            rejected.append(_reject(candidate, "Evidence excerpt does not match source near the cited line."))
            continue
        includes_cited_line = bool(cited_line) and cited_line in quote
        # Very long generated lines cannot fit in the evidence field. For these only,
        # accept a distinctive quote taken from that exact line.
        long_line_excerpt = (
            len(cited_line) > 450
            and len(quote) >= 40
            and quote in cited_line
        )
        if not includes_cited_line and not long_line_excerpt:
            rejected.append(_reject(candidate, "Evidence excerpt must include the cited changed line (or a distinctive excerpt from a very long line)."))
            continue
        if not _has_concrete_behavior(candidate.preconditions, candidate.expected_behavior, candidate.actual_behavior):
            rejected.append(_reject(candidate, "Finding lacks a concrete condition and expected-versus-actual behavior."))
            continue

        normalized_title = re.sub(r"\W+", " ", candidate.title.casefold()).strip()
        duplicate_key = (path.casefold(), candidate.line, normalized_title)
        if duplicate_key in seen:
            rejected.append(_reject(candidate, "Duplicate of an earlier finding at the same location."))
            continue
        seen.add(duplicate_key)

        stable = hashlib.sha256(f"{path}:{candidate.line}:{normalized_title}".encode("utf-8")).hexdigest()[:10]
        accepted.append(AcceptedFinding(
            finding_id=stable,
            file=path,
            line=candidate.line,
            severity=candidate.severity,
            category=candidate.category,
            title=candidate.title.strip(),
            explanation=candidate.explanation.strip(),
            preconditions=candidate.preconditions.strip(),
            expected_behavior=candidate.expected_behavior.strip(),
            actual_behavior=candidate.actual_behavior.strip(),
            evidence_excerpt=candidate.evidence_excerpt.strip(),
            confidence=candidate.confidence,
            priority=assign_priority(candidate),
            context_decision="not_run",
            context_notes="Context review not run yet; static candidate only.",
            verification_level="L1_STATIC",
            verification_status="Candidate only; deterministic source checks passed, defect not proven",
            reproduction_hint=candidate.reproduction_hint.strip(),
            reproduction_test=candidate.reproduction_test.strip(),
        ))

    accepted.sort(key=lambda item: (item.priority, item.file, item.line))
    return accepted, rejected


def _has_concrete_behavior(preconditions: str, expected: str, actual: str) -> bool:
    values = [_normalize(value) for value in (preconditions, expected, actual)]
    if any(len(value) < 8 for value in values):
        return False
    # This only rejects repeated boilerplate; semantic strength is reviewed by a human.
    return len({value.casefold() for value in values}) == 3


def _normalize_context_text(text: str) -> str:
    """Remove display line numbers and diff markers before checking quoted evidence."""
    cleaned: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^\s*\d+:\s?", "", line)
        if line[:1] in {"+", "-", " "}:
            line = line[1:]
        cleaned.append(line)
    return _normalize("\n".join(cleaned))


_CONTEXT_BLOCK_HEADER = re.compile(r"^\[[^:\]]+: (.+):(\d+)-(\d+)\]$")


def _context_for_file(context_text: str, evidence_file: str) -> str:
    """Return only blocks labeled with the file cited by the context assessment."""
    if not evidence_file:
        return ""
    matching_lines: list[str] = []
    current_file: str | None = None
    for line in context_text.splitlines():
        header = _CONTEXT_BLOCK_HEADER.match(line)
        if header:
            current_file = header.group(1)
            continue
        if current_file == evidence_file:
            matching_lines.append(line)
    return "\n".join(matching_lines)


def _context_quote_is_present(quote: str, context_text: str, evidence_file: str) -> bool:
    normalized_quote = _normalize(quote)
    file_context = _context_for_file(context_text, evidence_file)
    if not normalized_quote or not file_context:
        return False
    return normalized_quote in _normalize_context_text(file_context)


def apply_context_assessments(
    findings: list[AcceptedFinding],
    assessments: ContextReviewResponse,
    context_text: str = "",
) -> tuple[list[AcceptedFinding], list[RejectedFinding]]:
    """Apply contextual assessments only when their quoted evidence is grounded.

    The same model can offer a useful challenge to a finding, but it is not an
    independent oracle. An ungrounded contradiction must not suppress a candidate.
    """
    by_id = {item.finding_id: item for item in assessments.assessments}
    accepted: list[AcceptedFinding] = []
    rejected: list[RejectedFinding] = []

    for finding in findings:
        assessment = by_id.get(finding.finding_id)
        if assessment is None:
            accepted.append(finding.model_copy(update={
                "context_decision": "uncertain",
                "context_notes": "No matching contextual assessment was returned; finding remains an unverified candidate.",
                "verification_status": "Context assessment missing; candidate not proven",
            }))
            continue

        if not _context_quote_is_present(
            assessment.relevant_evidence, context_text, assessment.evidence_file
        ):
            accepted.append(finding.model_copy(update={
                "context_decision": "uncertain",
                "context_notes": "Context assessment did not provide a verifiable excerpt from the supplied context; candidate retained.",
                "verification_status": "Context evidence could not be verified; candidate not proven",
            }))
            continue

        if assessment.decision == "contradicted":
            rejected.append(RejectedFinding(
                title=finding.title,
                file=finding.file,
                line=finding.line,
                reason=f"Supplied context evidence contradicts the candidate: {assessment.reasoning}",
            ))
            continue

        if assessment.decision == "supported":
            note = "The context pass found no clear contradiction; this remains an unproven candidate."
            status = "Context checked; defect not proven"
        else:
            note = assessment.reasoning
            status = "Context remains ambiguous; candidate is unverified"

        accepted.append(finding.model_copy(update={
            "context_decision": assessment.decision,
            "context_notes": note,
            "verification_status": status,
        }))

    return accepted, rejected


def _reject(candidate: CandidateFinding, reason: str) -> RejectedFinding:
    return RejectedFinding(title=candidate.title, file=candidate.file, line=candidate.line, reason=reason)
