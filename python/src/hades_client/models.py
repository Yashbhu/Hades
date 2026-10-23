"""Validated result models for every diagnostic tool."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class ModelValidationError(ValueError):
    """Internal validator failure; converted to ProtocolError by the protocol layer."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ModelValidationError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ModelValidationError(f"{field} must be an array")
    return value


def _str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{field} must be a string")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _str(value, field)


def _int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelValidationError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ModelValidationError(f"{field} must be at least {minimum}")
    return value


def _float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelValidationError(f"{field} must be a number")
    return float(value)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ModelValidationError(f"{field} must be a boolean")
    return value


@dataclass(frozen=True)
class ToolSpec:
    """Discovery metadata for one server-side tool."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    input_schema_version: int
    output_schema_version: int

    @classmethod
    def from_dict(cls, value: Any) -> ToolSpec:
        data = _mapping(value, "tool")
        return cls(
            name=_str(data.get("name"), "tool.name"),
            description=_str(data.get("description"), "tool.description"),
            input_schema=_mapping(data.get("input_schema"), "tool.input_schema"),
            input_schema_version=_int(
                data.get("input_schema_version"), "tool.input_schema_version", minimum=1
            ),
            output_schema_version=_int(
                data.get("output_schema_version"), "tool.output_schema_version", minimum=1
            ),
        )


@dataclass(frozen=True)
class CollectionIssue:
    """A partial metric-collection failure reported by the service."""

    component: str
    message: str

    @classmethod
    def from_dict(cls, value: Any) -> CollectionIssue:
        data = _mapping(value, "issue")
        return cls(
            component=_str(data.get("component"), "issue.component"),
            message=_str(data.get("message"), "issue.message"),
        )


@dataclass(frozen=True)
class CpuInfo:
    """CPU utilization from a system snapshot."""

    overall_percent: float
    per_core_percent: tuple[float, ...]
    logical_core_count: int
    sampling_interval_ms: int

    @classmethod
    def from_dict(cls, value: Any) -> CpuInfo:
        data = _mapping(value, "cpu")
        return cls(
            overall_percent=_float(data.get("overall_percent"), "cpu.overall_percent"),
            per_core_percent=tuple(
                _float(item, "cpu.per_core_percent[]")
                for item in _list(data.get("per_core_percent"), "cpu.per_core_percent")
            ),
            logical_core_count=_int(
                data.get("logical_core_count"), "cpu.logical_core_count", minimum=0
            ),
            sampling_interval_ms=_int(
                data.get("sampling_interval_ms"), "cpu.sampling_interval_ms", minimum=0
            ),
        )


@dataclass(frozen=True)
class MemoryInfo:
    """Physical memory metrics in bytes."""

    total_bytes: int
    used_bytes: int
    available_bytes: int
    free_bytes: int
    used_percent: float

    @classmethod
    def from_dict(cls, value: Any) -> MemoryInfo:
        data = _mapping(value, "memory")
        return cls(
            total_bytes=_int(data.get("total_bytes"), "memory.total_bytes", minimum=0),
            used_bytes=_int(data.get("used_bytes"), "memory.used_bytes", minimum=0),
            available_bytes=_int(
                data.get("available_bytes"), "memory.available_bytes", minimum=0
            ),
            free_bytes=_int(data.get("free_bytes"), "memory.free_bytes", minimum=0),
            used_percent=_float(data.get("used_percent"), "memory.used_percent"),
        )


@dataclass(frozen=True)
class DiskInfo:
    """Capacity information for one mounted filesystem."""

    mount_point: str
    file_system: str | None
    total_bytes: int
    available_bytes: int
    used_bytes: int
    used_percent: float

    @classmethod
    def from_dict(cls, value: Any) -> DiskInfo:
        data = _mapping(value, "disk")
        return cls(
            mount_point=_str(data.get("mount_point"), "disk.mount_point"),
            file_system=_optional_str(data.get("file_system"), "disk.file_system"),
            total_bytes=_int(data.get("total_bytes"), "disk.total_bytes", minimum=0),
            available_bytes=_int(
                data.get("available_bytes"), "disk.available_bytes", minimum=0
            ),
            used_bytes=_int(data.get("used_bytes"), "disk.used_bytes", minimum=0),
            used_percent=_float(data.get("used_percent"), "disk.used_percent"),
        )


@dataclass(frozen=True)
class ProcessInfo:
    """Resource use for one process.

    Inspected processes also expose status, run time, and whether executable
    metadata is readable. Command lines and executable paths are never returned.
    """

    pid: int
    name: str
    cpu_percent: float
    memory_bytes: int
    status: str | None = None
    run_time_seconds: int | None = None
    executable_path_available: bool | None = None

    @classmethod
    def from_dict(cls, value: Any, *, inspected: bool = False) -> ProcessInfo:
        data = _mapping(value, "process")
        if inspected:
            status = _optional_str(data.get("status"), "process.status")
            run_time = _int(
                data.get("run_time_seconds"), "process.run_time_seconds", minimum=0
            )
            executable = _bool(
                data.get("executable_path_available"),
                "process.executable_path_available",
            )
        else:
            status = None
            run_time = None
            executable = None
        return cls(
            pid=_int(data.get("pid"), "process.pid", minimum=1),
            name=_str(data.get("name"), "process.name"),
            cpu_percent=_float(data.get("cpu_percent"), "process.cpu_percent"),
            memory_bytes=_int(data.get("memory_bytes"), "process.memory_bytes", minimum=0),
            status=status,
            run_time_seconds=run_time,
            executable_path_available=executable,
        )


@dataclass(frozen=True)
class ProcessList:
    """Bounded process ranking."""

    sorted_by: str
    limit: int
    items: tuple[ProcessInfo, ...]
    issues: tuple[CollectionIssue, ...]

    @classmethod
    def from_dict(cls, value: Any) -> ProcessList:
        data = _mapping(value, "processes")
        sorted_by = _str(data.get("sorted_by"), "processes.sorted_by")
        if sorted_by not in {"cpu", "memory"}:
            raise ModelValidationError("processes.sorted_by must be 'cpu' or 'memory'")
        limit = _int(data.get("limit"), "processes.limit", minimum=0)
        items = tuple(
            ProcessInfo.from_dict(item)
            for item in _list(data.get("items"), "processes.items")
        )
        if len(items) > limit:
            raise ModelValidationError("processes.items exceeds processes.limit")
        return cls(
            sorted_by=sorted_by,
            limit=limit,
            items=items,
            issues=tuple(
                CollectionIssue.from_dict(item)
                for item in _list(data.get("issues", []), "processes.issues")
            ),
        )


@dataclass(frozen=True)
class SystemMetrics:
    """Snapshot returned by ``get_system_metrics``."""

    schema_version: int
    collected_at: str
    cpu: CpuInfo | None
    memory: MemoryInfo | None
    disks: tuple[DiskInfo, ...]
    processes: ProcessList
    issues: tuple[CollectionIssue, ...]

    @classmethod
    def from_dict(cls, value: Any) -> SystemMetrics:
        data = _mapping(value, "system_metrics")
        cpu_raw = data.get("cpu")
        memory_raw = data.get("memory")
        return cls(
            schema_version=_int(data.get("schema_version"), "schema_version", minimum=1),
            collected_at=_str(data.get("collected_at"), "collected_at"),
            cpu=None if cpu_raw is None else CpuInfo.from_dict(cpu_raw),
            memory=None if memory_raw is None else MemoryInfo.from_dict(memory_raw),
            disks=tuple(
                DiskInfo.from_dict(item) for item in _list(data.get("disks"), "disks")
            ),
            processes=ProcessList.from_dict(data.get("processes")),
            issues=tuple(
                CollectionIssue.from_dict(item)
                for item in _list(data.get("issues"), "issues")
            ),
        )


@dataclass(frozen=True)
class DiskUsage:
    """Filesystem capacity returned by ``get_disk_usage``."""

    disks: tuple[DiskInfo, ...]
    issues: tuple[CollectionIssue, ...]

    @classmethod
    def from_dict(cls, value: Any) -> DiskUsage:
        data = _mapping(value, "disk_usage")
        return cls(
            disks=tuple(
                DiskInfo.from_dict(item) for item in _list(data.get("disks"), "disks")
            ),
            issues=tuple(
                CollectionIssue.from_dict(item)
                for item in _list(data.get("issues"), "issues")
            ),
        )


@dataclass(frozen=True)
class Incident:
    """Incident state exposed by the monitoring engine."""

    id: str
    kind: str
    subject: str | None
    severity: str
    state: str
    started_at: str
    updated_at: str
    last_observed_at: str
    recovered_at: str | None
    summary: str
    evidence: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> Incident:
        data = _mapping(value, "incident")
        severity = _str(data.get("severity"), "incident.severity")
        if severity not in {"warning", "critical"}:
            raise ModelValidationError("incident.severity is not supported")
        state = _str(data.get("state"), "incident.state")
        if state not in {"active", "acknowledged", "recovered"}:
            raise ModelValidationError("incident.state is not supported")
        return cls(
            id=_str(data.get("id"), "incident.id"),
            kind=_str(data.get("kind"), "incident.kind"),
            subject=_optional_str(data.get("subject"), "incident.subject"),
            severity=severity,
            state=state,
            started_at=_str(data.get("started_at"), "incident.started_at"),
            updated_at=_str(data.get("updated_at"), "incident.updated_at"),
            last_observed_at=_str(
                data.get("last_observed_at"), "incident.last_observed_at"
            ),
            recovered_at=_optional_str(data.get("recovered_at"), "incident.recovered_at"),
            summary=_str(data.get("summary"), "incident.summary"),
            evidence=_mapping(data.get("evidence"), "incident.evidence"),
        )


@dataclass(frozen=True)
class IncidentPage:
    """Bounded incident query result."""

    incidents: tuple[Incident, ...]
    total_known: int

    @classmethod
    def from_dict(cls, value: Any) -> IncidentPage:
        data = _mapping(value, "recent_incidents")
        incidents = tuple(
            Incident.from_dict(item)
            for item in _list(data.get("incidents"), "incidents")
        )
        total_known = _int(data.get("total_known"), "total_known", minimum=0)
        if len(incidents) > total_known:
            raise ModelValidationError("incidents exceeds total_known")
        return cls(incidents=incidents, total_known=total_known)
