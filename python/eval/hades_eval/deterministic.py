"""Deterministic diagnostic path for the Hades evaluation harness.

This module does not invoke an LLM. It collects a fixed, read-only evidence
set and produces a structured DiagnosticReport from explicit templates.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from hades_agent.models import (
    ConfidenceAssessment,
    ConfidenceLevel,
    DiagnosticReport,
    Evidence,
    Hypothesis,
    IncidentContext,
)
from hades_agent.tools import DiagnosticToolbox, ToolExecution


@dataclass(frozen=True)
class DeterministicResult:
    report: DiagnosticReport
    tool_executions: tuple[ToolExecution, ...]
    duration_ms: float
    model_invocations: int = 0


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _metric_value(output: str | None, *path: str) -> Any:
    data = _json_loads(output).get("data", {})
    for part in path:
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _finding(tool: str, output: str | None) -> str:
    if tool == "get_system_metrics":
        cpu = _metric_value(output, "cpu", "overall_percent")
        memory = _metric_value(output, "memory", "available_bytes")
        total = _metric_value(output, "memory", "total_bytes")
        if cpu is not None and memory is not None and total:
            return (
                f"CPU utilization was {cpu}%; available memory was "
                f"{memory}/{total} bytes."
            )
        return "Collected a bounded system snapshot."
    if tool == "get_disk_usage":
        disks = _metric_value(output, "disks")
        if isinstance(disks, list) and disks:
            first = disks[0]
            return (
                f"Disk {first.get('mount_point')} had "
                f"{first.get('available_bytes')} bytes available."
            )
        return "Collected bounded disk usage."
    if tool == "list_top_processes":
        return "Collected the top processes by the requested sort key."
    if tool == "get_recent_incidents":
        incidents = _metric_value(output, "incidents")
        if isinstance(incidents, list):
            return f"Retrieved {len(incidents)} recent incident records."
        return "Retrieved recent incident records."
    return f"Executed {tool}."


def diagnose(
    *,
    question: str,
    incident: IncidentContext | None,
    toolbox: DiagnosticToolbox,
    historical: list[dict[str, Any]] | None = None,
) -> DeterministicResult:
    """Run the deterministic read-only diagnostic path."""
    started = time.perf_counter()
    executions: list[ToolExecution] = []
    calls = [
        ("get_system_metrics", {}),
        ("get_disk_usage", {}),
        ("list_top_processes", {"sort": "cpu", "limit": 5}),
        ("get_recent_incidents", {"limit": 5}),
    ]
    for name, arguments in calls:
        executions.append(toolbox.execute(name, arguments))

    successful = [item for item in executions if item.ok]
    evidence = [
        Evidence(tool=item.name, finding=_finding(item.name, item.output))
        for item in successful
    ]
    evidence_tools = [item.name for item in successful]

    kind = incident.kind if incident is not None else None
    if kind == "high_cpu":
        summary = "CPU utilization is high in the supplied incident and snapshot."
        hypotheses = [
            Hypothesis(
                cause="Sustained CPU saturation is present.",
                confidence=ConfidenceLevel.MEDIUM,
                rationale="The incident and snapshot report high CPU utilization.",
                supporting_evidence=[
                    i for i, tool in enumerate(evidence_tools) if tool == "get_system_metrics"
                ],
            )
        ]
        next_checks = [
            "Collect CPU samples over several minutes to confirm the condition is sustained.",
            "Compare the current CPU reading with prior incidents before attributing a cause.",
        ]
    elif kind == "memory_pressure":
        summary = "Available memory is low in the supplied incident and snapshot."
        hypotheses = [
            Hypothesis(
                cause="Memory availability is below the configured threshold.",
                confidence=ConfidenceLevel.MEDIUM,
                rationale="The incident and snapshot report low available memory.",
                supporting_evidence=[
                    i for i, tool in enumerate(evidence_tools) if tool == "get_system_metrics"
                ],
            )
        ]
        next_checks = [
            (
                "Collect repeated memory samples to distinguish a transient "
                "spike from sustained pressure."
            ),
            "Inspect the largest resident processes if process data becomes available.",
        ]
    elif kind == "low_disk_space":
        summary = "Available disk space is low for the affected mount."
        hypotheses = [
            Hypothesis(
                cause="Disk capacity is below the configured threshold.",
                confidence=ConfidenceLevel.MEDIUM,
                rationale="The incident and disk usage report low available space.",
                supporting_evidence=[
                    i for i, tool in enumerate(evidence_tools) if tool == "get_disk_usage"
                ],
            )
        ]
        next_checks = [
            "Identify large files or growth on the affected mount using read-only inspection.",
            "Track available space over time before deleting or moving data.",
        ]
    else:
        summary = "No threshold breach is evident in the collected snapshot."
        hypotheses = []
        next_checks = [
            "Collect another snapshot over time.",
            "Compare the current readings with configured thresholds.",
        ]

    if historical:
        next_checks.append(
            
                "Compare this reading with the retrieved historical incidents "
                "before drawing conclusions."
            
        )

    data_gaps = [
        "Only a bounded point-in-time snapshot was collected.",
        "No root cause is established by the available evidence.",
    ]
    if not historical:
        data_gaps.append("No historical comparison was available.")

    report = DiagnosticReport(
        summary=summary,
        evidence=evidence,
        likely_causes=hypotheses,
        confidence=ConfidenceAssessment(
            level=ConfidenceLevel.LOW if not hypotheses else ConfidenceLevel.MEDIUM,
            explanation=(
                "The diagnosis is based only on collected observations and configured "
                "thresholds; it is not a verified root cause."
            ),
        ),
        next_checks=next_checks,
        data_gaps=data_gaps,
    )
    return DeterministicResult(
        report=report,
        tool_executions=tuple(executions),
        duration_ms=(time.perf_counter() - started) * 1000,
        model_invocations=0,
    )
