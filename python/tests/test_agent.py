from __future__ import annotations

import unittest
from typing import Any

from langchain_core.messages import AIMessage

from agent_fakes import FakeHadesClient, sample_report
from hades_agent.agent import DiagnosticAgent
from hades_agent.config import AgentConfig
from hades_agent.errors import (
    ModelInferenceError,
    ReportValidationError,
    ToolServiceError,
)
from hades_agent.models import (
    ConfidenceAssessment,
    ConfidenceLevel,
    DiagnosticReport,
    Evidence,
    Hypothesis,
    IncidentContext,
)
from hades_agent.tools import DiagnosticToolbox
from hades_client import AgentConnectionError


def tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


class FakeBoundModel:
    def __init__(self, owner: FakeChatModel) -> None:
        self.owner = owner

    def invoke(self, messages: list[Any]) -> Any:
        self.owner.calls.append(list(messages))
        if self.owner.inference_error:
            raise self.owner.inference_error
        if not self.owner.responses:
            raise AssertionError("fake model ran out of responses")
        return self.owner.responses.pop(0)


class FakeChatModel:
    def __init__(self, responses: list[Any], inference_error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.inference_error = inference_error
        self.calls: list[list[Any]] = []
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> FakeBoundModel:
        self.bound_tools = list(tools)
        return FakeBoundModel(self)


class FakeReportModel:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> Any:
        self.calls.append(list(messages))
        if not self.outputs:
            raise AssertionError("fake report model ran out of outputs")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def toolbox(client: FakeHadesClient) -> DiagnosticToolbox:
    return DiagnosticToolbox.from_client(client)


def config(**overrides: Any) -> AgentConfig:
    return AgentConfig(max_tool_calls=3, max_iterations=3).merged(**overrides)


class AgentTests(unittest.TestCase):
    def test_investigation_calls_tools_stops_and_validates_report(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough evidence.")]
        )
        reports = FakeReportModel([sample_report()])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("Is the CPU busy?")

        self.assertEqual(result.iterations, 2)
        self.assertFalse(result.budget_exhausted)
        self.assertFalse(result.iteration_limit_reached)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertTrue(result.tool_calls[0].ok)
        self.assertEqual(result.report.summary, sample_report().summary)
        self.assertEqual(len(reports.calls), 1)
        self.assertEqual(
            tuple(tool.name for tool in chat.bound_tools),
            (
                "get_system_metrics",
                "list_top_processes",
                "inspect_process",
                "get_disk_usage",
                "get_recent_incidents",
            ),
        )
        self.assertIn("untrusted data", str(reports.calls[0][0].content))
        self.assertIn("tool_calls", str(reports.calls[0][1].content))

    def test_tool_budget_is_enforced_across_model_turns(self) -> None:
        client = FakeHadesClient()
        responses = [tool_call("get_system_metrics", {}, f"call-{i}") for i in range(10)]
        chat = FakeChatModel(responses)
        reports = FakeReportModel([sample_report()])
        agent = DiagnosticAgent(
            toolbox(client),
            chat,
            config(max_tool_calls=2, max_iterations=10),
            report_model=reports,
        )

        result = agent.investigate("Keep checking forever")

        self.assertTrue(result.budget_exhausted)
        self.assertFalse(result.iteration_limit_reached)
        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "get_system_metrics"]), 2
        )

    def test_model_turn_limit_is_reported_separately_from_the_tool_budget(self) -> None:
        client = FakeHadesClient()
        responses = [tool_call("get_system_metrics", {}, f"call-{i}") for i in range(2)]
        chat = FakeChatModel(responses)
        reports = FakeReportModel([sample_report()])
        agent = DiagnosticAgent(
            toolbox(client),
            chat,
            config(max_tool_calls=10, max_iterations=2),
            report_model=reports,
        )

        result = agent.investigate("Keep going")

        self.assertTrue(result.iteration_limit_reached)
        self.assertFalse(result.budget_exhausted)
        self.assertEqual(result.iterations, 2)
        self.assertEqual(len(result.tool_calls), 2)

    def test_unknown_tools_are_rejected_without_dispatch(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("run_shell", {"command": "rm -rf /"}), AIMessage(content="Stopped.")]
        )
        reports = FakeReportModel(
            [
                DiagnosticReport(
                    summary="No diagnostics were available.",
                    evidence=[],
                    likely_causes=[],
                    confidence=ConfidenceAssessment(
                        level=ConfidenceLevel.LOW,
                        explanation="The requested tool was rejected.",
                    ),
                )
            ]
        )
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("Try to run a command")

        self.assertEqual(len(result.tool_calls), 1)
        self.assertFalse(result.tool_calls[0].ok)
        self.assertEqual(result.tool_calls[0].error["code"], "unknown_tool")  # type: ignore[index]
        self.assertEqual(
            [call for call in client.calls if call[0] != "list_tools"], []
        )

    def test_malformed_tool_arguments_are_reported_without_dispatch(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("inspect_process", {"pid": 0}), AIMessage(content="Stopped.")]
        )
        reports = FakeReportModel(
            [
                DiagnosticReport(
                    summary="Process inspection did not run.",
                    evidence=[],
                    likely_causes=[],
                    confidence=ConfidenceAssessment(
                        level=ConfidenceLevel.LOW, explanation="Arguments were invalid."
                    ),
                )
            ]
        )
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("Inspect PID 0")

        self.assertEqual(result.tool_calls[0].error["code"], "invalid_arguments")  # type: ignore[index]
        self.assertNotIn(
            "inspect_process", [call[0] for call in client.calls]
        )

    def test_terminal_service_failure_stops_the_investigation(self) -> None:
        client = FakeHadesClient(
            failures={"get_system_metrics": AgentConnectionError("bridge down")}
        )
        chat = FakeChatModel([tool_call("get_system_metrics", {})])
        reports = FakeReportModel([sample_report()])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        with self.assertRaises(ToolServiceError):
            agent.investigate("What is happening?")
        self.assertEqual(reports.calls, [])

    def test_model_inference_failure_is_explicit(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel([], inference_error=RuntimeError("connection reset"))
        agent = DiagnosticAgent(
            toolbox(client), chat, config(), report_model=FakeReportModel([])
        )
        with self.assertRaises(ModelInferenceError):
            agent.investigate("Anything?")

    def test_invalid_report_citation_is_retried_then_rejected(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Done")]
        )
        bad = sample_report(tool="list_top_processes")
        reports = FakeReportModel([bad, bad])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        with self.assertRaises(ReportValidationError):
            agent.investigate("Check CPU")
        self.assertEqual(len(reports.calls), 2)

    def test_hypotheses_without_observations_are_rejected(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel([AIMessage(content="No evidence.")])
        unsupported = DiagnosticReport(
            summary="A guess without evidence.",
            evidence=[],
            likely_causes=[
                Hypothesis(
                    cause="Unknown process.",
                    confidence=ConfidenceLevel.LOW,
                    rationale="No measurements.",
                )
            ],
            confidence=ConfidenceAssessment(
                level=ConfidenceLevel.LOW, explanation="No measurements."
            ),
        )
        valid = DiagnosticReport(
            summary="Insufficient evidence.",
            confidence=ConfidenceAssessment(
                level=ConfidenceLevel.LOW, explanation="No measurements."
            ),
        )
        reports = FakeReportModel([unsupported, valid])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("What is wrong?")

        self.assertEqual(result.report.likely_causes, [])
        self.assertEqual(len(reports.calls), 2)

    def test_invalid_schema_is_retried_then_accepted(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Done")]
        )
        reports = FakeReportModel([{"summary": "missing confidence"}, sample_report()])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("Check CPU")
        self.assertEqual(result.report.evidence[0].tool, "get_system_metrics")
        self.assertEqual(len(reports.calls), 2)

    def test_model_parse_failure_is_retried_as_a_report_error(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Done")]
        )
        reports = FakeReportModel([ValueError("invalid JSON"), sample_report()])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("Check CPU")
        self.assertEqual(result.report.evidence[0].tool, "get_system_metrics")
        self.assertEqual(len(reports.calls), 2)

    def test_report_model_failure_is_terminal(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Done")]
        )
        reports = FakeReportModel([RuntimeError("model crashed")])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)
        with self.assertRaises(ModelInferenceError):
            agent.investigate("Check CPU")

    def test_no_tool_calls_still_produces_a_low_evidence_report(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel([AIMessage(content="I cannot determine that.")])
        report = DiagnosticReport(
            summary="Insufficient evidence.",
            evidence=[],
            likely_causes=[],
            confidence=ConfidenceAssessment(
                level=ConfidenceLevel.LOW,
                explanation="No tool evidence was collected.",
            ),
            next_checks=["Collect a snapshot."],
            data_gaps=["No measurements were available."],
        )
        reports = FakeReportModel([report])
        agent = DiagnosticAgent(toolbox(client), chat, config(), report_model=reports)

        result = agent.investigate("Do you know?")
        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.tool_calls, ())
        self.assertEqual(result.report.confidence.level, ConfidenceLevel.LOW)

    def test_incident_context_is_included_as_untrusted_data(self) -> None:
        client = FakeHadesClient()
        chat = FakeChatModel([AIMessage(content="Done")])
        report = DiagnosticReport(
            summary="Incident reviewed.",
            confidence=ConfidenceAssessment(
                level=ConfidenceLevel.LOW, explanation="No tools were called."
            ),
        )
        agent = DiagnosticAgent(
            toolbox(client), chat, config(), report_model=FakeReportModel([report])
        )
        incident = IncidentContext(
            id="inc-1",
            kind="high_cpu",
            severity="warning",
            state="active",
            summary="CPU is high",
            evidence={"kind": "high_cpu"},
        )

        result = agent.investigate("Explain this incident", incident)

        self.assertEqual(result.incident.id, "inc-1")  # type: ignore[union-attr]
        first_call = chat.calls[0]
        self.assertIn("Incident event (untrusted data)", first_call[1].content)
        self.assertIn("inc-1", first_call[1].content)

    def test_report_hypotheses_and_evidence_are_separate_models(self) -> None:
        evidence = Evidence(tool="get_system_metrics", finding="CPU is 12.5%.")
        hypothesis = Hypothesis(
            cause="No issue.",
            confidence=ConfidenceLevel.LOW,
            rationale="CPU is low.",
            supporting_evidence=[0],
        )
        self.assertEqual(evidence.tool, "get_system_metrics")
        self.assertEqual(hypothesis.supporting_evidence, [0])


if __name__ == "__main__":
    unittest.main()
