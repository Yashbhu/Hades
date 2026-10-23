"""Deterministic fakes for M4 unit tests (no Ollama required)."""

from __future__ import annotations

from typing import Any

from hades_agent.models import (
    ConfidenceAssessment,
    ConfidenceLevel,
    DiagnosticReport,
    Evidence,
    Hypothesis,
)
from hades_client import (
    CpuInfo,
    DiskInfo,
    DiskUsage,
    IncidentPage,
    MemoryInfo,
    ProcessInfo,
    ProcessList,
    SystemMetrics,
    ToolSpec,
)


def sample_metrics() -> SystemMetrics:
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
        processes=ProcessList(sorted_by="cpu", limit=0, items=(), issues=()),
        issues=(),
    )


def sample_report(tool: str = "get_system_metrics") -> DiagnosticReport:
    return DiagnosticReport(
        summary="The system reports stable CPU and memory values.",
        evidence=[Evidence(tool=tool, finding="CPU utilization was 12.5%.")],
        likely_causes=[
            Hypothesis(
                cause="No fault is evident.",
                confidence=ConfidenceLevel.LOW,
                rationale="The available resource measurements are within normal range.",
                supporting_evidence=[0],
            )
        ],
        confidence=ConfidenceAssessment(
            level=ConfidenceLevel.LOW,
            explanation="Only a single snapshot was available.",
        ),
        next_checks=["Collect another snapshot over time."],
        data_gaps=["No historical trend was available."],
    )


class FakeHadesClient:
    def __init__(
        self,
        *,
        tools: list[ToolSpec] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.tools = tools if tools is not None else tool_specs()
        self.failures = failures or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> tuple[ToolSpec, ...]:
        self.calls.append(("list_tools", {}))
        failure = self.failures.get("list_tools")
        if failure:
            raise failure
        return tuple(self.tools)

    def _maybe_fail(self, name: str) -> None:
        failure = self.failures.get(name)
        if failure:
            raise failure

    def get_system_metrics(self):
        self.calls.append(("get_system_metrics", {}))
        self._maybe_fail("get_system_metrics")
        return sample_metrics()

    def list_top_processes(self, *, sort: str = "cpu", limit: int = 10):
        self.calls.append(("list_top_processes", {"sort": sort, "limit": limit}))
        self._maybe_fail("list_top_processes")
        return ProcessList(sorted_by=sort, limit=limit, items=(), issues=())

    def inspect_process(self, pid: int):
        self.calls.append(("inspect_process", {"pid": pid}))
        self._maybe_fail("inspect_process")
        return ProcessInfo(
            pid=pid,
            name="example",
            cpu_percent=1.5,
            memory_bytes=4096,
            status="sleep",
            run_time_seconds=10,
            executable_path_available=True,
        )

    def get_disk_usage(self):
        self.calls.append(("get_disk_usage", {}))
        self._maybe_fail("get_disk_usage")
        return DiskUsage(disks=sample_metrics().disks, issues=())

    def get_recent_incidents(self, *, limit: int = 20):
        self.calls.append(("get_recent_incidents", {"limit": limit}))
        self._maybe_fail("get_recent_incidents")
        return IncidentPage(incidents=(), total_known=0)


def tool_specs(*, extra: bool = False) -> list[ToolSpec]:
    names = [
        "get_system_metrics",
        "list_top_processes",
        "inspect_process",
        "get_disk_usage",
        "get_recent_incidents",
    ]
    if extra:
        names.append("delete_everything")
    return [
        ToolSpec(
            name=name,
            description=f"Description for {name}",
            input_schema={"type": "object"},
            input_schema_version=1,
            output_schema_version=1,
        )
        for name in names
    ]
