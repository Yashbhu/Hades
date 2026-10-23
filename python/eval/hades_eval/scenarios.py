"""Synthetic diagnostic scenarios for the Hades evaluation harness.

These scenarios are grounded in the M2 incident and metric shapes that Hades
actually collects. They describe observable symptoms only; they are not claims
about root causes.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from hades_agent.models import IncidentContext
from hades_client import CpuInfo, DiskInfo, DiskUsage, MemoryInfo, SystemMetrics


@dataclass(frozen=True)
class Scenario:
    """A controlled diagnostic case."""

    name: str
    question: str
    incident: IncidentContext | None
    expected_incident_kind: str | None
    expected_primary_metric: str | None
    notes: str


def _incident(
    incident_id: str,
    kind: str,
    summary: str,
    evidence: dict[str, Any],
    *,
    subject: str | None = None,
) -> IncidentContext:
    return IncidentContext(
        id=incident_id,
        kind=kind,
        severity="warning",
        state="active",
        subject=subject,
        summary=summary,
        started_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        last_observed_at="2026-01-01T00:00:00Z",
        evidence=evidence,
    )


def normal_metrics() -> SystemMetrics:
    return SystemMetrics(
        schema_version=1,
        collected_at="2026-01-01T00:00:00Z",
        cpu=CpuInfo(
            overall_percent=12.5,
            per_core_percent=(10.0, 15.0),
            logical_core_count=2,
            sampling_interval_ms=200,
        ),
        memory=MemoryInfo(
            total_bytes=1000,
            used_bytes=500,
            available_bytes=500,
            free_bytes=100,
            used_percent=50.0,
        ),
        disks=(
            DiskInfo(
                mount_point="/",
                file_system="apfs",
                total_bytes=1000,
                available_bytes=200,
                used_bytes=800,
                used_percent=80.0,
            ),
        ),
        processes=None,
        issues=(),
    )


def high_cpu_metrics() -> SystemMetrics:
    metrics = normal_metrics()
    return dataclasses.replace(
        metrics,
        cpu=CpuInfo(
            overall_percent=95.0,
            per_core_percent=(94.0, 96.0),
            logical_core_count=2,
            sampling_interval_ms=200,
        ),
    )


def memory_pressure_metrics() -> SystemMetrics:
    metrics = normal_metrics()
    return dataclasses.replace(
        metrics,
        memory=MemoryInfo(
            total_bytes=1000,
            used_bytes=950,
            available_bytes=50,
            free_bytes=10,
            used_percent=95.0,
        ),
    )


def low_disk_metrics() -> SystemMetrics:
    metrics = normal_metrics()
    return dataclasses.replace(
        metrics,
        disks=(
            DiskInfo(
                mount_point="/",
                file_system="apfs",
                total_bytes=1000,
                available_bytes=10,
                used_bytes=990,
                used_percent=99.0,
            ),
        ),
    )


def disk_usage_for(metrics: SystemMetrics) -> DiskUsage:
    return DiskUsage(disks=metrics.disks, issues=metrics.issues)


SCENARIOS: list[Scenario] = [
    Scenario(
        name="normal_operation",
        question="Is the system healthy?",
        incident=None,
        expected_incident_kind=None,
        expected_primary_metric="cpu",
        notes="No threshold breach is expected in the supplied snapshot.",
    ),
    Scenario(
        name="high_cpu_sustained",
        question="Why is CPU usage high?",
        incident=_incident(
            "scenario-high-cpu",
            "high_cpu",
            "CPU utilization is sustained above the configured threshold.",
            {
                "kind": "high_cpu",
                "observed": {"latest_percent": 95.0},
                "derived": {"recent_average_percent": 94.0},
            },
        ),
        expected_incident_kind="high_cpu",
        expected_primary_metric="cpu_percent",
        notes=(
                "The scenario states a sustained high-CPU symptom; "
                "it does not identify a process or root cause."
            ),
    ),
    Scenario(
        name="memory_pressure",
        question="Why is available memory low?",
        incident=_incident(
            "scenario-memory-pressure",
            "memory_pressure",
            "Available memory is below the configured threshold.",
            {
                "kind": "memory_pressure",
                "observed": {
                    "latest_used_percent": 95.0,
                    "latest_available_percent": 5.0,
                },
                "derived": {"recent_average_available_percent": 5.0},
            },
            subject="memory",
        ),
        expected_incident_kind="memory_pressure",
        expected_primary_metric="memory_available_percent",
        notes=(
                "The scenario states a memory-availability symptom; "
                "it does not identify a leaking process."
            ),
    ),
    Scenario(
        name="low_disk_space",
        question="Why is disk space low?",
        incident=_incident(
            "scenario-low-disk",
            "low_disk_space",
            "Available disk space is below the configured threshold.",
            {
                "kind": "low_disk_space",
                "observed": {
                    "mount_point": "/",
                    "available_percent": 1.0,
                },
            },
            subject="/",
        ),
        expected_incident_kind="low_disk_space",
        expected_primary_metric="disk_available_percent",
        notes=(
                "The scenario states a disk-capacity symptom for one mount; "
                "it does not identify files or a root cause."
            ),
    ),
]


def metrics_for_scenario(name: str) -> SystemMetrics:
    if name == "high_cpu_sustained":
        return high_cpu_metrics()
    if name == "memory_pressure":
        return memory_pressure_metrics()
    if name == "low_disk_space":
        return low_disk_metrics()
    return normal_metrics()


def seed_history(store: Any) -> None:
    """Seed one prior incident without making it current evidence."""
    from hades_agent.models import IncidentContext

    history_incident = IncidentContext(
        id="history-high-cpu",
        kind="high_cpu",
        severity="warning",
        state="recovered",
        subject=None,
        summary="Prior high CPU incident.",
        started_at="2025-12-01T00:00:00Z",
        updated_at="2025-12-01T00:00:00Z",
        last_observed_at="2025-12-01T00:00:00Z",
        evidence={
            "kind": "high_cpu",
            "observed": {"latest_percent": 90.0},
            "derived": {"recent_average_percent": 89.0},
        },
    )
    store.upsert_incident(history_incident)
    store.record_investigation(
        "inv-history",
        "run-history",
        incident_id="history-high-cpu",
        question="prior question",
        model="local",
        stage="terminated",
    )
    from agent_fakes import sample_report

    store.save_outcome(
        investigation_id="inv-history",
        execution_id="run-history",
        incident_id="history-high-cpu",
        question="prior question",
        model="local",
        stage="terminated",
        termination_status="completed",
        report=sample_report(),
        evidence=[
            {
                "evidence_id": "ev-history",
                "call_id": "call-history",
                "tool": "get_system_metrics",
                "arguments": {},
                "summary": "Prior CPU 90%",
                "collected_at": "2025-12-01T00:00:01Z",
            }
        ],
    )
