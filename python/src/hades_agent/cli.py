"""Command-line interface for the local Hades diagnostic agent."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hades_client import (
    AgentConnectionError,
    AgentTimeoutError,
    HadesClient,
    ProtocolError,
    RemoteToolError,
)

from .config import AgentConfig
from .errors import HadesAgentError, IncidentInputError, MemoryInputError
from .incident_memory import (
    IncidentMemoryStore,
    IncidentRecord,
    cutoff_days_ago,
)
from .model import OllamaModelProvider
from .models import DiagnosticReport, IncidentContext, InvestigationResult
from .tools import DiagnosticToolbox
from .workflow import InvestigationWorkflow, TerminationStatus, WorkflowOutcome


@dataclass(frozen=True)
class MemoryCommandResult:
    """A non-investigation command result with JSON and text renderings."""

    payload: dict[str, Any]
    text: str
    exit_code: int = 0


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
        "--incident-db",
        type=Path,
        help="Local SQLite incident-history path (default: ~/.hades/incident_history.sqlite)",
    )
    common.add_argument(
        "--max-history-results",
        type=int,
        help="Maximum historical incidents supplied to the planner (1-20)",
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
    parser.add_argument("--version", action="version", version="hades-agent 0.6.0")
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

    memory_common = argparse.ArgumentParser(add_help=False)
    memory_common.add_argument(
        "--incident-db",
        type=Path,
        help="Local SQLite incident-history path (default: ~/.hades/incident_history.sqlite)",
    )
    memory_common.add_argument("--json", action="store_true", help="Print as JSON")

    history = commands.add_parser(
        "history",
        parents=[memory_common],
        help="Inspect stored incident history",
        description="List durable incident records with optional structured filters.",
    )
    history.add_argument("--limit", type=int, default=20, help="Maximum records (1-200)")
    history.add_argument("--kind", help="Filter by incident kind")
    history.add_argument("--state", help="Filter by M2 lifecycle state")
    history.add_argument(
        "--resolution",
        choices=("open", "resolved", "recovered"),
        help="Filter by resolution status",
    )
    history.add_argument("--severity", help="Filter by severity")
    history.add_argument("--tag", help="Filter by tag")
    history.add_argument("--since", help="Only incidents updated at/after this ISO timestamp")

    history_show = commands.add_parser(
        "history-show",
        parents=[memory_common],
        help="Show one incident and its associations",
        description="Read a stored incident, its evidence, diagnoses, and events.",
    )
    history_show.add_argument("incident_id", help="Incident ID")

    history_verify = commands.add_parser(
        "history-verify",
        parents=[memory_common],
        help="Record a diagnosis verification outcome",
        description="Append confirmed, disproved, or unknown verification evidence.",
    )
    history_verify.add_argument("incident_id", help="Incident ID")
    history_verify.add_argument(
        "--status",
        required=True,
        choices=("confirmed", "disproved", "unknown"),
        help="Verification outcome",
    )
    history_verify.add_argument("--investigation", help="Specific investigation ID")
    history_verify.add_argument("--note", default="", help="Bounded verification note")

    history_resolve = commands.add_parser(
        "history-resolve",
        parents=[memory_common],
        help="Record an incident resolution status",
        description="Record resolution separately from diagnosis verification.",
    )
    history_resolve.add_argument("incident_id", help="Incident ID")
    history_resolve.add_argument(
        "--resolution",
        required=True,
        choices=("open", "resolved", "recovered"),
        help="Resolution status",
    )
    history_resolve.add_argument("--note", default="", help="Bounded note")

    history_export = commands.add_parser(
        "history-export",
        parents=[memory_common],
        help="Export incident history to a local JSON file",
        description="Write a bounded, local-only structured export.",
    )
    history_export.add_argument("--output", type=Path, required=True, help="Output JSON path")
    history_export.add_argument(
        "--limit", type=int, default=1_000, help="Maximum rows per table (1-1000)"
    )
    history_export.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )

    history_delete = commands.add_parser(
        "history-delete",
        parents=[memory_common],
        help="Delete one incident and its dependent records",
        description="Permanently delete one local incident record.",
    )
    history_delete.add_argument("incident_id", help="Incident ID")
    history_delete.add_argument(
        "--yes", action="store_true", help="Confirm permanent deletion"
    )

    history_prune = commands.add_parser(
        "history-prune",
        parents=[memory_common],
        help="Delete old incident and investigation records",
        description="Apply local retention to records older than a cutoff.",
    )
    history_prune.add_argument(
        "--older-than-days", type=int, required=True, help="Retention window in days"
    )
    history_prune.add_argument(
        "--dry-run", action="store_true", help="Report counts without deleting"
    )
    history_prune.add_argument(
        "--yes", action="store_true", help="Confirm permanent deletion"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"ask", "incident"} and not getattr(args, "investigation_id", None):
        args.investigation_id = str(uuid.uuid4())
    try:
        result = run(args)
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

    if isinstance(result, MemoryCommandResult):
        print(
            json.dumps(result.payload, ensure_ascii=False, indent=2)
            if args.json
            else result.text
        )
        return result.exit_code

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_workflow(result))
    return _exit_code(result)


def run(args: argparse.Namespace) -> WorkflowOutcome | MemoryCommandResult:
    if args.command.startswith("history"):
        return _run_memory_command(args)
    if args.command == "status":
        return InvestigationWorkflow.status(_status_config(args), args.investigation_id)

    config = _config_from_args(args)
    memory = IncidentMemoryStore(config.incident_db)
    try:
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
        workflow = InvestigationWorkflow(toolbox, model, config, memory=memory)
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
    finally:
        memory.close()


def _run_memory_command(args: argparse.Namespace) -> MemoryCommandResult:
    config = _memory_config(args)
    with IncidentMemoryStore(config.incident_db) as store:
        if args.command == "history":
            records = store.list_incidents(
                limit=args.limit,
                kind=args.kind,
                state=args.state,
                resolution_status=args.resolution,
                severity=args.severity,
                tag=args.tag,
                since=args.since,
            )
            payload = {
                "schema_version": store.schema_version,
                "incidents": [record.to_dict() for record in records],
            }
            return MemoryCommandResult(payload, _format_history(records))

        if args.command == "history-show":
            data = store.get_incident(args.incident_id)
            return MemoryCommandResult(data, _format_history_detail(data))

        if args.command == "history-verify":
            diagnosis = store.add_diagnosis_verification(
                args.incident_id,
                args.status,
                investigation_id=args.investigation,
                note=args.note,
            )
            payload = {
                "incident_id": args.incident_id,
                "diagnosis": diagnosis,
            }
            text = (
                f"Recorded diagnosis verification for {args.incident_id}: "
                f"{args.status}\nInvestigation: "
                f"{diagnosis['investigation_id']}\n"
            )
            return MemoryCommandResult(payload, text)

        if args.command == "history-resolve":
            record = store.resolve_incident(
                args.incident_id, args.resolution, note=args.note
            )
            payload = record.to_dict()
            text = (
                f"Incident {record.incident_id}: resolution "
                f"{record.resolution_status}\n"
                "Diagnosis verification is unchanged; resolution and verification "
                "are separate facts.\n"
            )
            return MemoryCommandResult(payload, text)

        if args.command == "history-export":
            data = store.export(limit=args.limit)
            target = _write_export(
                args.output,
                data,
                force=args.force,
                protected_paths=(config.incident_db, config.checkpoint_db),
            )
            payload = {
                "output": str(target),
                "schema_version": data["schema_version"],
                "incident_rows": len(data["tables"].get("incidents", [])),
            }
            return MemoryCommandResult(
                payload,
                f"Exported incident history to {target} (schema "
                f"{data['schema_version']}).\n",
            )

        if args.command == "history-delete":
            if not args.yes:
                raise MemoryInputError(
                    "refusing to delete incident history without --yes"
                )
            result = store.delete_incident(args.incident_id)
            return MemoryCommandResult(
                result,
                f"Deleted incident {args.incident_id} and dependent records.\n",
            )

        if args.command == "history-prune":
            if not args.dry_run and not args.yes:
                raise MemoryInputError(
                    "refusing to prune incident history without --yes"
                )
            cutoff = cutoff_days_ago(args.older_than_days)
            result = store.prune(cutoff, dry_run=args.dry_run)
            action = "Would delete" if args.dry_run else "Deleted"
            text = (
                f"{action} {result['incidents']} incidents and "
                f"{result['investigations']} investigations older than "
                f"{result['before']}.\n"
            )
            return MemoryCommandResult(result, text)

    raise MemoryInputError(f"unsupported history command: {args.command}")


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


def _memory_config(args: argparse.Namespace) -> AgentConfig:
    """Build only the configuration needed for local incident-memory commands."""
    config = AgentConfig()
    env_incident = os.environ.get("HADES_INCIDENT_DB")
    if env_incident:
        config = config.merged(incident_db=env_incident)
    incident_db = getattr(args, "incident_db", None)
    if incident_db is not None:
        config = config.merged(incident_db=incident_db)
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
        incident_db=getattr(args, "incident_db", None),
        max_history_results=getattr(args, "max_history_results", None),
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


def _format_history(records: list[IncidentRecord]) -> str:
    lines = ["Incident history", "================"]
    if not records:
        lines.append("No stored incidents matched.")
        lines.append("")
        return "\n".join(lines)
    for record in records:
        lines.append(
            f"- {record.incident_id} [{record.kind}/{record.severity}] "
            f"{record.state}; resolution={record.resolution_status}; "
            f"diagnosis={record.latest_diagnosis_status or 'none'}"
        )
        lines.append(f"  {record.summary}")
        lines.append(f"  updated: {record.updated_at}")
        if record.tags:
            lines.append(f"  tags: {', '.join(record.tags)}")
    lines.append("")
    lines.append("Use `hades-agent history-show <incident-id>` for details.")
    return "\n".join(lines)


def _format_history_detail(data: dict[str, Any]) -> str:
    lines = [
        "Incident",
        "========",
        f"ID: {data.get('incident_id')}",
        f"Kind: {data.get('kind')}",
        f"Severity: {data.get('severity')}",
        f"State: {data.get('state')}",
        f"Resolution: {data.get('resolution_status')}",
        f"Summary: {data.get('summary')}",
        f"Updated: {data.get('updated_at')}",
        "",
    ]
    metrics = data.get("metrics") or []
    lines.append("Latest metric samples")
    lines.append("---------------------")
    if metrics:
        latest: dict[str, dict[str, Any]] = {}
        for metric in metrics:
            name = str(metric.get("name"))
            latest.setdefault(name, metric)
        for name, metric in latest.items():
            lines.append(
                f"- {name}: {metric.get('value')} {metric.get('unit', '')} "
                f"({metric.get('observed_at')})"
            )
    else:
        lines.append("- None stored.")
    lines.append("")

    investigations = data.get("investigations") or []
    lines.append("Investigations")
    lines.append("--------------")
    if investigations:
        for investigation in investigations[:20]:
            lines.append(
                f"- {investigation.get('investigation_id')} "
                f"({investigation.get('stage')}, "
                f"{investigation.get('termination_status') or 'unterminated'})"
            )
    else:
        lines.append("- None stored.")
    lines.append("")

    diagnoses = data.get("diagnoses") or []
    lines.append("Diagnoses")
    lines.append("---------")
    if diagnoses:
        for diagnosis in diagnoses:
            lines.append(
                f"- {diagnosis.get('investigation_id')}: "
                f"{diagnosis.get('diagnosis_status')} / "
                f"{diagnosis.get('summary')}"
            )
            if diagnosis.get("verification_note"):
                lines.append(f"  Note: {diagnosis['verification_note']}")
    else:
        lines.append("- None stored.")
    lines.append("")

    evidence = data.get("evidence") or []
    lines.append("Current investigation evidence")
    lines.append("------------------------------")
    if evidence:
        for item in evidence[:20]:
            lines.append(
                f"- [{item.get('tool')}] {str(item.get('summary', ''))[:500]}"
            )
    else:
        lines.append("- None stored.")
    lines.append("")

    actions = data.get("actions") or []
    if actions:
        lines.append("Recorded remediation actions")
        lines.append("------------------------------")
        for item in actions[:20]:
            lines.append(
                f"- {item.get('status')}: {item.get('action')} "
                f"({item.get('outcome') or 'no outcome recorded'})"
            )
        lines.append("")

    verifications = data.get("diagnosis_verifications") or []
    if verifications:
        lines.append("Verification history")
        lines.append("-------------------")
        for item in verifications[:20]:
            lines.append(
                f"- {item.get('verified_at')}: {item.get('status')} "
                f"({item.get('note') or 'no note'})"
            )
        lines.append("")

    events = data.get("events") or []
    if events:
        lines.append("Lifecycle events")
        lines.append("----------------")
        for item in events[:20]:
            lines.append(
                f"- {item.get('occurred_at')}: {item.get('event_type')} "
                f"({item.get('detail') or 'no detail'})"
            )
        lines.append("")
    lines.extend(
        [
            "Historical records are not current evidence. Resolution and diagnosis",
            "verification are separate facts; this store never executes remediation.",
        ]
    )
    return "\n".join(lines)


def _write_export(
    path: Path,
    data: dict[str, Any],
    *,
    force: bool,
    protected_paths: tuple[Path, ...] = (),
) -> Path:
    target = path.expanduser()
    target_resolved = target.resolve()
    for protected in protected_paths:
        if target_resolved == Path(protected).expanduser().resolve():
            raise MemoryInputError(
                "refusing to overwrite a Hades database with a JSON export"
            )
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise MemoryInputError(
            f"cannot create export directory {target.parent}: {error}"
        ) from error
    if target.exists() and not force:
        raise MemoryInputError(
            f"{target} already exists; pass --force to replace it"
        )
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as error:
        raise MemoryInputError(f"cannot write export {target}: {error}") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return target


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

    if outcome.historical_incidents:
        lines.append("Historical context (not current evidence)")
        lines.append("-----------------------------------------")
        for item in outcome.historical_incidents:
            incident_id = item.get("incident_id", "unknown")
            summary = str(item.get("summary", ""))[:300]
            lines.append(
                f"- {incident_id} [{item.get('kind', 'unknown')}]: {summary}"
            )
            diagnosis = item.get("prior_diagnosis")
            if isinstance(diagnosis, dict):
                lines.append(
                    "  Prior diagnosis: "
                    f"{diagnosis.get('diagnosis_status', 'unknown')} / "
                    f"{str(diagnosis.get('summary', ''))[:300]}"
                )
            actions = item.get("prior_actions")
            if isinstance(actions, list):
                for action in actions[:3]:
                    if isinstance(action, dict):
                        lines.append(
                            "  Prior action: "
                            f"{action.get('status', 'unknown')} / "
                            f"{str(action.get('action', ''))[:300]}"
                        )
            reason = item.get("ranking_reason")
            if reason:
                lines.append(f"  Retrieved because: {reason}")
        lines.append("")

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
    if outcome.memory_errors:
        lines.append("Incident-memory warnings:")
        lines.extend(
            f"- {item.get('stage', 'memory')}: {item.get('message', 'error')}"
            for item in outcome.memory_errors
        )
        lines.append(
            "The investigation result is still shown; some incident-memory "
            "operations failed."
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
