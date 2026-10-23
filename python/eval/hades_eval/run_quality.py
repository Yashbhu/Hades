"""Run diagnostic-quality evaluation for Hades.

Fake mode exercises harness correctness, fixtures, and scoring. It is not
evidence of real LLM diagnostic quality. Live mode uses the configured Ollama
model and a running Rust diagnostic service.
"""

from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import dataclasses
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "eval"))

from test_agent import FakeChatModel, FakeReportModel, tool_call
from hades_agent.config import AgentConfig
from hades_agent.incident_memory import IncidentMemoryStore
from hades_agent.tools import DiagnosticToolbox
from hades_agent.workflow import InvestigationWorkflow
from hades_client import HadesClient

from hades_eval.deterministic import diagnose as diagnose_deterministic
from hades_eval.quality import score_report, to_dict as score_to_dict
from hades_eval.scenarios import SCENARIOS, metrics_for_scenario, seed_history

try:
    from ollama import ResponseError as OllamaResponseError
except Exception:  # pragma: no cover - optional dependency
    OllamaResponseError = Exception

try:
    from hades_agent.model import OllamaModelProvider
except Exception:  # pragma: no cover
    OllamaModelProvider = None


class ScenarioClient:
    """A scenario-aware fake Hades client."""

    def __init__(self, scenario_name: str) -> None:
        from agent_fakes import FakeHadesClient

        self._base = FakeHadesClient()
        self.scenario_name = scenario_name
        self.calls = self._base.calls

    def list_tools(self) -> Any:
        return self._base.list_tools()

    def get_system_metrics(self) -> Any:
        self.calls.append(("get_system_metrics", {}))
        return metrics_for_scenario(self.scenario_name)

    def get_disk_usage(self) -> Any:
        self.calls.append(("get_disk_usage", {}))
        return self._base.get_disk_usage() if False else None

    def list_top_processes(self, *, sort: str = "cpu", limit: int = 10) -> Any:
        self.calls.append(("list_top_processes", {"sort": sort, "limit": limit}))
        return self._base.list_top_processes(sort=sort, limit=limit)

    def inspect_process(self, pid: int) -> Any:
        self.calls.append(("inspect_process", {"pid": pid}))
        return self._base.inspect_process(pid=pid)

    def get_recent_incidents(self, *, limit: int = 20) -> Any:
        self.calls.append(("get_recent_incidents", {"limit": limit}))
        return self._base.get_recent_incidents(limit=limit)


def _disk_usage_for(scenario_name: str) -> Any:
    from hades_client import DiskUsage

    metrics = metrics_for_scenario(scenario_name)
    return DiskUsage(disks=metrics.disks, issues=metrics.issues)


# Fix ScenarioClient.get_disk_usage after imports above.
ScenarioClient.get_disk_usage = lambda self: (  # type: ignore[method-assign]
    self.calls.append(("get_disk_usage", {})) or _disk_usage_for(self.scenario_name)
)


def _make_config(temp_dir: Path) -> AgentConfig:
    return AgentConfig(
        checkpoint_db=temp_dir / "checkpoints.sqlite",
        incident_db=temp_dir / "incidents.sqlite",
        max_tool_calls=4,
        max_steps=6,
        max_consecutive_tool_failures=3,
    )


def _run_deterministic(
    scenario, config: AgentConfig, client, history: bool
) -> dict[str, Any]:
    toolbox = DiagnosticToolbox.from_client(client)
    store = IncidentMemoryStore(config.incident_db)
    try:
        if history:
            seed_history(store)
        historical = []
        if history:
            from hades_agent.incident_memory import IncidentContext as _  # noqa: F401

        started = time.perf_counter()
        result = diagnose_deterministic(
            question=scenario.question,
            incident=scenario.incident,
            toolbox=toolbox,
            historical=[],
        )
        elapsed = (time.perf_counter() - started) * 1000
        if history:
            try:
                found = store.find_relevant(scenario.incident, limit=config.max_history_results)
                historical = [item.to_dict() for item in found]
            except Exception:
                historical = []
        score = score_report(
            scenario_name=scenario.name,
            expected_incident_kind=scenario.expected_incident_kind,
            actual_incident_kind=scenario.incident.kind if scenario.incident else None,
            report=result.report,
            tool_executions=[dataclasses.asdict(item) for item in result.tool_executions],
        )
        return {
            "path": "deterministic",
            "backend": "deterministic",
            "scenario": scenario.name,
            "question": scenario.question,
            "duration_ms": result.duration_ms,
            "elapsed_ms": elapsed,
            "model_invocations": result.model_invocations,
            "tool_calls": [dataclasses.asdict(item) for item in result.tool_executions],
            "historical_incidents": historical,
            "report": result.report.model_dump(mode="json"),
            "score": score_to_dict(score),
        }
    finally:
        store.close()


