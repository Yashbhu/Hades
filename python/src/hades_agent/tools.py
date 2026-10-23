"""Allowlisted LangChain adapters for the M3 diagnostic tools."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hades_client import (
    MAX_INCIDENT_LIMIT,
    MAX_PROCESS_LIMIT,
    AgentConnectionError,
    AgentTimeoutError,
    HadesClient,
    ProtocolError,
    RemoteToolError,
    ToolSpec,
)

from .errors import (
    ToolAllowlistError,
    ToolArgumentError,
    ToolCatalogError,
    ToolServiceError,
)
from .models import ToolExecution

EXPECTED_TOOLS = (
    "get_system_metrics",
    "list_top_processes",
    "inspect_process",
    "get_disk_usage",
    "get_recent_incidents",
)
SUPPORTED_SCHEMA_VERSION = 1


class EmptyArguments(BaseModel):
    """No arguments."""

    model_config = ConfigDict(extra="forbid", strict=True)


class TopProcessArguments(BaseModel):
    """Arguments for ``list_top_processes``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sort: Literal["cpu", "memory"] = Field(
        default="cpu", description="Rank by CPU or resident memory."
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=MAX_PROCESS_LIMIT,
        description=f"Maximum items (1-{MAX_PROCESS_LIMIT}).",
    )


class InspectProcessArguments(BaseModel):
    """Arguments for ``inspect_process``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    pid: int = Field(ge=1, le=0xFFFFFFFF, description="Process identifier.")


class RecentIncidentArguments(BaseModel):
    """Arguments for ``get_recent_incidents``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    limit: int = Field(
        default=20,
        ge=1,
        le=MAX_INCIDENT_LIMIT,
        description=f"Maximum incidents (1-{MAX_INCIDENT_LIMIT}).",
    )


@dataclass(frozen=True)
class ToolBinding:
    """One registered tool and its validated argument schema."""

    name: str
    description: str
    args_model: type[BaseModel]
    output_schema_version: int
    invoke: Callable[[BaseModel], Any]


