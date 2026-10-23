"""Validated configuration for the local Hades agent."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigurationError

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _validate_loopback_url(value: str, field: str) -> str:
    if "://" not in value:
        value = f"http://{value}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http":
        raise ValueError(f"{field} must use http")
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"{field} must use a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field} contains an invalid port") from error
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    return f"http://{host}:{port}" if port is not None else f"http://{host}"


class AgentConfig(BaseModel):
    """Agent limits and local endpoints.

    All inference is local by construction: both endpoints must be loopback.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=200)
    ollama_url: str = DEFAULT_OLLAMA_URL
    bridge_url: str = DEFAULT_BRIDGE_URL
    model_timeout: float = Field(default=120.0, gt=0, le=600)
    tool_timeout: float = Field(default=10.0, gt=0, le=60)
    max_tool_calls: int = Field(default=8, ge=1, le=32)
    max_iterations: int = Field(default=6, ge=1, le=16)
    max_question_chars: int = Field(default=8_000, ge=256, le=100_000)
    max_tool_result_chars: int = Field(default=4_096, ge=1_024, le=100_000)
    max_context_chars: int = Field(default=24_000, ge=4_096, le=131_072)
    max_steps: int = Field(default=12, ge=1, le=64)
    max_consecutive_tool_failures: int = Field(default=3, ge=1, le=10)
    checkpoint_db: Path = Field(
        default_factory=lambda: Path.home() / ".hades" / "investigations.sqlite"
    )
    incident_db: Path = Field(
        default_factory=lambda: Path.home() / ".hades" / "incident_history.sqlite"
    )
    max_history_results: int = Field(default=5, ge=1, le=20)
    max_history_chars: int = Field(default=8_000, ge=1_000, le=64_000)
    num_ctx: int = Field(default=16384, ge=2048, le=131_072)
    num_predict: int = Field(default=1024, ge=128, le=8192)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: int = 0

    @field_validator("ollama_url", "bridge_url")
    @classmethod
    def _loopback_endpoint(cls, value: str, info: Any) -> str:
        return _validate_loopback_url(value, info.field_name)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentConfig:
        """Load configuration from environment variables.

        ``HADES_OLLAMA_URL`` takes precedence over ``OLLAMA_BASE_URL``.
        ``OLLAMA_HOST`` is not used because it configures the Ollama daemon's
        own listen address, not a client endpoint.
        """
        values = os.environ if env is None else env
        data: dict[str, Any] = {}
        mappings = {
            "model": ("HADES_MODEL",),
            "ollama_url": ("HADES_OLLAMA_URL", "OLLAMA_BASE_URL"),
            "bridge_url": ("HADES_BRIDGE_URL",),
            "model_timeout": ("HADES_MODEL_TIMEOUT",),
            "tool_timeout": ("HADES_TOOL_TIMEOUT",),
            "max_tool_calls": ("HADES_MAX_TOOL_CALLS",),
            "max_iterations": ("HADES_MAX_ITERATIONS",),
            "max_question_chars": ("HADES_MAX_QUESTION_CHARS",),
            "max_tool_result_chars": ("HADES_MAX_TOOL_RESULT_CHARS",),
            "max_context_chars": ("HADES_MAX_CONTEXT_CHARS",),
            "max_steps": ("HADES_MAX_STEPS",),
            "max_consecutive_tool_failures": (
                "HADES_MAX_CONSECUTIVE_TOOL_FAILURES",
            ),
            "checkpoint_db": ("HADES_CHECKPOINT_DB",),
            "incident_db": ("HADES_INCIDENT_DB",),
            "max_history_results": ("HADES_MAX_HISTORY_RESULTS",),
            "max_history_chars": ("HADES_MAX_HISTORY_CHARS",),
            "num_ctx": ("HADES_NUM_CTX",),
            "num_predict": ("HADES_NUM_PREDICT",),
            "temperature": ("HADES_TEMPERATURE",),
            "seed": ("HADES_SEED",),
        }
        for field, names in mappings.items():
            for name in names:
                value = values.get(name)
                if value is not None and value != "":
                    data[field] = value
                    break
        try:
            return cls.model_validate(data)
        except ValidationError as error:
            raise ConfigurationError(f"invalid agent configuration: {error}") from error

    def merged(self, **overrides: Any) -> AgentConfig:
        """Return a validated copy with selected fields replaced."""
        data = self.model_dump()
        data.update({key: value for key, value in overrides.items() if value is not None})
        try:
            return AgentConfig.model_validate(data)
        except ValidationError as error:
            raise ConfigurationError(f"invalid agent configuration: {error}") from error
