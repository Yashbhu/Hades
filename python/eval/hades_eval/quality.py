"""Scoring helpers for the Hades evaluation harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from hades_agent.models import DiagnosticReport


@dataclass(frozen=True)
class Score:
    detection_correct: bool
    citation_valid: bool
    unsupported_claim_count: int
    contradiction_count: int
    useful_beyond_deterministic: bool
    notes: str


def _successful_tools(tool_executions: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for item in tool_executions:
        if item.get("ok") is not True:
            continue
        for key in ("tool", "name"):
            value = item.get(key)
            if isinstance(value, str):
                result.add(value)
    return result


def _report_text(report: DiagnosticReport) -> str:
    parts = [report.summary]
    parts.extend(item.finding for item in report.evidence)
    parts.extend(item.cause for item in report.likely_causes)
    parts.extend(item.rationale for item in report.likely_causes)
    return " ".join(parts).lower()


def score_report(
    *,
    scenario_name: str,
    expected_incident_kind: str | None,
    actual_incident_kind: str | None,
    report: DiagnosticReport,
    tool_executions: list[dict[str, Any]],
) -> Score:
    """Score a report using evidence-grounded, reproducible checks."""
    successful = _successful_tools(tool_executions)
    citation_valid = True
    for item in report.evidence:
        if item.tool not in successful:
            citation_valid = False
    for hypothesis in report.likely_causes:
        if not hypothesis.supporting_evidence:
            citation_valid = False
        for index in hypothesis.supporting_evidence:
            if index < 0 or index >= len(report.evidence):
                citation_valid = False

    unsupported = 0
    for hypothesis in report.likely_causes:
        if not hypothesis.supporting_evidence:
            unsupported += 1
            continue
        if any(
            index < 0 or index >= len(report.evidence)
            for index in hypothesis.supporting_evidence
        ):
            unsupported += 1

    text = _report_text(report)
    contradictions = 0
    if expected_incident_kind is None:
        if any(word in text for word in ("high", "pressure", "exhaustion", "critical")):
            contradictions += 1
    else:
        if any(word in text for word in ("stable", "normal", "no fault", "healthy")):
            contradictions += 1

    detection_correct = expected_incident_kind == actual_incident_kind

    # This flag is intentionally conservative. A report is only automatically
    # marked useful if it contains actionable next checks or data gaps in
    # addition to valid citations. Final usefulness still requires human review.
    useful = citation_valid and bool(report.next_checks or report.data_gaps)

    notes = (
        "Automated scoring checks citation validity and obvious contradictions only. "
        "It does not establish factual correctness or real-world usefulness."
    )
    return Score(
        detection_correct=detection_correct,
        citation_valid=citation_valid,
        unsupported_claim_count=unsupported,
        contradiction_count=contradictions,
        useful_beyond_deterministic=useful,
        notes=notes,
    )


def to_dict(score: Score) -> dict[str, Any]:
    return asdict(score)
