"""User-facing errors for the local diagnostic agent."""

from __future__ import annotations


class HadesAgentError(Exception):
    """Base class for agent failures with a safe, actionable message."""


class ConfigurationError(HadesAgentError):
    """Invalid configuration value."""


class ModelUnavailableError(HadesAgentError):
    """The local Ollama daemon could not be reached."""


class ModelNotFoundError(HadesAgentError):
    """The configured model is not installed locally."""


class ModelInferenceError(HadesAgentError):
    """The local model failed while generating a response."""


class ToolCatalogError(HadesAgentError):
    """The Rust service does not expose the required diagnostic tools."""


class ToolAllowlistError(HadesAgentError):
    """A model requested a tool that is not registered by the agent."""


class ToolArgumentError(HadesAgentError):
    """Tool arguments failed validation; no request was sent."""


class ToolServiceError(HadesAgentError):
    """The bridge failed at the transport or protocol level."""


class ReportValidationError(HadesAgentError):
    """The model produced a report that failed schema or evidence checks."""


class IncidentInputError(HadesAgentError):
    """A supplied incident event is malformed or unavailable."""


class InvestigationInputError(HadesAgentError):
    """The question or incident supplied to the agent is invalid."""
