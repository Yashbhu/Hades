from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import unittest
from pathlib import Path

from hades_agent.agent import DiagnosticAgent
from hades_agent.config import AgentConfig
from hades_agent.model import OllamaModelProvider
from hades_agent.tools import DiagnosticToolbox
from hades_client import AgentConnectionError, HadesClient

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get("HADES_BIN", ROOT / "target" / "debug" / "os-agent"))


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@unittest.skipUnless(
    os.environ.get("HADES_OLLAMA_INTEGRATION") == "1",
    "set HADES_OLLAMA_INTEGRATION=1 with Ollama and the Rust service available",
)
class OptionalOllamaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BINARY.exists():
            raise RuntimeError(f"missing {BINARY}; run `cargo build --bin os-agent`")
        port = free_port()
        cls.config = AgentConfig.from_env().merged(
            bridge_url=f"http://127.0.0.1:{port}"
        )
        OllamaModelProvider(cls.config).preflight()
        cls.process = subprocess.Popen(
            [
                str(BINARY),
                "serve",
                "--bind",
                f"127.0.0.1:{port}",
                "--interval",
                "250ms",
                "--duration",
                "120s",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        cls.client = HadesClient(cls.config.bridge_url, timeout=cls.config.tool_timeout)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                cls.client.list_tools()
                break
            except AgentConnectionError:
                time.sleep(0.05)
        else:
            cls._stop()
            raise RuntimeError("Rust service did not start")

    @classmethod
    def _stop(cls) -> None:
        process = getattr(cls, "process", None)
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop()

    def test_real_local_model_and_service_produce_a_report(self) -> None:
        toolbox = DiagnosticToolbox.from_client(self.client)
        model = OllamaModelProvider(self.config).create_chat_model()
        agent = DiagnosticAgent(toolbox, model, self.config)
        result = agent.investigate("What are the current CPU and memory levels?")
        self.assertTrue(result.report.summary)
        self.assertGreaterEqual(result.iterations, 1)
        self.assertGreaterEqual(len(result.tool_calls), 1)


if __name__ == "__main__":
    unittest.main()