def _run_llm_fake(
    scenario, config: AgentConfig, client, history: bool
) -> dict[str, Any]:
    from langchain_core.messages import AIMessage

    toolbox = DiagnosticToolbox.from_client(client)
    store = IncidentMemoryStore(config.incident_db)
    try:
        if history:
            seed_history(store)
        chat = FakeChatModel(
            [tool_call("get_system_metrics", {}), AIMessage(content="Enough evidence.")]
        )
        reports = FakeReportModel([__import__("agent_fakes").sample_report()])
        workflow = InvestigationWorkflow(
            toolbox,
            chat,
            config,
            report_model=reports,
            memory=store,
        )
        try:
            counts = {"invoke": 0, "report": 0}
            original_invoke = workflow.agent.invoke_model
            original_report = workflow.agent.generate_report

            def counted_invoke(*args: Any, **kwargs: Any) -> Any:
                counts["invoke"] += 1
                return original_invoke(*args, **kwargs)

            def counted_report(*args: Any, **kwargs: Any) -> Any:
                counts["report"] += 1
                return original_report(*args, **kwargs)

            workflow.agent.invoke_model = counted_invoke  # type: ignore[method-assign]
            workflow.agent.generate_report = counted_report  # type: ignore[method-assign]

            started = time.perf_counter()
            outcome = workflow.start(scenario.question, scenario.incident)
            elapsed = (time.perf_counter() - started) * 1000
            tool_calls = [
                {"tool": item.get("tool"), "ok": True}
                for item in outcome.evidence
            ]
            score = score_report(
                scenario_name=scenario.name,
                expected_incident_kind=scenario.expected_incident_kind,
                actual_incident_kind=scenario.incident.kind if scenario.incident else None,
                report=outcome.report,
                tool_executions=tool_calls,
            )
            return {
                "path": "llm",
                "backend": "fake",
                "scenario": scenario.name,
                "question": scenario.question,
                "duration_ms": elapsed,
                "model_invocations": counts["invoke"] + counts["report"],
                "planning_invocations": counts["invoke"],
                "report_invocations": counts["report"],
                "termination_status": outcome.termination_status,
                "memory_status": outcome.memory_status,
                "memory_errors": list(outcome.memory_errors),
                "historical_incidents": list(outcome.historical_incidents),
                "tool_calls": tool_calls,
                "report": outcome.report.model_dump(mode="json") if outcome.report else None,
                "score": score_to_dict(score),
                "interpretation": (
                    "Fake-model result; validates harness plumbing only, "
                    "not LLM quality."
                ),
            }
        finally:
            workflow.close()
    finally:
        store.close()


def _live_llm_available(config: AgentConfig) -> tuple[bool, str]:
    if OllamaModelProvider is None:
        return False, "OllamaModelProvider unavailable"
    try:
        OllamaModelProvider(config).preflight()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, ""


def _run_llm_live(
    scenario, config: AgentConfig, client, history: bool
) -> dict[str, Any]:
    toolbox = DiagnosticToolbox.from_client(client)
    store = IncidentMemoryStore(config.incident_db)
    try:
        if history:
            seed_history(store)
        from hades_agent.model import OllamaModelProvider

        model = OllamaModelProvider(config).create_chat_model()
        workflow = InvestigationWorkflow(
            toolbox,
            model,
            config,
            memory=store,
        )
        try:
            counts = {"invoke": 0, "report": 0}
            original_invoke = workflow.agent.invoke_model
            original_report = workflow.agent.generate_report

            def counted_invoke(*args: Any, **kwargs: Any) -> Any:
                counts["invoke"] += 1
                return original_invoke(*args, **kwargs)

            def counted_report(*args: Any, **kwargs: Any) -> Any:
                counts["report"] += 1
                return original_report(*args, **kwargs)

            workflow.agent.invoke_model = counted_invoke  # type: ignore[method-assign]
            workflow.agent.generate_report = counted_report  # type: ignore[method-assign]

            started = time.perf_counter()
            outcome = workflow.start(scenario.question, scenario.incident)
            elapsed = (time.perf_counter() - started) * 1000
            tool_calls = [
                {"tool": item.get("tool"), "ok": True}
                for item in outcome.evidence
            ]
            score = score_report(
                scenario_name=scenario.name,
                expected_incident_kind=scenario.expected_incident_kind,
                actual_incident_kind=scenario.incident.kind if scenario.incident else None,
                report=outcome.report,
                tool_executions=tool_calls,
            )
            return {
                "path": "llm",
                "backend": "live",
                "scenario": scenario.name,
                "question": scenario.question,
                "duration_ms": elapsed,
                "model_invocations": counts["invoke"] + counts["report"],
                "planning_invocations": counts["invoke"],
                "report_invocations": counts["report"],
                "termination_status": outcome.termination_status,
                "memory_status": outcome.memory_status,
                "memory_errors": list(outcome.memory_errors),
                "historical_incidents": list(outcome.historical_incidents),
                "tool_calls": tool_calls,
                "report": outcome.report.model_dump(mode="json") if outcome.report else None,
                "score": score_to_dict(score),
                "interpretation": (
                    "Live Ollama result. Quality and overhead are "
                    "machine- and model-dependent."
                ),
            }
        finally:
            workflow.close()
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Hades diagnostic quality evaluation.")
    parser.add_argument("--backend", choices=("fake", "live"), default="fake")
    parser.add_argument(
        "--history", action="store_true", help="Seed equivalent historical context."
    )
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            config = _make_config(temp_dir)
            config = config.merged(bridge_url=args.bridge_url)
            client = (
                ScenarioClient(scenario.name)
                if args.backend == "fake"
                else HadesClient(args.bridge_url)
            )

            det = _run_deterministic(scenario, config, client, args.history)
            results.append(det)

            if args.backend == "fake":
                llm = _run_llm_fake(scenario, config, client, args.history)
                results.append(llm)
            else:
                available, reason = _live_llm_available(config)
                if available:
                    llm = _run_llm_live(scenario, config, client, args.history)
                    results.append(llm)
                else:
                    results.append(
                        {
                            "path": "llm",
                            "backend": "live",
                            "scenario": scenario.name,
                            "status": "skipped",
                            "reason": reason,
                        }
                    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {len(results)} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
