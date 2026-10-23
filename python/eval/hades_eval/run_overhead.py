"""Run resource-overhead evaluation for Hades.

This harness measures process-level cost. It does not fabricate unavailable
temperature or power data.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PYTHON_ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get("HADES_BIN", REPO_ROOT / "target" / "debug" / "os-agent"))


def _sample_process(pid: int) -> tuple[float | None, int | None]:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "pcpu=,rss=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None, None
    if not output:
        return None, None
    parts = output.split()
    if len(parts) < 2:
        return None, None
    try:
        return float(parts[0]), int(parts[1]) * 1024
    except ValueError:
        return None, None


def _sample_ollama() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    try:
        output = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,comm=,rss="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return samples
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_text, comm, rss_text = parts
        if "ollama" not in comm.lower():
            continue
        try:
            samples.append(
                {
                    "pid": int(pid_text),
                    "rss_bytes": int(rss_text) * 1024,
                }
            )
        except ValueError:
            continue
    return samples


def _run_monitor(duration: float) -> dict[str, Any]:
    if not BINARY.exists():
        return {"status": "skipped", "reason": f"missing binary: {BINARY}"}
    started = time.perf_counter()
    process = subprocess.Popen(
        [str(BINARY), "monitor", "--duration", f"{duration}s", "--json-lines"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cpu_samples: list[float] = []
    rss_samples: list[int] = []
    while process.poll() is None:
        cpu, rss = _sample_process(process.pid)
        if cpu is not None:
            cpu_samples.append(cpu)
        if rss is not None:
            rss_samples.append(rss)
        time.sleep(0.5)
    wall = time.perf_counter() - started
    return {
        "status": "completed",
        "wall_seconds": wall,
        "exit_code": process.returncode,
        "cpu_percent_median": statistics.median(cpu_samples) if cpu_samples else None,
        "cpu_percent_max": max(cpu_samples) if cpu_samples else None,
        "rss_bytes_median": statistics.median(rss_samples) if rss_samples else None,
        "rss_bytes_max": max(rss_samples) if rss_samples else None,
        "samples": len(cpu_samples),
    }


def _run_python_worker(script: str, iterations: int) -> dict[str, Any]:
    code = f"""
import time
start = time.perf_counter()
for _ in range({iterations}):
{script}
print(time.perf_counter() - start)
"""
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PYTHON_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": (
                str(PYTHON_ROOT / "src")
                + os.pathsep
                + str(PYTHON_ROOT / "tests")
                + os.pathsep
                + str(PYTHON_ROOT / "eval")
            ),
        },
    )
    cpu_samples: list[float] = []
    rss_samples: list[int] = []
    ollama_samples: list[dict[str, Any]] = []
    while process.poll() is None:
        cpu, rss = _sample_process(process.pid)
        if cpu is not None:
            cpu_samples.append(cpu)
        if rss is not None:
            rss_samples.append(rss)
        ollama_samples.extend(_sample_ollama())
        time.sleep(0.5)
    stdout, stderr = process.communicate(timeout=30)
    wall = time.perf_counter() - started
    try:
        worker_seconds = float(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        worker_seconds = None
    return {
        "status": "completed" if process.returncode == 0 else "failed",
        "exit_code": process.returncode,
        "stderr": stderr.strip()[:1000],
        "wall_seconds": wall,
        "worker_seconds": worker_seconds,
        "iterations": iterations,
        "cpu_percent_median": statistics.median(cpu_samples) if cpu_samples else None,
        "cpu_percent_max": max(cpu_samples) if cpu_samples else None,
        "rss_bytes_median": statistics.median(rss_samples) if rss_samples else None,
        "rss_bytes_max": max(rss_samples) if rss_samples else None,
        "ollama_samples": ollama_samples,
        "samples": len(cpu_samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Hades resource-overhead evaluation.")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--workload", default="", help="Optional workload command (not executed by default)."
    )
    parser.add_argument("--llm", action="store_true", help="Attempt the live LLM path.")
    args = parser.parse_args()

    deterministic_script = """
from pathlib import Path
from hades_client import DiskUsage
from hades_eval.scenarios import SCENARIOS, metrics_for_scenario
from hades_eval.deterministic import diagnose
from hades_agent.config import AgentConfig
from hades_agent.tools import DiagnosticToolbox
from agent_fakes import FakeHadesClient

def make_client(scenario_name):
    class Client(FakeHadesClient):
        def get_system_metrics(self):
            self.calls.append(("get_system_metrics", {}))
            return metrics_for_scenario(scenario_name)
        def get_disk_usage(self):
            self.calls.append(("get_disk_usage", {}))
            m = metrics_for_scenario(scenario_name)
            return DiskUsage(disks=m.disks, issues=m.issues)
    return Client()

for scenario in SCENARIOS:
    with tempfile.TemporaryDirectory() as d:
        config = AgentConfig(checkpoint_db=Path(d) / "c.sqlite", incident_db=Path(d) / "i.sqlite")
        toolbox = DiagnosticToolbox.from_client(make_client(scenario.name))
        diagnose(
            question=scenario.question,
            incident=scenario.incident,
            toolbox=toolbox,
            historical=[],
        )
"""

    llm_script = """
from pathlib import Path
from hades_eval.scenarios import SCENARIOS
from hades_agent.config import AgentConfig
from hades_agent.model import OllamaModelProvider
from hades_agent.tools import DiagnosticToolbox
from hades_client import HadesClient
from hades_agent.workflow import InvestigationWorkflow
config = None
for scenario in SCENARIOS:
    with tempfile.TemporaryDirectory() as d:
        config = AgentConfig(checkpoint_db=Path(d) / "c.sqlite", incident_db=Path(d) / "i.sqlite", bridge_url="http://127.0.0.1:8765")
        client = HadesClient(config.bridge_url)
        toolbox = DiagnosticToolbox.from_client(client)
        model = OllamaModelProvider(config).create_chat_model()
        workflow = InvestigationWorkflow(toolbox, model, config)
        try:
            workflow.start(scenario.question, scenario.incident)
        finally:
            workflow.close()
"""

    results: dict[str, Any] = {
        "trials": args.trials,
        "duration_seconds": args.duration,
        "iterations": args.iterations,
        "monitor_only": [],
        "deterministic": [],
        "llm": [],
        "workload": args.workload or None,
        "temperature_power": (
            "unavailable: requires root-level powermetrics or "
            "platform-specific tooling"
        ),
    }

    for _ in range(args.trials):
        results["monitor_only"].append(_run_monitor(args.duration))

    for _ in range(args.trials):
        results["deterministic"].append(_run_python_worker(deterministic_script, args.iterations))

    if args.llm:
        for _ in range(args.trials):
            results["llm"].append(_run_python_worker(llm_script, args.iterations))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Wrote overhead results to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
