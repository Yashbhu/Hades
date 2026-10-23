from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import unittest
from unittest.mock import patch

from hades_agent.cli import build_parser, format_report, format_workflow, main
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
            code = main(["ask", "Why?", "--json", "--checkpoint-db", "checkpoints.sqlite"])

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
