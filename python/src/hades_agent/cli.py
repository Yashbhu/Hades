"""Command-line interface for the local Hades diagnostic agent."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from hades_client import (
    AgentConnectionError,
    AgentTimeoutError,
    HadesClient,
    ProtocolError,
    RemoteToolError,
)

from .agent import DiagnosticAgent
from .config import AgentConfig
from .errors import HadesAgentError, IncidentInputError
from .model import OllamaModelProvider
from .models import IncidentContext, InvestigationResult
from .tools import DiagnosticToolbox


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--model",
        help="Ollama model name (default: env HADES_MODEL or qwen2.5:7b)",
    )
    common.add_argument("--ollama-url", help="Loopback Ollama endpoint")
    common.add_argument("--bridge-url", help="Loopback Rust diagnostic service")
    common.add_argument("--model-timeout", type=float, help="Model request timeout in seconds")
    common.add_argument("--tool-timeout", type=float, help="Tool request timeout in seconds")
    common.add_argument("--max-tool-calls", type=int, help="Maximum tool calls (1-32)")
    common.add_argument("--max-iterations", type=int, help="Maximum model turns (1-16)")
    common.add_argument(
        "--json",
        action="store_true",
        help="Print the validated report and tool trace as JSON",
    )

    parser = argparse.ArgumentParser(
        prog="hades-agent",
        description="Local-first diagnostic agent over read-only Hades tools.",
    )
    parser.add_argument("--version", action="version", version="hades-agent 0.4.0")
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser(
        "ask",
        parents=[common],
        help="Ask a system-health question",
        description="Investigate a question using the local model and registered tools.",
    )
    ask.add_argument("question", help="Question for the diagnostic agent")
    ask.add_argument(
        "--incident-file",
        type=Path,
        help="Optional incident JSON to include as untrusted context",
    )

    incident = commands.add_parser(
        "incident",
        parents=[common],
        help="Investigate an incident event or the newest known incident",
        description="Investigate a supplied incident or fetch the newest one from the bridge.",
    )
    source = incident.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Incident event JSON file")
    source.add_argument(
        "--latest",
        action="store_true",
        help="Fetch the newest incident retained by the Rust monitor",
    )
    incident.add_argument(
        "--question",
        default="Investigate this incident and explain what the evidence shows.",
        help="Question to investigate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except HadesAgentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return 0


def run(args: argparse.Namespace) -> InvestigationResult:
    config = _config_from_args(args)
    client = HadesClient(config.bridge_url, timeout=config.tool_timeout)
    toolbox = DiagnosticToolbox.from_client(client)

    incident: IncidentContext | None = None
    question: str = args.question
    if args.command == "ask" and args.incident_file is not None:
        incident = _load_incident_file(args.incident_file)
    elif args.command == "incident":
        if args.file is not None:
            incident = _load_incident_file(args.file)
        else:
            incident = _latest_incident(client)
        question = args.question

    provider = OllamaModelProvider(config)
    model = provider.create_chat_model()
    agent = DiagnosticAgent(toolbox, model, config)
    return agent.investigate(question, incident)


def _config_from_args(args: argparse.Namespace) -> AgentConfig:
    config = AgentConfig.from_env()
    return config.merged(
        model=getattr(args, "model", None),
        ollama_url=getattr(args, "ollama_url", None),
        bridge_url=getattr(args, "bridge_url", None),
        model_timeout=getattr(args, "model_timeout", None),
        tool_timeout=getattr(args, "tool_timeout", None),
        max_tool_calls=getattr(args, "max_tool_calls", None),
        max_iterations=getattr(args, "max_iterations", None),
    )


def _load_incident_file(path: Path) -> IncidentContext:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise IncidentInputError(f"cannot read incident file {path}: {error}") from error
    if len(raw) > 1_000_000:
        raise IncidentInputError("incident file is larger than 1 MB")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IncidentInputError(f"incident file is not valid JSON: {error}") from error
    return IncidentContext.from_payload(payload)


def _latest_incident(client: HadesClient) -> IncidentContext:
    try:
        page = client.get_recent_incidents(limit=1)
    except RemoteToolError as error:
        raise IncidentInputError(f"could not query incidents: {error}") from error
    except (AgentConnectionError, AgentTimeoutError, ProtocolError) as error:
        raise IncidentInputError(f"could not query incidents: {error}") from error
    if not page.incidents:
        raise IncidentInputError("the Rust monitor has not observed any incidents yet")
    return IncidentContext.from_payload(dataclasses.asdict(page.incidents[0]))


def format_report(result: InvestigationResult) -> str:
    report = result.report
    lines: list[str] = ["Diagnosis", "=========", report.summary, ""]

    lines.append("Observed evidence")
    lines.append("-----------------")
    if report.evidence:
        for index, evidence in enumerate(report.evidence):
            lines.append(f"{index + 1}. [{evidence.tool}] {evidence.finding}")
    else:
        lines.append("No tool evidence was available.")
    lines.append("")

    lines.append("Likely causes (hypotheses)")
    lines.append("--------------------------")
    if report.likely_causes:
        for index, hypothesis in enumerate(report.likely_causes, start=1):
            lines.append(
                f"{index}. {hypothesis.cause} "
                f"(confidence: {hypothesis.confidence.value})"
            )
            lines.append(f"   {hypothesis.rationale}")
            if hypothesis.supporting_evidence:
                refs = ", ".join(str(item + 1) for item in hypothesis.supporting_evidence)
                lines.append(f"   Supporting evidence: {refs}")
    else:
        lines.append("No likely cause could be ranked from the available evidence.")
    lines.append("")

    lines.append(f"Confidence: {report.confidence.level.value}")
    lines.append(report.confidence.explanation)
    lines.append("")

    lines.append("Next read-only checks")
    lines.append("---------------------")
    if report.next_checks:
        lines.extend(f"- {item}" for item in report.next_checks)
    else:
        lines.append("- None suggested.")
    lines.append("")

    if report.data_gaps:
        lines.append("Data gaps")
        lines.append("---------")
        lines.extend(f"- {item}" for item in report.data_gaps)
        lines.append("")

    failures = [call for call in result.tool_calls if not call.ok]
    stop_reasons = []
    if result.budget_exhausted:
        stop_reasons.append("tool-call budget exhausted")
    if result.iteration_limit_reached:
        stop_reasons.append("model-turn limit reached")
    lines.append(
        f"Investigation: {len(result.tool_calls)} tool calls, "
        f"{result.iterations} model turns, model {result.model}"
        + (f"; {'; '.join(stop_reasons)}" if stop_reasons else "")
    )
    if failures:
        lines.append("Tool failures:")
        lines.extend(
            f"- {call.name}: {call.error.get('code', 'error') if call.error else 'error'}"
            for call in failures
        )
    lines.append("")
    lines.append(
        "Note: hypotheses are model-generated and may be wrong. "
        "Verify them before acting. Hades performs no remediation."
    )
    return "\n".join(lines)
