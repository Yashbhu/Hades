"""Command-line interface for the local Hades diagnostic agent."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import uuid
from pathlib import Path

from hades_client import (
    AgentConnectionError,
    AgentTimeoutError,
    HadesClient,
    ProtocolError,
    RemoteToolError,
)

from .config import AgentConfig
from .errors import HadesAgentError, IncidentInputError
from .model import OllamaModelProvider
from .models import DiagnosticReport, IncidentContext, InvestigationResult
from .tools import DiagnosticToolbox
from .workflow import InvestigationWorkflow, TerminationStatus, WorkflowOutcome


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
    common.add_argument("--max-iterations", type=int, help="Legacy M4 model-turn limit (1-16)")
    common.add_argument("--max-steps", type=int, help="Maximum graph planning steps (1-64)")
    common.add_argument(
        "--max-consecutive-tool-failures",
        type=int,
        help="Stop after this many consecutive rejected/failed actions (1-10)",
    )
    common.add_argument(
        "--checkpoint-db",
        type=Path,
        help="Local SQLite checkpoint path (default: ~/.hades/investigations.sqlite)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        help="Print the investigation snapshot and report as JSON",
    )

    parser = argparse.ArgumentParser(
        prog="hades-agent",
        description="Local-first diagnostic agent over read-only Hades tools.",
    )
    parser.add_argument("--version", action="version", version="hades-agent 0.5.0")
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
    ask.add_argument(
        "--investigation-id",
        help="Stable ID for this investigation (default: generated UUID)",
    )

    incident = commands.add_parser(
        "incident",
        parents=[common],
        help="Investigate an incident event or an existing incident ID",
        description="Investigate a supplied incident or fetch one from the bridge.",
    )
    source = incident.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="Incident event JSON file")
    source.add_argument(
        "--latest",
        action="store_true",
        help="Fetch the newest incident retained by the Rust monitor",
    )
    source.add_argument(
        "--id",
        help="Fetch a retained incident by ID (searches the 100 most recent)",
    )
    incident.add_argument(
        "--question",
        default="Investigate this incident and explain what the evidence shows.",
        help="Question to investigate",
    )
    incident.add_argument(
        "--investigation-id",
        help="Stable ID for this investigation (default: generated UUID)",
    )

    resume = commands.add_parser(
        "resume",
        parents=[common],
        help="Resume an interrupted investigation",
        description="Resume a checkpointed investigation by ID.",
    )
    resume.add_argument("investigation_id", help="Investigation ID")

    status = commands.add_parser(
        "status",
        help="Show the saved stage and state of an investigation",
        description="Read a local checkpoint without loading a model or Rust service.",
    )
    status.add_argument("investigation_id", help="Investigation ID")
    status.add_argument(
        "--checkpoint-db",
        type=Path,
        help="Local SQLite checkpoint path (default: ~/.hades/investigations.sqlite)",
    )
    status.add_argument("--json", action="store_true", help="Print the snapshot as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"ask", "incident"} and not getattr(args, "investigation_id", None):
        args.investigation_id = str(uuid.uuid4())
    try:
        outcome = run(args)
    except HadesAgentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        investigation_id = getattr(args, "investigation_id", None)
        if investigation_id:
            print(
                "interrupted; if the checkpoint was written, resume with "
                f"`hades-agent resume {investigation_id}`",
                file=sys.stderr,
            )
        else:
            print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_workflow(outcome))
    return _exit_code(outcome)


def run(args: argparse.Namespace) -> WorkflowOutcome:
    if args.command == "status":
        return InvestigationWorkflow.status(_status_config(args), args.investigation_id)

    config = _config_from_args(args)
    client = HadesClient(config.bridge_url, timeout=config.tool_timeout)
    toolbox = DiagnosticToolbox.from_client(client)

    incident: IncidentContext | None = None
    question = ""
    if args.command == "resume":
        pass
    elif args.command == "ask":
        question = args.question
        if args.incident_file is not None:
            incident = _load_incident_file(args.incident_file)
    elif args.command == "incident":
        question = args.question
        if args.file is not None:
            incident = _load_incident_file(args.file)
        elif args.latest:
            incident = _latest_incident(client)
        else:
            incident = _incident_by_id(client, args.id)

    provider = OllamaModelProvider(config)
    model = provider.create_chat_model()
    workflow = InvestigationWorkflow(toolbox, model, config)
    try:
        if args.command == "resume":
            return workflow.resume(args.investigation_id)
        return workflow.start(
            question,
            incident,
            investigation_id=args.investigation_id,
        )
    finally:
        workflow.close()


def _status_config(args: argparse.Namespace) -> AgentConfig:
    """Build only the configuration needed to read local checkpoints.

    `status` must not fail because an unrelated Ollama or bridge environment
    value is invalid, and it must not load either service.
    """
    config = AgentConfig()
    env_checkpoint = os.environ.get("HADES_CHECKPOINT_DB")
    env_model = os.environ.get("HADES_MODEL")
    if env_checkpoint:
        config = config.merged(checkpoint_db=env_checkpoint)
    if env_model:
        config = config.merged(model=env_model)
    checkpoint_db = getattr(args, "checkpoint_db", None)
    if checkpoint_db is not None:
        config = config.merged(checkpoint_db=checkpoint_db)
    return config


def _config_from_args(args: argparse.Namespace) -> AgentConfig:
    config = AgentConfig.from_env()
    max_steps = getattr(args, "max_steps", None)
    max_iterations = getattr(args, "max_iterations", None)
    if max_steps is None and max_iterations is not None:
        max_steps = max_iterations
    return config.merged(
        model=getattr(args, "model", None),
        ollama_url=getattr(args, "ollama_url", None),
        bridge_url=getattr(args, "bridge_url", None),
        model_timeout=getattr(args, "model_timeout", None),
        tool_timeout=getattr(args, "tool_timeout", None),
        max_tool_calls=getattr(args, "max_tool_calls", None),
        max_iterations=getattr(args, "max_iterations", None),
        max_steps=max_steps,
        max_consecutive_tool_failures=getattr(
            args, "max_consecutive_tool_failures", None
        ),
        checkpoint_db=getattr(args, "checkpoint_db", None),
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


def _incident_by_id(client: HadesClient, incident_id: str) -> IncidentContext:
    try:
        page = client.get_recent_incidents(limit=100)
    except RemoteToolError as error:
        raise IncidentInputError(f"could not query incidents: {error}") from error
    except (AgentConnectionError, AgentTimeoutError, ProtocolError) as error:
        raise IncidentInputError(f"could not query incidents: {error}") from error
    for incident in page.incidents:
        if incident.id == incident_id:
            return IncidentContext.from_payload(dataclasses.asdict(incident))
    raise IncidentInputError(
        f"incident {incident_id!r} was not found among the 100 most recent incidents"
    )


def format_report(result: InvestigationResult) -> str:
    """Render an M4 result in the original human-readable format."""
    lines = ["Diagnosis", "=========", result.report.summary, ""]
    lines.extend(_report_sections(result.report))
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
    lines.extend(_report_footer())
    return "\n".join(lines)


def format_workflow(outcome: WorkflowOutcome) -> str:
    """Render a graph-backed investigation snapshot and final report."""
    lines = [
        "Investigation",
        "=============",
        f"ID: {outcome.investigation_id}",
        f"Execution: {outcome.execution_id}",
        f"Stage: {outcome.stage}",
        f"Status: {outcome.termination_status or 'in progress'}",
        f"Reason: {outcome.termination_reason or 'not finished'}",
    ]
    if outcome.resumable:
        lines.append(f"Resume: hades-agent resume {outcome.investigation_id}")
    lines.append("")

    if outcome.report is None:
        lines.extend(["Diagnosis", "=========", "No final report is available.", ""])
    else:
        lines.extend(["Diagnosis", "=========", outcome.report.summary, ""])
        lines.extend(_report_sections(outcome.report))

    if outcome.contradictions:
        lines.append("Contradictory evidence")
        lines.append("---------------------")
        lines.extend(f"- {item}" for item in outcome.contradictions)
        lines.append("")

    lines.append(
        f"Investigation: {outcome.step_count} planning steps, "
        f"{outcome.tool_call_count} tool-call attempts, model {outcome.model}"
    )
    if outcome.tool_errors:
        lines.append("Tool/action failures:")
        lines.extend(
            f"- {item.get('tool') or '(unknown)'}: {item.get('code', 'error')}"
            for item in outcome.tool_errors
        )
    lines.extend(_report_footer())
    return "\n".join(lines)


def _report_sections(report: DiagnosticReport) -> list[str]:
    lines: list[str] = []
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
    return lines


def _report_footer() -> list[str]:
    return [
        "",
        "Note: hypotheses are model-generated and may be wrong. "
        "Verify them before acting. Hades performs no remediation.",
    ]


def _exit_code(outcome: WorkflowOutcome) -> int:
    if outcome.report is None:
        return 2
    if outcome.termination_status in {
        TerminationStatus.UNRECOVERABLE_ERROR.value,
        TerminationStatus.MALFORMED_MODEL_OUTPUT.value,
    }:
        return 2
    return 0
