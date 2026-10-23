from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hades_agent.cli import build_parser, format_report, format_workflow, main
from hades_agent.incident_memory import IncidentMemoryStore
from hades_agent.models import (
    ConfidenceAssessment,
    ConfidenceLevel,
    DiagnosticReport,
    Evidence,
    IncidentContext,
    InvestigationResult,
    ToolExecution,
)
from hades_agent.workflow import WorkflowOutcome


class CliTests(unittest.TestCase):
    def test_parser_supports_ask_and_incident_modes(self) -> None:
        parser = build_parser()
        ask = parser.parse_args(["ask", "Why is memory high?", "--json"])
        self.assertEqual(ask.command, "ask")
        self.assertEqual(ask.question, "Why is memory high?")
        self.assertTrue(ask.json)

        incident = parser.parse_args(["incident", "--latest"])
        self.assertEqual(incident.command, "incident")
        self.assertTrue(incident.latest)

    def test_parser_supports_graph_commands_and_flags(self) -> None:
        parser = build_parser()
        resume = parser.parse_args(["resume", "inv-1", "--max-steps", "4"])
        self.assertEqual(resume.command, "resume")
        self.assertEqual(resume.investigation_id, "inv-1")
        self.assertEqual(resume.max_steps, 4)

        status = parser.parse_args(["status", "inv-1", "--json"])
        self.assertEqual(status.command, "status")
        self.assertEqual(status.investigation_id, "inv-1")
        self.assertTrue(status.json)

        incident = parser.parse_args(["incident", "--id", "inc-3"])
        self.assertEqual(incident.id, "inc-3")

        history = parser.parse_args(
            ["history", "--kind", "high_cpu", "--limit", "3", "--json"]
        )
        self.assertEqual(history.command, "history")
        self.assertEqual(history.limit, 3)
        self.assertTrue(history.json)

        verify = parser.parse_args(
            ["history-verify", "inc-1", "--status", "confirmed"]
        )
        self.assertEqual(verify.status, "confirmed")

    def test_incident_sources_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["incident", "--latest", "--file", "x.json"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["incident", "--latest", "--id", "inc-1"])

    def test_text_output_separates_evidence_hypotheses_and_confidence(self) -> None:
        result = InvestigationResult(
            question="Why?",
            incident=None,
            report=DiagnosticReport(
                summary="CPU is elevated.",
                evidence=[Evidence(tool="get_system_metrics", finding="CPU was 95%.")],
                likely_causes=[],
                confidence=ConfidenceAssessment(
                    level=ConfidenceLevel.MEDIUM,
                    explanation="One snapshot only.",
                ),
                next_checks=["Check per-core values."],
            ),
            tool_calls=(
                ToolExecution(
                    name="get_system_metrics",
                    arguments={},
                    ok=True,
                    output="{}",
                    error=None,
                    duration_ms=1.0,
                ),
            ),
            model="qwen2.5:7b",
            iterations=2,
            budget_exhausted=False,
        )
        text = format_report(result)
        self.assertIn("Observed evidence", text)
        self.assertIn("[get_system_metrics] CPU was 95%.", text)
        self.assertIn("Likely causes (hypotheses)", text)
        self.assertIn("Confidence: medium", text)
        self.assertIn("Verify them before acting", text)

    def test_workflow_text_shows_stage_status_and_report(self) -> None:
        text = format_workflow(_outcome())
        self.assertIn("Investigation", text)
        self.assertIn("ID: inv-1", text)
        self.assertIn("Stage: terminated", text)
        self.assertIn("Status: completed", text)
        self.assertIn("Observed evidence", text)
        self.assertIn("Verify them before acting", text)

    def test_workflow_text_shows_contradictory_evidence(self) -> None:
        outcome = dataclasses.replace(
            _outcome(),
            contradictions=("Repeated get_system_metrics observations differed.",),
        )
        text = format_workflow(outcome)
        self.assertIn("Contradictory evidence", text)
        self.assertIn("Repeated get_system_metrics", text)

    def test_configuration_errors_are_reported_without_network_access(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "ask",
                    "question",
                    "--bridge-url",
                    "http://203.0.113.10:8765",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("loopback", stderr.getvalue())

    def test_main_wires_the_graph_workflow_and_emits_json(self) -> None:
        outcome = _outcome()
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            incident_db = Path(temp) / "incidents.sqlite"
            with (
                patch("hades_agent.cli.HadesClient"),
                patch("hades_agent.cli.DiagnosticToolbox") as toolbox,
                patch("hades_agent.cli.OllamaModelProvider") as provider,
                patch("hades_agent.cli.InvestigationWorkflow") as workflow_cls,
                contextlib.redirect_stdout(stdout),
            ):
                toolbox.from_client.return_value = object()
                provider.return_value.create_chat_model.return_value = object()
                workflow_cls.return_value.start.return_value = outcome
                code = main(
                    [
                        "ask",
                        "Why?",
                        "--json",
                        "--checkpoint-db",
                        "checkpoints.sqlite",
                        "--incident-db",
                        str(incident_db),
                    ]
                )

            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["report"]["summary"], "CPU is elevated.")
            self.assertEqual(output["investigation_id"], "inv-1")
            self.assertIn("termination_status", output)
            workflow_cls.return_value.start.assert_called_once()
            workflow_cls.return_value.close.assert_called_once()

    def test_status_command_uses_workflow_status_without_a_model(self) -> None:
        outcome = _outcome()
        stdout = io.StringIO()
        with (
            patch("hades_agent.cli.InvestigationWorkflow.status", return_value=outcome),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["status", "inv-1", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["stage"], "terminated")

    def test_status_does_not_validate_unrelated_service_settings(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"HADES_BRIDGE_URL": "http://203.0.113.10:8765"}),
            patch(
                "hades_agent.cli.InvestigationWorkflow.status",
                return_value=_outcome(),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            code = main(["status", "inv-1", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["investigation_id"], "inv-1")

    def test_json_outcome_keeps_m4_compatible_keys(self) -> None:
        data = _outcome().to_dict()
        for key in (
            "report",
            "model",
            "limits",
            "iterations",
            "tool_calls",
            "budget_exhausted",
            "iteration_limit_reached",
        ):
            self.assertIn(key, data)

    def test_history_commands_inspect_verify_and_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "incidents.sqlite"
            _seed_memory(db)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["history", "--incident-db", str(db), "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(stdout.getvalue())["incidents"]), 1)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "history-verify",
                        "inc-1",
                        "--status",
                        "confirmed",
                        "--note",
                        "confirmed locally",
                        "--incident-db",
                        str(db),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(stdout.getvalue())["diagnosis"]["diagnosis_status"],
                "confirmed",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "history-resolve",
                        "inc-1",
                        "--resolution",
                        "resolved",
                        "--incident-db",
                        str(db),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["resolution_status"], "resolved")

    def test_history_export_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "incidents.sqlite"
            export = Path(temp) / "export.json"
            _seed_memory(db)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "history-delete",
                        "inc-1",
                        "--incident-db",
                        str(db),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("--yes", stderr.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "history-export",
                        "--output",
                        str(export),
                        "--incident-db",
                        str(db),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(export.exists())
            self.assertEqual(export.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(export.read_text())["schema_version"], 1)
            self.assertTrue(json.loads(stdout.getvalue())["output"].endswith("export.json"))

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "history-export",
                        "--output",
                        str(db),
                        "--force",
                        "--incident-db",
                        str(db),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("refusing to overwrite", stderr.getvalue())

    def test_history_show_text_marks_historical_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "incidents.sqlite"
            _seed_memory(db)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["history-show", "inc-1", "--incident-db", str(db)])
            self.assertEqual(code, 0)
            self.assertIn("Historical records are not current evidence", stdout.getvalue())
            self.assertIn("verification are separate facts", stdout.getvalue())

    def test_incident_payload_accepts_event_envelopes(self) -> None:
        incident = IncidentContext.from_payload(
            {
                "event": "incident_started",
                "timestamp": "2026-01-01T00:00:00Z",
                "incident": {
                    "id": "inc-1",
                    "kind": "memory_pressure",
                    "severity": "critical",
                    "state": "active",
                    "summary": "Memory is low",
                    "evidence": {"kind": "memory_pressure"},
                },
            }
        )
        self.assertEqual(incident.id, "inc-1")
        self.assertEqual(incident.severity, "critical")


def _seed_memory(path: Path) -> None:
    with IncidentMemoryStore(path) as store:
        store.upsert_incident(
            IncidentContext(
                id="inc-1",
                kind="high_cpu",
                severity="warning",
                state="active",
                summary="CPU is high",
                started_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                evidence={
                    "kind": "high_cpu",
                    "observed": {"latest_percent": 90.0},
                    "derived": {"recent_average_percent": 90.0},
                },
            )
        )
        store.record_investigation(
            "inv-1",
            "run-1",
            incident_id="inc-1",
            question="Why?",
            model="qwen2.5:7b",
            stage="terminated",
        )
        store.save_outcome(
            investigation_id="inv-1",
            execution_id="run-1",
            incident_id="inc-1",
            question="Why?",
            model="qwen2.5:7b",
            stage="terminated",
            termination_status="completed",
            report=_report(),
            evidence=[],
        )


def _report() -> DiagnosticReport:
    return DiagnosticReport(
        summary="CPU is elevated.",
        evidence=[Evidence(tool="get_system_metrics", finding="CPU was 95%.")],
        likely_causes=[],
        confidence=ConfidenceAssessment(
            level=ConfidenceLevel.MEDIUM,
            explanation="One snapshot only.",
        ),
        next_checks=["Check per-core values."],
    )


def _outcome() -> WorkflowOutcome:
    return WorkflowOutcome(
        investigation_id="inv-1",
        execution_id="run-1",
        schema_version=1,
        question="Why?",
        incident=None,
        stage="terminated",
        step_count=2,
        tool_call_count=1,
        evidence=(),
        tool_errors=(),
        model_errors=(),
        hypotheses=(),
        missing_evidence=(),
        contradictions=(),
        next_checks=("Check per-core values.",),
        report=_report(),
        stop_reason="model_final",
        termination_status="completed",
        termination_reason="model reported sufficient evidence",
        transitions=(),
        checkpoint_id="cp-1",
        next_nodes=(),
        model="qwen2.5:7b",
        limits={"max_tool_calls": 8},
    )


if __name__ == "__main__":
    unittest.main()