class DiagnosticToolbox:
    """Strict registry that is the only path from a model to the Rust service.

    The model receives LangChain tool schemas, but execution goes through
    :meth:`execute`, which re-validates arguments and rejects anything outside
    :data:`EXPECTED_TOOLS`.
    """

    def __init__(
        self,
        client: HadesClient,
        descriptions: Mapping[str, str],
        output_versions: Mapping[str, int],
    ) -> None:
        self.client = client
        self._bindings = {
            "get_system_metrics": ToolBinding(
                name="get_system_metrics",
                description=descriptions["get_system_metrics"],
                args_model=EmptyArguments,
                output_schema_version=output_versions["get_system_metrics"],
                invoke=lambda _args: client.get_system_metrics(),
            ),
            "list_top_processes": ToolBinding(
                name="list_top_processes",
                description=descriptions["list_top_processes"],
                args_model=TopProcessArguments,
                output_schema_version=output_versions["list_top_processes"],
                invoke=lambda args: client.list_top_processes(
                    sort=args.sort, limit=args.limit
                ),
            ),
            "inspect_process": ToolBinding(
                name="inspect_process",
                description=descriptions["inspect_process"],
                args_model=InspectProcessArguments,
                output_schema_version=output_versions["inspect_process"],
                invoke=lambda args: client.inspect_process(pid=args.pid),
            ),
            "get_disk_usage": ToolBinding(
                name="get_disk_usage",
                description=descriptions["get_disk_usage"],
                args_model=EmptyArguments,
                output_schema_version=output_versions["get_disk_usage"],
                invoke=lambda _args: client.get_disk_usage(),
            ),
            "get_recent_incidents": ToolBinding(
                name="get_recent_incidents",
                description=descriptions["get_recent_incidents"],
                args_model=RecentIncidentArguments,
                output_schema_version=output_versions["get_recent_incidents"],
                invoke=lambda args: client.get_recent_incidents(limit=args.limit),
            ),
        }
        self._langchain_tools = tuple(
            self._make_langchain_tool(binding) for binding in self._bindings.values()
        )

    @classmethod
    def from_client(cls, client: HadesClient) -> DiagnosticToolbox:
        """Discover M3 tools and expose only the fixed allowlist."""
        try:
            discovered = {tool.name: tool for tool in client.list_tools()}
        except (AgentConnectionError, AgentTimeoutError) as error:
            raise ToolServiceError(
                "the Rust diagnostic service is unavailable; start it with "
                "`os-agent serve`"
            ) from error
        except ProtocolError as error:
            raise ToolServiceError(
                f"the Rust service returned malformed discovery data: {error}"
            ) from error

        missing = [name for name in EXPECTED_TOOLS if name not in discovered]
        if missing:
            raise ToolCatalogError(
                "the Rust service is missing required tools: " + ", ".join(missing)
            )
        unsupported = [
            name
            for name in EXPECTED_TOOLS
            if discovered[name].input_schema_version != SUPPORTED_SCHEMA_VERSION
            or discovered[name].output_schema_version != SUPPORTED_SCHEMA_VERSION
        ]
        if unsupported:
            raise ToolCatalogError(
                "the Rust service exposes unsupported schema versions for: "
                + ", ".join(unsupported)
            )
        descriptions = {name: _description(discovered[name]) for name in EXPECTED_TOOLS}
        versions = {
            name: discovered[name].output_schema_version for name in EXPECTED_TOOLS
        }
        return cls(client, descriptions, versions)

    @property
    def langchain_tools(self) -> Sequence[BaseTool]:
        """Tools to bind to the chat model."""
        return self._langchain_tools

    @property
    def allowed_names(self) -> tuple[str, ...]:
        return EXPECTED_TOOLS

    def arguments_schema(self, name: str) -> type[BaseModel]:
        """Return the Pydantic arguments model for tests and introspection."""
        return self._binding(name).args_model

    def validate_call(
        self, name: str, arguments: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Validate an allowlisted tool call without dispatching it.

        This is the application-code safety boundary used by the stateful
        workflow before it executes a model-proposed action.
        """
        binding = self._binding(name)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ToolArgumentError("tool arguments must be a JSON object")
        try:
            validated = binding.args_model.model_validate(dict(arguments))
        except ValidationError as error:
            raise ToolArgumentError(f"invalid arguments for {name}: {error}") from error
        return validated.model_dump()

    def execute(self, name: str, arguments: Mapping[str, Any] | None) -> ToolExecution:
        """Validate and execute one tool call.

        Tool-level service errors are returned as structured failures and are
        counted by the caller. Transport and protocol failures raise
        :class:`ToolServiceError` so the investigation can stop.
        """
        binding = self._binding(name)
        started = time.monotonic()
        normalized = self.validate_call(name, arguments)
        validated = binding.args_model.model_validate(normalized)
        try:
            result = binding.invoke(validated)
        except RemoteToolError as error:
            details = {
                "code": error.code,
                "message": error.message,
            }
            if error.details is not None:
                details["details"] = error.details
            output = json.dumps(
                {"ok": False, "tool": name, "error": details},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return ToolExecution(
                name=name,
                arguments=normalized,
                ok=False,
                output=output,
                error=details,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except (AgentConnectionError, AgentTimeoutError) as error:
            raise ToolServiceError(
                f"diagnostic service failed while calling {name}: {error}"
            ) from error
        except ProtocolError as error:
            raise ToolServiceError(
                f"diagnostic service returned a malformed response for {name}: {error}"
            ) from error
        except ValueError as error:
            raise ToolArgumentError(f"invalid arguments for {name}: {error}") from error

        payload = dataclasses.asdict(result)
        output = json.dumps(
            {
                "ok": True,
                "tool": name,
                "schema_version": binding.output_schema_version,
                "data": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return ToolExecution(
            name=name,
            arguments=normalized,
            ok=True,
            output=output,
            error=None,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _binding(self, name: str) -> ToolBinding:
        binding = self._bindings.get(name)
        if binding is None:
            raise ToolAllowlistError(f"tool '{name}' is not registered")
        return binding

    def _make_langchain_tool(self, binding: ToolBinding) -> BaseTool:
        def invoke_tool(**kwargs: Any) -> str:
            try:
                result = self.execute(binding.name, kwargs)
            except (ToolAllowlistError, ToolArgumentError) as error:
                return json.dumps(
                    {
                        "ok": False,
                        "tool": binding.name,
                        "error": {"code": "invalid_arguments", "message": str(error)},
                    },
                    ensure_ascii=False,
                )
            return result.output or ""

        return StructuredTool.from_function(
            func=invoke_tool,
            name=binding.name,
            description=binding.description,
            args_schema=binding.args_model,
        )


def _description(tool: ToolSpec) -> str:
    description = tool.description.strip()
    return description or f"Registered diagnostic tool {tool.name}"
