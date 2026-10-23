"""Typed exceptions raised by the Hades Python client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class HadesError(Exception):
    """Base class for every client-visible error."""


class AgentConnectionError(HadesError):
    """The local agent could not be reached."""


class AgentTimeoutError(HadesError):
    """The request exceeded the configured timeout."""


class ProtocolError(HadesError):
    """The response was not a valid Hades protocol envelope."""


class ProtocolVersionError(ProtocolError):
    """The service returned an unsupported protocol version."""


class RemoteToolError(HadesError):
    """A structured tool error returned by the Rust service."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else None
        self.http_status = http_status
