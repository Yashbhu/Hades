"""Python client for the local Hades diagnostic service."""

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_INCIDENT_LIMIT,
    MAX_PROCESS_LIMIT,
    HadesClient,
)
from .errors import (
    AgentConnectionError,
    AgentTimeoutError,
    HadesError,
    ProtocolError,
    ProtocolVersionError,
    RemoteToolError,
)
from .models import (
    CollectionIssue,
    CpuInfo,
    DiskInfo,
    DiskUsage,
    Incident,
    IncidentPage,
    MemoryInfo,
    ProcessInfo,
    ProcessList,
    SystemMetrics,
    ToolSpec,
)
from .protocol import PROTOCOL_VERSION

__all__ = [
    "AgentConnectionError",
    "AgentTimeoutError",
    "CollectionIssue",
    "CpuInfo",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DiskInfo",
    "DiskUsage",
    "HadesClient",
    "HadesError",
    "Incident",
    "IncidentPage",
    "MAX_INCIDENT_LIMIT",
    "MAX_PROCESS_LIMIT",
    "MemoryInfo",
    "PROTOCOL_VERSION",
    "ProcessInfo",
    "ProcessList",
    "ProtocolError",
    "ProtocolVersionError",
    "RemoteToolError",
    "SystemMetrics",
    "ToolSpec",
]
