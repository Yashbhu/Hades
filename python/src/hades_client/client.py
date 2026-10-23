"""Synchronous, dependency-free client for the local Hades diagnostic service."""

from __future__ import annotations

import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from .errors import (
    AgentConnectionError,
    AgentTimeoutError,
    ProtocolError,
    RemoteToolError,
)
from .models import (
    DiskUsage,
    IncidentPage,
    ModelValidationError,
    ProcessInfo,
    ProcessList,
    SystemMetrics,
    ToolSpec,
)
from .protocol import CallRequest, CallResult, parse_call_response, parse_discovery

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PROCESS_LIMIT = 100
MAX_INCIDENT_LIMIT = 100

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class HadesClient:
    """Client for the read-only Hades diagnostic tools.

    The client performs no retries and never falls back to executing local
    commands. Every request is a JSON POST to ``/v1/tools/call``; discovery is
    a JSON GET to ``/v1/tools``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number of seconds")
        self.timeout = float(timeout)
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise ValueError("max_response_bytes must be an integer")
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")
        self.max_response_bytes = max_response_bytes

    def list_tools(self) -> tuple[ToolSpec, ...]:
        """Return discovery metadata for every registered tool."""
        status, payload = self._request("GET", "/v1/tools")
        if status != 200:
            _raise_error_envelope(payload, status)
            raise ProtocolError(f"discovery returned HTTP {status}")
        try:
            return parse_discovery(payload)
        except ProtocolError:
            raise

    def get_system_metrics(self) -> SystemMetrics:
        """Return a fresh CPU, memory, and filesystem snapshot."""
        data = self._call("get_system_metrics", {})
        return _validate(data, SystemMetrics.from_dict, "get_system_metrics")

    def list_top_processes(self, *, sort: str = "cpu", limit: int = 10) -> ProcessList:
        """Return a bounded process ranking sorted by ``cpu`` or ``memory``."""
        if sort not in {"cpu", "memory"}:
            raise ValueError("sort must be 'cpu' or 'memory'")
        _require_limit(limit, MAX_PROCESS_LIMIT)
        data = self._call("list_top_processes", {"sort": sort, "limit": limit})
        return _validate(data, ProcessList.from_dict, "list_top_processes")

    def inspect_process(self, pid: int) -> ProcessInfo:
        """Inspect one process by PID.

        Raises :class:`RemoteToolError` with code ``process_unavailable`` when
        the process exited or is not visible to the service user.
        """
        if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 0xFFFFFFFF:
            raise ValueError("pid must be an integer between 1 and 4294967295")
        data = self._call("inspect_process", {"pid": pid})
        return _validate(
            data,
            lambda value: ProcessInfo.from_dict(value, inspected=True),
            "inspect_process",
        )

    def get_disk_usage(self) -> DiskUsage:
        """Return filesystem capacity; the service never recursively scans."""
        data = self._call("get_disk_usage", {})
        return _validate(data, DiskUsage.from_dict, "get_disk_usage")

    def get_recent_incidents(self, *, limit: int = 20) -> IncidentPage:
        """Return recent incidents retained by the Rust monitor."""
        _require_limit(limit, MAX_INCIDENT_LIMIT)
        data = self._call("get_recent_incidents", {"limit": limit})
        return _validate(data, IncidentPage.from_dict, "get_recent_incidents")

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        request = CallRequest.create(tool, arguments)
        body = json.dumps(request.to_dict(), separators=(",", ":")).encode("utf-8")
        status, payload = self._request("POST", "/v1/tools/call", body)
        try:
            result: CallResult = parse_call_response(
                payload, request.request_id, http_status=status
            )
        except ProtocolError:
            raise
        if result.schema_version < 1:
            raise ProtocolError("result schema_version must be at least 1")
        return result.data

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                _require_json_content_type(response.headers.get("Content-Type"))
                payload = _decode_json(_read_limited(response, self.max_response_bytes))
                return status, payload
        except urllib.error.HTTPError as error:
            try:
                headers = error.headers
                _require_json_content_type(
                    headers.get("Content-Type") if headers else None
                )
                payload = _decode_json(_read_limited(error, self.max_response_bytes))
            finally:
                error.close()
            return error.code, payload
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise AgentTimeoutError(
                    f"request timed out after {self.timeout:g} seconds"
                ) from error
            raise AgentConnectionError(
                f"could not connect to the Hades agent at {self.base_url}"
            ) from error
        except TimeoutError as error:
            raise AgentTimeoutError(
                f"request timed out after {self.timeout:g} seconds"
            ) from error
        except OSError as error:
            raise AgentConnectionError(
                f"connection to the Hades agent failed: {error}"
            ) from error


def _validate(data: Mapping[str, Any], parser: Any, tool: str) -> Any:
    try:
        return parser(data)
    except ModelValidationError as error:
        raise ProtocolError(f"invalid {tool} result: {error}") from error


def _require_limit(limit: Any, maximum: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("base_url must be a string")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http":
        raise ValueError("base_url must use http")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("base_url must use a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("base_url contains an invalid port") from error
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    return f"http://{host}:{port}" if port is not None else f"http://{host}"


def _require_json_content_type(value: str | None) -> None:
    if value is None or value.split(";", 1)[0].strip().lower() != "application/json":
        raise ProtocolError("response Content-Type must be application/json")


def _read_limited(stream: Any, maximum: int) -> bytes:
    payload = stream.read(maximum + 1)
    if len(payload) > maximum:
        raise ProtocolError(f"response exceeds the {maximum} byte limit")
    return payload


def _decode_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("response body is not valid JSON") from error


def _raise_error_envelope(payload: Any, status: int) -> None:
    if not isinstance(payload, dict):
        return
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        message = error.get("message")
        details = error.get("details")
        raise RemoteToolError(
            error["code"],
            message if isinstance(message, str) else "unknown tool error",
            details=details if isinstance(details, dict) else None,
            http_status=status,
        )
