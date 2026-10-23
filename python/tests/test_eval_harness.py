"""Tests for the Hades evaluation harness."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "eval"))

# ruff: noqa: E402, I001
from hades_agent.models import DiagnosticReport
from hades_eval.deterministic import diagnose
from hades_eval.quality import score_report
from hades_eval.run_quality import main as run_quality_main
from hades_eval.run_quality import ScenarioClient
from hades_eval.scenarios import SCENARIOS, metrics_for_scenario
import unittest


class EvalHarnessTests(unittest.TestCase):
    def test_scenarios_are_well_formed(self) -> None:
        for scenario in SCENARIOS:
            metrics = metrics_for_scenario(scenario.name)
            self.assertEqual(metrics.schema_version, 1)
            if scenario.incident is not None:
                self.assertEqual(scenario.incident.kind, scenario.expected_incident_kind)

    def test_deterministic_diagnosis_is_valid(self) -> None:
        for scenario in SCENARIOS:
            client = ScenarioClient(scenario.name)
            from hades_agent.tools import DiagnosticToolbox

            toolbox = DiagnosticToolbox.from_client(client)
            result = diagnose(
                question=scenario.question,
                incident=scenario.incident,
                toolbox=toolbox,
                historical=[],
            )
            self.assertIsInstance(result.report, DiagnosticReport)
            self.assertEqual(result.model_invocations, 0)
            self.assertTrue(result.tool_executions)
            successful = {item.name for item in result.tool_executions if item.ok}
            for evidence in result.report.evidence:
                self.assertIn(evidence.tool, successful)

    def test_scoring_rejects_invalid_citation(self) -> None:
        from hades_agent.models import (
            ConfidenceAssessment,
            ConfidenceLevel,
            DiagnosticReport,
            Evidence,
        )

        report = DiagnosticReport(
            summary="Invalid citation example.",
            evidence=[Evidence(tool="get_system_metrics", finding="CPU was 12.5%.")],
            likely_causes=[],
            confidence=ConfidenceAssessment(
                level=ConfidenceLevel.LOW,
                explanation="Test.",
            ),
            next_checks=["Collect more data."],
        )
        score = score_report(
            scenario_name="normal_operation",
            expected_incident_kind=None,
            actual_incident_kind=None,
            report=report,
            tool_executions=[{"tool": "get_disk_usage", "ok": True}],
        )
        self.assertFalse(score.citation_valid)

    def test_run_quality_fake_writes_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "quality.json"
            old_argv = sys.argv
            sys.argv = [
                "run_quality",
                "--backend",
                "fake",
                "--output",
                str(output),
            ]
            try:
                self.assertEqual(run_quality_main(), 0)
            finally:
                sys.argv = old_argv
            records = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(records), len(SCENARIOS) * 2)
            self.assertTrue(all("score" in record for record in records))
            self.assertTrue(all(record["duration_ms"] >= 0 for record in records))
