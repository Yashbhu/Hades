from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import unittest
from pathlib import Path

from hades_client import AgentConnectionError, HadesClient

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get("HADES_BIN", ROOT / "target" / "debug" / "os-agent"))


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LiveServiceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BINARY.exists():
            raise RuntimeError(
                f"missing Rust binary at {BINARY}; run `cargo build --bin os-agent` first"
            )
        cls.port = free_port()
        cls.address = f"127.0.0.1:{cls.port}"
        cls.process = subprocess.Popen(
            [
                str(BINARY),
                "serve",
                "--bind",
                cls.address,
                "--interval",
                "250ms",
                "--duration",
                "60s",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        cls.client = HadesClient(f"http://{cls.address}", timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                stderr = cls.process.stderr.read() if cls.process.stderr else ""
                raise RuntimeError(f"service exited during startup: {stderr}")
            try:
                cls.client.list_tools()
                return
            except AgentConnectionError:
                time.sleep(0.05)
        cls._stop()
        raise RuntimeError("service did not become ready within 10 seconds")

    @classmethod
    def _stop(cls) -> None:
        process = getattr(cls, "process", None)
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop()

    def test_discovery_matches_rust_registry(self) -> None:
        names = {tool.name for tool in self.client.list_tools()}
        self.assertEqual(
            names,
            {
                "get_system_metrics",
                "list_top_processes",
                "inspect_process",
                "get_disk_usage",
                "get_recent_incidents",
            },
        )

    def test_real_metrics_and_disks(self) -> None:
        metrics = self.client.get_system_metrics()
        self.assertEqual(metrics.schema_version, 1)
        self.assertGreater(metrics.memory.total_bytes if metrics.memory else 0, 0)
        self.assertGreater(len(metrics.disks), 0)

        disks = self.client.get_disk_usage()
        self.assertGreater(len(disks.disks), 0)
        self.assertGreater(disks.disks[0].total_bytes, 0)
        self.assertGreaterEqual(disks.disks[0].available_bytes, 0)

    def test_real_process_list_and_inspection(self) -> None:
        processes = self.client.list_top_processes(limit=3)
        self.assertLessEqual(len(processes.items), 3)
        self.assertEqual(processes.limit, 3)
        self.assertIn(processes.sorted_by, {"cpu", "memory"})

        inspected = self.client.inspect_process(os.getpid())
        self.assertEqual(inspected.pid, os.getpid())
        self.assertTrue(inspected.name)
        self.assertIsNotNone(inspected.executable_path_available)

    def test_recent_incidents_are_queryable(self) -> None:
        page = self.client.get_recent_incidents(limit=5)
        self.assertLessEqual(len(page.incidents), 5)
        self.assertGreaterEqual(page.total_known, len(page.incidents))


if __name__ == "__main__":
    unittest.main()
