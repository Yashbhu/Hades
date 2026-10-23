from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from agent_fakes import FakeHadesClient, sample_metrics, sample_report
from hades_agent.config import AgentConfig
from hades_agent.errors import (
    CheckpointError,
    CheckpointIncompatibleError,
    InvestigationExistsError,
    InvestigationInputError,
    InvestigationNotFoundError,
)
from hades_agent.models import IncidentContext
from hades_agent.tools import DiagnosticToolbox
from hades_agent.workflow import (
    CheckpointStore,
    InvestigationWorkflow,
    TerminationStatus,
    _detect_contradictions,
)
from hades_client import AgentConnectionError, RemoteToolError
from test_agent import FakeChatModel, FakeReportModel, tool_call


class InterruptingChatModel:
    """Fake model that simulates a process interruption during inference."""

    def __init__(self, responses: list[Any], interrupt_on: int) -> None:
        self.responses = list(responses)
        self.interrupt_on = interrupt_on
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any]) -> InterruptingChatModel:
        return self

    def invoke(self, messages: list[Any]) -> Any:
        self.calls.append(list(messages))
        if len(self.calls) == self.interrupt_on:
            raise KeyboardInterrupt
        return self.responses.pop(0)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def config(self, name: str = "workflow", **overrides: Any) -> AgentConfig:
        return AgentConfig(
            checkpoint_db=Path(self.temp.name) / f"{name}.sqlite",
            max_tool_calls=4,
            max_steps=6,
            max_consecutive_tool_failures=3,
        ).merged(**overrides)

    def make_workflow(
        self,
        config: AgentConfig,
        responses: list[Any],
        *,
        client: FakeHadesClient | None = None,
        report_model: FakeReportModel | None = None,
        interrupt_after: tuple[str, ...] | None = None,
        inference_error: Exception | None = None,
    ) -> tuple[InvestigationWorkflow, FakeHadesClient]:
        client = client or FakeHadesClient()
        toolbox = DiagnosticToolbox.from_client(client)
        chat = FakeChatModel(responses, inference_error=inference_error)
        if report_model is None:
            report_model = FakeReportModel([sample_report()])
        workflow = InvestigationWorkflow(
            toolbox,
            chat,
            config,
            report_model=report_model,
            interrupt_after=interrupt_after,
        )
        self.addCleanup(workflow.close)
        return workflow, client

    def test_normal_investigation_reaches_a_validated_report(self) -> None:
        config = self.config()
        workflow, client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough evidence.")],
        )

        outcome = workflow.start("Is the CPU busy?")

        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(outcome.stage, "terminated")
        self.assertFalse(outcome.resumable)
        self.assertEqual(outcome.step_count, 2)
        self.assertEqual(outcome.tool_call_count, 1)
        self.assertEqual(len(outcome.evidence), 1)
        self.assertEqual(outcome.evidence[0]["tool"], "get_system_metrics")
        self.assertIsNotNone(outcome.report)
        self.assertEqual(
            [item["node"] for item in outcome.transitions],
            [
                "initialize",
                "retrieve_history",
                "plan",
                "validate",
                "execute",
                "assess",
                "plan",
                "finalize",
                "terminate",
            ],
        )
        self.assertEqual(
            [call[0] for call in client.calls if call[0] != "list_tools"],
            ["get_system_metrics"],
        )
        self.assertIsNotNone(outcome.checkpoint_id)

    def test_evidence_is_retained_across_multiple_tool_calls(self) -> None:
        config = self.config()
        workflow, _client = self.make_workflow(
            config,
            [
                tool_call("get_system_metrics", {}, "call-1"),
                tool_call("get_disk_usage", {}, "call-2"),
                AIMessage(content="Enough."),
            ],
        )

        outcome = workflow.start("Check resources")

        self.assertEqual(len(outcome.evidence), 2)
        self.assertEqual(
            [item["tool"] for item in outcome.evidence],
            ["get_system_metrics", "get_disk_usage"],
        )
        self.assertTrue(outcome.evidence[0]["summary"])
        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)

    def test_unknown_tool_is_rejected_without_dispatch(self) -> None:
        config = self.config(max_consecutive_tool_failures=1)
        workflow, client = self.make_workflow(
            config,
            [tool_call("run_shell", {"command": "rm -rf /"})],
            report_model=FakeReportModel([]),
        )

        outcome = workflow.start("Try a shell command")

        self.assertEqual(outcome.termination_status, TerminationStatus.TOOL_FAILURE_LIMIT.value)
        self.assertEqual(outcome.tool_errors[0]["code"], "unknown_tool")
        self.assertEqual(
            [call for call in client.calls if call[0] != "list_tools"],
            [],
        )

    def test_invalid_arguments_are_rejected_without_dispatch(self) -> None:
        config = self.config(max_consecutive_tool_failures=1)
        workflow, client = self.make_workflow(
            config,
            [tool_call("inspect_process", {"pid": 0})],
            report_model=FakeReportModel([]),
        )

        outcome = workflow.start("Inspect PID 0")

        self.assertEqual(outcome.tool_errors[0]["code"], "invalid_arguments")
        self.assertNotIn("inspect_process", [call[0] for call in client.calls])

    def test_tool_call_budget_is_enforced(self) -> None:
        config = self.config(max_tool_calls=2, max_steps=10)
        responses = [
            tool_call("get_system_metrics", {}, f"call-{index}") for index in range(5)
        ]
        workflow, client = self.make_workflow(config, responses)

        outcome = workflow.start("Keep checking")

        self.assertEqual(outcome.termination_status, TerminationStatus.BUDGET_EXHAUSTED.value)
        self.assertEqual(outcome.tool_call_count, 2)
        self.assertEqual(len(outcome.evidence), 2)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "get_system_metrics"]), 2
        )

    def test_step_limit_is_enforced_separately_from_tool_budget(self) -> None:
        config = self.config(max_tool_calls=10, max_steps=2)
        responses = [
            tool_call("get_system_metrics", {}, f"call-{index}") for index in range(5)
        ]
        workflow, _client = self.make_workflow(config, responses)

        outcome = workflow.start("Keep planning")

        self.assertEqual(outcome.termination_status, TerminationStatus.STEP_LIMIT.value)
        self.assertEqual(outcome.step_count, 2)
        self.assertEqual(outcome.tool_call_count, 2)

    def test_model_inference_failure_is_explicit(self) -> None:
        config = self.config()
        workflow, _client = self.make_workflow(
            config,
            [],
            report_model=FakeReportModel([]),
            inference_error=RuntimeError("timed out"),
        )

        outcome = workflow.start("Anything?")

        self.assertEqual(
            outcome.termination_status, TerminationStatus.UNRECOVERABLE_ERROR.value
        )
        self.assertIn("timed out", outcome.termination_reason or "")
        self.assertIsNotNone(outcome.report)
        self.assertFalse(outcome.report.likely_causes if outcome.report else True)
        self.assertEqual(outcome.model_errors[0]["code"], "model_inference_error")

    def test_malformed_model_output_is_handled(self) -> None:
        workflow, _client = self.make_workflow(
            self.config(),
            [AIMessage(content="")],
            report_model=FakeReportModel([]),
        )

        outcome = workflow.start("Anything?")

        self.assertEqual(
            outcome.termination_status,
            TerminationStatus.MALFORMED_MODEL_OUTPUT.value,
        )
        self.assertIsNotNone(outcome.report)

    def test_tool_service_failure_is_terminal(self) -> None:
        config = self.config()
        client = FakeHadesClient(failures={"get_system_metrics": AgentConnectionError("down")})
        workflow, _client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {})],
            client=client,
            report_model=FakeReportModel([]),
        )

        outcome = workflow.start("What is happening?")

        self.assertEqual(
            outcome.termination_status, TerminationStatus.UNRECOVERABLE_ERROR.value
        )
        self.assertEqual(outcome.tool_errors[0]["code"], "tool_service_error")
        self.assertEqual(outcome.tool_call_count, 1)

    def test_tool_failure_retry_limit_is_bounded(self) -> None:
        config = self.config(max_tool_calls=5, max_steps=5, max_consecutive_tool_failures=2)
        client = FakeHadesClient(
            failures={
                "inspect_process": RemoteToolError(
                    "process_unavailable", "process is gone", http_status=404
                )
            }
        )
        workflow, _client = self.make_workflow(
            config,
            [
                tool_call("inspect_process", {"pid": 404}, "call-1"),
                tool_call("inspect_process", {"pid": 404}, "call-2"),
            ],
            client=client,
            report_model=FakeReportModel([]),
        )

        outcome = workflow.start("Inspect the process")

        self.assertEqual(
            outcome.termination_status, TerminationStatus.TOOL_FAILURE_LIMIT.value
        )
        self.assertEqual(outcome.tool_call_count, 2)
        self.assertEqual([item["code"] for item in outcome.tool_errors], [
            "process_unavailable",
            "process_unavailable",
        ])

    def test_insufficient_evidence_returns_an_explicit_inconclusive_report(self) -> None:
        workflow, _client = self.make_workflow(
            self.config(),
            [AIMessage(content="I cannot determine this.")],
            report_model=FakeReportModel([]),
        )

        outcome = workflow.start("What is wrong?")

        self.assertEqual(outcome.termination_status, TerminationStatus.INCONCLUSIVE.value)
        self.assertIsNotNone(outcome.report)
        assert outcome.report is not None
        self.assertEqual(outcome.report.likely_causes, [])
        self.assertEqual(outcome.report.evidence, [])
        self.assertTrue(outcome.report.data_gaps)

    def test_incident_id_is_retained_in_evidence(self) -> None:
        incident = IncidentContext(
            id="inc-1",
            kind="high_cpu",
            severity="warning",
            state="active",
            summary="CPU is high",
            evidence={"kind": "high_cpu"},
        )
        workflow, _client = self.make_workflow(
            self.config(),
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
        )

        outcome = workflow.start("Explain this incident", incident)

        self.assertEqual(outcome.incident["id"], "inc-1")  # type: ignore[index]
        self.assertEqual(outcome.evidence[0]["incident_ids"], ["inc-1"])

    def test_checkpoint_is_created_and_resumed_without_repeating_completed_work(self) -> None:
        config = self.config()
        workflow, client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
            interrupt_after=("execute",),
        )

        paused = workflow.start("Check CPU")
        self.assertTrue(paused.resumable)
        self.assertEqual(paused.stage, "executing")
        self.assertEqual(len(paused.evidence), 1)
        self.assertEqual(
            [call for call in client.calls if call[0] != "list_tools"],
            [("get_system_metrics", {})],
        )

        saved = InvestigationWorkflow.status(config, paused.investigation_id)
        self.assertEqual(saved.stage, "executing")
        self.assertIsNone(saved.termination_status)
        self.assertTrue(saved.resumable)

        resumed = workflow.resume(paused.investigation_id)
        self.assertEqual(resumed.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(len(resumed.evidence), 1)
        self.assertEqual(
            [call for call in client.calls if call[0] != "list_tools"],
            [("get_system_metrics", {})],
        )

    def test_process_interruption_is_recovered_from_the_last_checkpoint(self) -> None:
        config = self.config()
        client = FakeHadesClient()
        toolbox = DiagnosticToolbox.from_client(client)
        interrupted = InterruptingChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
            interrupt_on=2,
        )
        first = InvestigationWorkflow(
            toolbox,
            interrupted,
            config,
            report_model=FakeReportModel([sample_report()]),
        )
        with self.assertRaises(KeyboardInterrupt):
            first.start("Check CPU", investigation_id="interrupt-id")
        first.close()

        resumed = InvestigationWorkflow(
            toolbox,
            FakeChatModel([AIMessage(content="Enough.")]),
            config,
            report_model=FakeReportModel([sample_report()]),
        )
        self.addCleanup(resumed.close)
        outcome = resumed.resume("interrupt-id")
        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(len(outcome.evidence), 1)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "get_system_metrics"]),
            1,
        )

    def test_corrupt_checkpoint_database_is_reported(self) -> None:
        path = Path(self.temp.name) / "corrupt.sqlite"
        path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(CheckpointError):
            CheckpointStore(path)

    def test_incompatible_checkpoint_state_is_rejected(self) -> None:
        config = self.config()
        workflow, _client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
        )
        outcome = workflow.start("Check CPU")
        configurable = {"configurable": {"thread_id": outcome.investigation_id}}
        workflow.graph.update_state(configurable, {"schema_version": 999})
        workflow.close()

        with self.assertRaises(CheckpointIncompatibleError):
            InvestigationWorkflow.status(config, outcome.investigation_id)

    def test_completed_checkpoint_is_not_reexecuted(self) -> None:
        config = self.config()
        workflow, client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough.")],
        )
        outcome = workflow.start("Check CPU")
        calls_before = len(client.calls)

        again = workflow.resume(outcome.investigation_id)

        self.assertEqual(again.termination_status, outcome.termination_status)
        self.assertEqual(len(client.calls), calls_before)

    def test_uncertain_in_flight_call_becomes_a_fresh_read_only_check(self) -> None:
        config = self.config()
        workflow, client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {})],
            interrupt_after=("validate",),
        )
        paused = workflow.start("Check CPU")
        values, _checkpoint_id, _metadata = workflow.store.load_raw(
            paused.investigation_id
        )  # type: ignore[misc]
        action = values["pending_action"]
        workflow.store.journal.mark_started(
            action["call_id"],
            paused.investigation_id,
            action["tool"],
            action["arguments"],
        )

        result = workflow._execute(values)  # noqa: SLF001

        self.assertTrue(result["evidence"][0]["fresh_check"])
        self.assertEqual(
            len([call for call in client.calls if call[0] == "get_system_metrics"]),
            1,
        )

    def test_completed_journal_entry_prevents_duplicate_execution(self) -> None:
        config = self.config()
        workflow, client = self.make_workflow(
            config,
            [tool_call("get_system_metrics", {})],
            interrupt_after=("validate",),
        )
        paused = workflow.start("Check CPU")
        values, _checkpoint_id, _metadata = workflow.store.load_raw(
            paused.investigation_id
        )  # type: ignore[misc]
        first = workflow._execute(values)  # noqa: SLF001
        second = workflow._execute(values)  # noqa: SLF001

        self.assertEqual(len(first["evidence"]), 1)
        self.assertEqual(len(second["evidence"]), 1)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "get_system_metrics"]),
            1,
        )

    def test_journaled_result_is_reused_after_reopening_the_database(self) -> None:
        config = self.config()
        client = FakeHadesClient()
        toolbox = DiagnosticToolbox.from_client(client)
        first = InvestigationWorkflow(
            toolbox,
            FakeChatModel([tool_call("get_system_metrics", {})]),
            config,
            report_model=FakeReportModel([sample_report()]),
            interrupt_after=("validate",),
        )
        paused = first.start("Check CPU")
        values, _checkpoint_id, _metadata = first.store.load_raw(
            paused.investigation_id
        )  # type: ignore[misc]
        first._execute(values)  # noqa: SLF001
        first.close()

        resumed = InvestigationWorkflow(
            toolbox,
            FakeChatModel([AIMessage(content="Enough.")]),
            config,
            report_model=FakeReportModel([sample_report()]),
        )
        self.addCleanup(resumed.close)
        outcome = resumed.resume(paused.investigation_id)

        self.assertEqual(outcome.termination_status, TerminationStatus.COMPLETED.value)
        self.assertEqual(len(outcome.evidence), 1)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "get_system_metrics"]),
            1,
        )

    def test_starting_an_existing_investigation_is_rejected(self) -> None:
        config = self.config()
        workflow, _client = self.make_workflow(
            config,
            [AIMessage(content="Enough.")],
            report_model=FakeReportModel([]),
        )
        outcome = workflow.start("Check CPU", investigation_id="fixed-id")

        with self.assertRaises(InvestigationExistsError):
            workflow.start("Check again", investigation_id=outcome.investigation_id)

    def test_missing_checkpoint_and_invalid_input_are_explicit(self) -> None:
        config = self.config()
        with self.assertRaises(InvestigationNotFoundError):
            InvestigationWorkflow.status(config, "does-not-exist")
        workflow, _client = self.make_workflow(config, [])
        with self.assertRaises(InvestigationInputError):
            workflow.start("   ")
        with self.assertRaises(InvestigationInputError):
            workflow.start("Question", investigation_id="../bad")

    def test_contradiction_detection_marks_repeated_disagreement(self) -> None:
        items = [
            {"tool": "get_system_metrics", "arguments": {}, "summary": "CPU 10%"},
            {"tool": "get_system_metrics", "arguments": {}, "summary": "CPU 95%"},
        ]
        self.assertEqual(len(_detect_contradictions(items)), 1)

    def test_assessment_records_contradictory_repeated_observations(self) -> None:
        class ChangingClient(FakeHadesClient):
            def __init__(self) -> None:
                super().__init__()
                self.metric_count = 0

            def get_system_metrics(self):
                self.metric_count += 1
                self.calls.append(("get_system_metrics", {}))
                metrics = sample_metrics()
                if self.metric_count > 1:
                    assert metrics.cpu is not None
                    metrics = dataclasses.replace(
                        metrics,
                        cpu=dataclasses.replace(metrics.cpu, overall_percent=95.0),
                    )
                return metrics

        client = ChangingClient()
        workflow, _client = self.make_workflow(
            self.config(),
            [
                tool_call("get_system_metrics", {}, "call-1"),
                tool_call("get_system_metrics", {}, "call-2"),
                AIMessage(content="Enough."),
            ],
            client=client,
        )

        outcome = workflow.start("Check CPU twice")

        self.assertEqual(len(outcome.contradictions), 1)
        self.assertIn("differed", outcome.contradictions[0])


if __name__ == "__main__":
    unittest.main()
