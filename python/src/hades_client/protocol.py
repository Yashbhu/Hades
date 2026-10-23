"""Versioned Hades protocol envelopes and response validation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ProtocolError, ProtocolVersionError, RemoteToolError
from .models import ModelValidationError, ToolSpec

PROTOCOL_VERSION = 1
MAX_REQUEST_ID_LENGTH = 128


@dataclass(frozen=True)
class CallRequest:
    """Request envelope sent to the Rust service."""

    request_id: str
    tool: str
    arguments: Mapping[str, Any]

    @classmethod
    def create(cls, tool: str, arguments: Mapping[str, Any]) -> CallRequest:
        return cls(request_id=uuid.uuid4().hex, tool=tool, arguments=arguments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": self.request_id,
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class CallResult:
    """Validated successful result payload."""

    schema_version: int
    data: Mapping[str, Any]


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a string")
    return value


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ProtocolError(f"{field} must be at least {minimum}")
    return value


def _check_protocol_version(payload: Mapping[str, Any]) -> None:
    version = payload.get("protocol_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProtocolError("protocol_version must be an integer")
    if version != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"service protocol version {version} is not supported; expected {PROTOCOL_VERSION}"
        )


def _check_request_id(payload: Mapping[str, Any], expected: str) -> None:
    request_id = payload.get("request_id")
    if request_id != expected:
        raise ProtocolError("response request_id does not match the request")


def parse_call_response(
    payload: Any,
    request_id: str,
    *,
    http_status: int | None = None,
) -> CallResult:
    """Validate a call response, raising typed errors for protocol or tool failures."""
    envelope = _mapping(payload, "response")
    _check_protocol_version(envelope)
    _check_request_id(envelope, request_id)

    status = envelope.get("status")
    if status == "ok":
        if "error" in envelope:
            raise ProtocolError("successful response must not contain error")
        result = _mapping(envelope.get("result"), "result")
        schema_version = _integer(result.get("schema_version"), "result.schema_version", minimum=1)
        data = _mapping(result.get("data"), "result.data")
        return CallResult(schema_version=schema_version, data=data)

    if status == "error":
        if "result" in envelope:
            raise ProtocolError("error response must not contain result")
        error = _mapping(envelope.get("error"), "error")
        code = _string(error.get("code"), "error.code")
        message = _string(error.get("message"), "error.message")
        details = error.get("details")
        if details is not None and not isinstance(details, dict):
            raise ProtocolError("error.details must be an object or null")
        raise RemoteToolError(
            code,
            message,
            details=details,
            http_status=http_status,
        )

    raise ProtocolError("status must be 'ok' or 'error'")


def parse_discovery(payload: Any) -> tuple[ToolSpec, ...]:
    """Validate the tool-discovery response."""
    envelope = _mapping(payload, "discovery")
    _check_protocol_version(envelope)
    tools_raw = envelope.get("tools")
    if not isinstance(tools_raw, list):
        raise ProtocolError("tools must be an array")
    try:
        tools = tuple(ToolSpec.from_dict(item) for item in tools_raw)
    except ModelValidationError as error:
        raise ProtocolError(f"invalid tool discovery: {error}") from error
    if len({tool.name for tool in tools}) != len(tools):
        raise ProtocolError("tool discovery contains duplicate names")
    return tools


def tool_names(tools: Sequence[ToolSpec]) -> tuple[str, ...]:
    """Convenience helper for introspection tests."""
    return tuple(tool.name for tool in tools)
