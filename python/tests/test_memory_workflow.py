from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from agent_fakes import FakeHadesClient, sample_report
from hades_agent.config import AgentConfig
from hades_agent.errors import MemoryStoreError
from hades_agent.incident_memory import IncidentMemoryStore
from hades_agent.tools import DiagnosticToolbox
from hades_agent.workflow import InvestigationWorkflow, TerminationStatus
from test_agent import FakeChatModel, FakeReportModel, tool_call
from test_incident_memory import incident


class BrokenMemory:
    def upsert_incident(self, *_args: Any, **_kwargs: Any) -> None:
        raise MemoryStoreError("history database unavailable")

    def record_investigation(self, *_args: Any, **_kwargs: Any) -> None:
        raise MemoryStoreError("history database unavailable")

    def find_relevant(self, *_args: Any, **_kwargs: Any) -> Any:
        raise MemoryStoreError("history database unavailable")

    def save_outcome(self, *_args: Any, **_kwargs: Any) -> None:
        raise MemoryStoreError("history database unavailable")


class MemoryWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = AgentConfig(
            checkpoint_db=Path(self.temp.name) / "checkpoints.sqlite",
            incident_db=Path(self.temp.name) / "incidents.sqlite",
            max_tool_calls=3,
            max_steps=5,
        )
        self.memory = IncidentMemoryStore(self.config.incident_db)
        self.addCleanup(self.memory.close)

    def seed(self, incident_id: str = "inc-old", value: float = 95.0) -> None:
        self.memory.upsert_incident(incident(incident_id, value=value))
        self.memory.record_investigation(
            "inv-old",
            "run-old",
            incident_id=incident_id,
            question="old question",
            model="local",
            stage="terminated",
        )
        self.memory.save_outcome(
            investigation_id="inv-old",
            execution_id="run-old",
            incident_id=incident_id,
            question="old question",
            model="local",
            stage="terminated",
            termination_status="completed",
            report=sample_report(),
            evidence=[
                {
                    "evidence_id": "ev-old",
                    "call_id": "call-old",
                    "tool": "get_system_metrics",
                    "arguments": {},
                    "summary": "historical CPU 95%",
                    "collected_at": "2026-01-01T00:00:01Z",
                }
            ],
        )

    def workflow(
        self,
        responses: list[Any],
        reports: FakeReportModel,
        *,
        memory: Any = None,
        interrupt_after: tuple[str, ...] | None = None,
        client: FakeHadesClient | None = None,
    ) -> tuple[InvestigationWorkflow, FakeHadesClient]:
        client = client or FakeHadesClient()
        workflow = InvestigationWorkflow(
            DiagnosticToolbox.from_client(client),
            FakeChatModel(responses),
            self.config,
            report_model=reports,
            memory=self.memory if memory is None else memory,
            interrupt_after=interrupt_after,
        )
        self.addCleanup(workflow.close)
        return workflow, client

    def test_history_is_retrieved_before_planning_and_persisted(self) -> None:
        self.seed()
        reports = FakeReportModel([sample_report()])
        workflow, _client = self.workflow(
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
            reports,
        )

        outcome = workflow.start(
            "Investigate current CPU",
            incident("inc-new", value=96.0, at="2026-01-02T00:00:00Z"),
        )

        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(outcome.memory_status, "persisted")
        self.assertEqual(len(outcome.historical_incidents), 1)
        historical = outcome.historical_incidents[0]
        self.assertEqual(historical["incident_id"], "inc-old")
        self.assertFalse(historical["is_current_evidence"])
        self.assertEqual(
            [item["node"] for item in outcome.transitions[:2]],
            ["initialize", "retrieve_history"],
        )
        self.assertEqual(len(outcome.evidence), 1)
        self.assertEqual(outcome.evidence[0]["incident_ids"], ["inc-new"])
        self.assertTrue(
            any(
                "historical_incidents" in str(message.content)
                for message in reports.calls[0]
            )
        )
        self.assertIn("not current", str(reports.calls[0][1].content))
        detail = self.memory.get_incident("inc-new")
        self.assertEqual(len(detail["evidence"]), 1)
        self.assertEqual(detail["diagnoses"][0]["diagnosis_status"], "unverified")

    def test_resume_keeps_history_and_does_not_duplicate_evidence(self) -> None:
        self.seed()
        first, _client = self.workflow(
            [tool_call("get_system_metrics", {})],
            FakeReportModel([sample_report()]),
            interrupt_after=("retrieve_history",),
        )
        paused = first.start(
            "Investigate current CPU",
            incident("inc-new", value=96.0, at="2026-01-02T00:00:00Z"),
        )
        self.assertTrue(paused.resumable)
        self.assertEqual(len(paused.historical_incidents), 1)
        self.assertEqual(paused.evidence, ())

        # Simulate a new process: reopen both the checkpoint database and the
        # incident-memory database before resuming.
        self.memory.close()
        reopened_memory = IncidentMemoryStore(self.config.incident_db)
        self.addCleanup(reopened_memory.close)
        resumed, _client = self.workflow(
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
            FakeReportModel([sample_report()]),
            memory=reopened_memory,
        )
        outcome = resumed.resume(paused.investigation_id)

        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(len(outcome.historical_incidents), 1)
        self.assertEqual(len(outcome.evidence), 1)
        detail = reopened_memory.get_incident("inc-new")
        self.assertEqual(len(detail["evidence"]), 1)
        self.assertEqual(len(detail["diagnoses"]), 1)

    def test_memory_failure_is_explicit_but_does_not_fake_evidence(self) -> None:
        reports = FakeReportModel([sample_report()])
        workflow, _client = self.workflow(
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
            reports,
            memory=BrokenMemory(),
        )

        outcome = workflow.start(
            "Investigate current CPU",
            incident("inc-new", value=96.0, at="2026-01-02T00:00:00Z"),
        )

        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(outcome.memory_status, "error")
        self.assertGreaterEqual(len(outcome.memory_errors), 2)
        self.assertEqual(len(outcome.evidence), 1)
        self.assertEqual(outcome.historical_incidents, ())
        self.assertIsNotNone(outcome.report)

    def test_prior_diagnosis_is_not_promoted_to_current_evidence(self) -> None:
        self.seed()
        workflow, _client = self.workflow(
            [AIMessage(content="Enough.")],
            FakeReportModel([sample_report()]),
        )

        outcome = workflow.start(
            "Investigate current CPU",
            incident("inc-new", value=96.0, at="2026-01-02T00:00:00Z"),
        )

        self.assertEqual(len(outcome.evidence), 0)
        self.assertEqual(len(outcome.historical_incidents), 1)
        self.assertIsNotNone(outcome.report)
        self.assertEqual(outcome.report.evidence, [])


if __name__ == "__main__":
    unittest.main()
