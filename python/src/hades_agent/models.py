"""Validated report, incident, and trace models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import IncidentInputError


class ConfidenceLevel(str, Enum):
    """Qualitative confidence, never a fabricated numeric probability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Evidence(BaseModel):
    """An observation attributed to a successful diagnostic tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(
        min_length=1,
        description="Exact name of the tool that returned this observation.",
    )
    finding: str = Field(
        min_length=1,
        description="A factual finding taken only from that tool's output.",
    )


class Hypothesis(BaseModel):
    """A ranked, explicitly uncertain explanation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: str = Field(min_length=1, description="A possible cause, not a fact.")
    confidence: ConfidenceLevel
    rationale: str = Field(
        min_length=1,
        description="How the cited evidence supports or weakens this hypothesis.",
    )
    supporting_evidence: list[int] = Field(
        default_factory=list,
        description="Zero-based indices into the evidence list.",
    )


class ConfidenceAssessment(BaseModel):
    """Overall confidence with an explanation of uncertainty."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: ConfidenceLevel
    explanation: str = Field(min_length=1)


class DiagnosticReport(BaseModel):
    """Structured, evidence-backed diagnosis returned by the agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(
        min_length=1,
        description="What appears to be happening, based only on observations.",
    )
    evidence: list[Evidence] = Field(
        default_factory=list,
        description="Observed facts. Must not contain guesses or inferred causes.",
    )
    likely_causes: list[Hypothesis] = Field(
        default_factory=list,
        description="Ranked hypotheses. Empty when the evidence is insufficient.",
    )
    confidence: ConfidenceAssessment
    next_checks: list[str] = Field(
        default_factory=list,
        description="Additional read-only checks that could clarify the diagnosis.",
    )
    data_gaps: list[str] = Field(
        default_factory=list,
        description="Information the investigation could not obtain.",
    )


class IncidentContext(BaseModel):
    """Incident data accepted from a JSON event, a file, or the bridge."""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    state: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    started_at: str | None = None
    updated_at: str | None = None
    last_observed_at: str | None = None
    recovered_at: str | None = None
    subject: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> IncidentContext:
        """Accept an incident object or an event envelope containing one."""
        if not isinstance(payload, dict):
            raise IncidentInputError("incident JSON must be an object")
        candidate = payload.get("incident", payload)
        if not isinstance(candidate, dict):
            raise IncidentInputError("event.incident must be an object")
        try:
            return cls.model_validate(candidate)
        except ValidationError as error:
            raise IncidentInputError(f"invalid incident event: {error}") from error


@dataclass(frozen=True)
class ToolExecution:
    """One attempted tool call, recorded independently of the model's prose."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    output: str | None
    error: dict[str, Any] | None
    duration_ms: float
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class InvestigationResult:
    """Validated report plus the factual record of tool calls."""

    question: str
    incident: IncidentContext | None
    report: DiagnosticReport
    tool_calls: tuple[ToolExecution, ...]
    model: str
    iterations: int
    budget_exhausted: bool
    iteration_limit_reached: bool = False
    limits: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "incident": self.incident.model_dump(mode="json") if self.incident else None,
            "report": self.report.model_dump(mode="json"),
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "model": self.model,
            "iterations": self.iterations,
            "budget_exhausted": self.budget_exhausted,
            "iteration_limit_reached": self.iteration_limit_reached,
            "limits": self.limits,
        }
