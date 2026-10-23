from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from hades_agent.cli import build_parser, format_report, main
from hades_agent.models import (
    ConfidenceAssessment,
    ConfidenceLevel,
    DiagnosticReport,
    Evidence,
    IncidentContext,
    InvestigationResult,
    ToolExecution,
)


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

    def test_incident_sources_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["incident", "--latest", "--file", "x.json"])

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

    def test_main_wires_the_agent_and_emits_json(self) -> None:
        result = _result()
        stdout = io.StringIO()
        with (
            patch("hades_agent.cli.HadesClient"),
            patch("hades_agent.cli.DiagnosticToolbox") as toolbox,
            patch("hades_agent.cli.OllamaModelProvider") as provider,
            patch("hades_agent.cli.DiagnosticAgent") as agent,
            contextlib.redirect_stdout(stdout),
        ):
            toolbox.from_client.return_value = object()
            provider.return_value.create_chat_model.return_value = object()
            agent.return_value.investigate.return_value = result
            code = main(["ask", "Why?", "--json"])

        self.assertEqual(code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["report"]["summary"], "CPU is elevated.")
        self.assertIn("iteration_limit_reached", output)
        agent.return_value.investigate.assert_called_once()

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


def _result() -> InvestigationResult:
    return InvestigationResult(
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
        tool_calls=(),
        model="qwen2.5:7b",
        iterations=1,
        budget_exhausted=False,
    )


if __name__ == "__main__":
    unittest.main()
