"""Data models exchanged between the reviewer, context check and report writer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ConfigDict

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal[
    "logic",
    "input_validation",
    "security",
    "data_loss",
    "concurrency",
    "error_handling",
    "resource_usage",
    "compatibility",
    "other",
]
Priority = Literal["P1", "P2", "P3"]
ContextDecision = Literal["supported", "contradicted", "uncertain"]


class CandidateFinding(BaseModel):
    """A model-proposed issue. It is not accepted as correct just because it parses."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="Repository-relative path to the changed Python file.")
    line: int = Field(ge=1, description="Line number in the new version; must be added/changed.")
    severity: Severity
    category: Category
    title: str = Field(min_length=6, max_length=140)
    explanation: str = Field(min_length=20, max_length=1200)
    preconditions: str = Field(min_length=8, max_length=600)
    expected_behavior: str = Field(min_length=8, max_length=500)
    actual_behavior: str = Field(min_length=8, max_length=500)
    evidence_excerpt: str = Field(min_length=3, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0, description="Uncalibrated model self-estimate; not proof.")
    reproduction_hint: str = Field(default="", max_length=500)
    reproduction_test: str = Field(default="", max_length=5000, description="Draft only; never run automatically.")


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=1000)
    findings: list[CandidateFinding] = Field(default_factory=list, max_length=8)


class ContextAssessment(BaseModel):
    """Second-pass assessment using bounded source context; still not proof of a bug."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1, max_length=32)
    decision: ContextDecision
    reasoning: str = Field(min_length=8, max_length=800)
    evidence_file: str = Field(
        max_length=300,
        description="Repository-relative file path from the context block that contains the quoted evidence; empty if ungrounded.",
    )
    relevant_evidence: str = Field(
        max_length=1000,
        description="Verbatim excerpt from evidence_file; empty if the assessment is ungrounded.",
    )


class ContextReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[ContextAssessment] = Field(default_factory=list, max_length=8)


class AcceptedFinding(BaseModel):
    finding_id: str
    file: str
    line: int
    severity: Severity
    category: Category
    title: str
    explanation: str
    preconditions: str
    expected_behavior: str
    actual_behavior: str
    evidence_excerpt: str
    confidence: float
    priority: Priority
    context_decision: Literal["supported", "contradicted", "uncertain", "not_run"] = "not_run"
    context_notes: str = "Context review not run; static candidate only."
    verification_level: Literal["L1_STATIC"] = "L1_STATIC"
    verification_status: str = "Candidate only; deterministic checks passed, defect not proven"
    reproduction_hint: str = ""
    reproduction_test: str = ""

    def context_candidate_payload(self) -> dict[str, object]:
        """Return only the fields needed by the optional context-check request."""
        fields = {
            "finding_id", "file", "line", "severity", "category", "title",
            "explanation", "preconditions", "expected_behavior", "actual_behavior",
            "evidence_excerpt",
        }
        return self.model_dump(include=fields)


class RejectedFinding(BaseModel):
    title: str = "Unnamed finding"
    file: str = ""
    line: int | None = None
    reason: str


class CheckResult(BaseModel):
    status: Literal["not_run", "passed", "failed", "timed_out", "unavailable"] = "not_run"
    command: str = "python -m pytest -q"
    exit_code: int | None = None
    duration_seconds: float = 0.0
    output_excerpt: str = ""
    note: str = ""


class ReviewReport(BaseModel):
    schema_version: int = 1
    repository: str
    base_ref: str
    created_at: str
    model: str
    summary: str
    findings: list[AcceptedFinding]
    rejected_findings: list[RejectedFinding]
    repository_checks: CheckResult
    evidence_policy: dict[str, str]
    limitations: list[str]
    context_summary: str = ""
