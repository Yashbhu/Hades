"""Local-first diagnostic agent for Hades."""

from .agent import DiagnosticAgent
from .config import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, AgentConfig
from .errors import (
    ConfigurationError,
    HadesAgentError,
    IncidentInputError,
    InvestigationInputError,
    ModelInferenceError,
    ModelNotFoundError,
    ModelUnavailableError,
    ReportValidationError,
    ToolAllowlistError,
    ToolArgumentError,
    ToolCatalogError,
    ToolServiceError,
)
from .models import (
    ConfidenceAssessment,
    ConfidenceLevel,
    DiagnosticReport,
    Evidence,
    Hypothesis,
    IncidentContext,
    InvestigationResult,
    ToolExecution,
)
from .tools import EXPECTED_TOOLS, DiagnosticToolbox

__all__ = [
    "AgentConfig",
    "ConfidenceAssessment",
    "ConfidenceLevel",
    "ConfigurationError",
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DiagnosticAgent",
    "DiagnosticReport",
    "DiagnosticToolbox",
    "EXPECTED_TOOLS",
    "Evidence",
    "HadesAgentError",
    "Hypothesis",
    "IncidentContext",
    "IncidentInputError",
    "InvestigationInputError",
    "InvestigationResult",
    "ModelInferenceError",
    "ModelNotFoundError",
    "ModelUnavailableError",
    "ReportValidationError",
    "ToolAllowlistError",
    "ToolArgumentError",
    "ToolCatalogError",
    "ToolExecution",
    "ToolServiceError",
]
