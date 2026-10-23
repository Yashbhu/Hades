from __future__ import annotations

import json
import socket
import unittest

from fake_service import (
    FakeServer,
    discovery_payload,
    envelope,
    error_envelope,
    json_response,
    sample_system_metrics,
    slow_responder,
    tool_payload,
)
from hades_client import (
    AgentConnectionError,
    AgentTimeoutError,
    HadesClient,
    ProtocolError,
    ProtocolVersionError,
    RemoteToolError,
)


def standard_responder(method: str, path: str, body: bytes):
    if path == "/v1/tools":
        assert method == "GET"
        return json_response(200, discovery_payload())
    assert path == "/v1/tools/call"
    request = json.loads(body)
    return envelope(body, tool_payload(request["tool"], request.get("arguments")))


class ClientTests(unittest.TestCase):
    def test_every_tool_round_trips_through_validated_models(self) -> None:
        with FakeServer(standard_responder) as server:
            client = HadesClient(server.url, timeout=2)
            tools = client.list_tools()
            self.assertEqual(len(tools), 2)
            self.assertEqual(tools[0].name, "get_system_metrics")

            metrics = client.get_system_metrics()
            self.assertEqual(metrics.schema_version, 1)
            self.assertEqual(metrics.cpu.logical_core_count, 2)  # type: ignore[union-attr]
            self.assertEqual(metrics.disks[0].mount_point, "/")

            processes = client.list_top_processes(sort="memory", limit=2)
            self.assertEqual(processes.sorted_by, "memory")

            process = client.inspect_process(123)
            self.assertEqual(process.name, "example")
            self.assertTrue(process.executable_path_available)

            disks = client.get_disk_usage()
            self.assertEqual(disks.disks[0].available_bytes, 200)

            incidents = client.get_recent_incidents(limit=5)
            self.assertEqual(incidents.incidents, ())
            self.assertEqual(incidents.total_known, 0)

    def test_requests_use_the_documented_envelope_and_arguments(self) -> None:
        with FakeServer(standard_responder) as server:
            client = HadesClient(server.url, timeout=2)
            client.list_top_processes(sort="memory", limit=7)
            call = server.requests[-1]
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["path"], "/v1/tools/call")
            self.assertTrue(
                call["headers"]["Content-Type"].startswith("application/json")
            )
            body = json.loads(call["body"])
            self.assertEqual(body["protocol_version"], 1)
            self.assertEqual(body["tool"], "list_top_processes")
            self.assertEqual(body["arguments"], {"sort": "memory", "limit": 7})
            self.assertTrue(body["request_id"])

    def test_structured_tool_errors_are_preserved(self) -> None:
        def responder(method: str, path: str, body: bytes):
            if path == "/v1/tools":
                return json_response(200, discovery_payload())
            return error_envelope(
                body, "process_unavailable", "process is gone", status=404
            )

        with FakeServer(responder) as server:
            client = HadesClient(server.url, timeout=2)
            with self.assertRaises(RemoteToolError) as raised:
                client.inspect_process(123)
            self.assertEqual(raised.exception.code, "process_unavailable")
            self.assertEqual(raised.exception.http_status, 404)

    def test_protocol_errors_are_distinguished_from_tool_errors(self) -> None:
        def non_json(method: str, path: str, body: bytes):
            return 200, b"not json", "text/plain"

        with FakeServer(non_json) as server:
            with self.assertRaises(ProtocolError):
                HadesClient(server.url).get_system_metrics()

        def bad_json(method: str, path: str, body: bytes):
            return 200, b"{", "application/json"

        with FakeServer(bad_json) as server:
            with self.assertRaises(ProtocolError):
                HadesClient(server.url).get_system_metrics()

    def test_request_id_and_protocol_version_are_verified(self) -> None:
        def wrong_id(method: str, path: str, body: bytes):
            return json_response(
                200,
                {
                    "protocol_version": 1,
                    "request_id": "someone-else",
                    "status": "ok",
                    "result": {"schema_version": 1, "data": sample_system_metrics()},
                },
            )

        with FakeServer(wrong_id) as server:
            with self.assertRaises(ProtocolError):
                HadesClient(server.url).get_system_metrics()

        def wrong_version(method: str, path: str, body: bytes):
            request = json.loads(body)
            return json_response(
                200,
                {
                    "protocol_version": 2,
                    "request_id": request["request_id"],
                    "status": "ok",
                    "result": {"schema_version": 1, "data": sample_system_metrics()},
                },
            )

        with FakeServer(wrong_version) as server:
            with self.assertRaises(ProtocolVersionError):
                HadesClient(server.url).get_system_metrics()

    def test_malformed_result_shapes_raise_protocol_error(self) -> None:
        def wrong_shape(method: str, path: str, body: bytes):
            return envelope(body, {"schema_version": 1, "collected_at": 5})

        with FakeServer(wrong_shape) as server:
            with self.assertRaises(ProtocolError):
                HadesClient(server.url).get_system_metrics()

    def test_local_argument_validation_prevents_requests(self) -> None:
        with FakeServer(standard_responder) as server:
            client = HadesClient(server.url)
            for call in (
                lambda: client.list_top_processes(sort="disk"),
                lambda: client.list_top_processes(limit=0),
                lambda: client.inspect_process(0),
                lambda: client.get_recent_incidents(limit=101),
            ):
                with self.assertRaises(ValueError):
                    call()
            self.assertEqual(server.requests, [])

    def test_timeout_and_connection_failures_are_distinct(self) -> None:
        with FakeServer(slow_responder(0.3)) as server:
            with self.assertRaises(AgentTimeoutError):
                HadesClient(server.url, timeout=0.05).get_system_metrics()

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(AgentConnectionError):
            HadesClient(f"http://127.0.0.1:{port}", timeout=0.2).get_system_metrics()

    def test_discovery_validation_rejects_duplicates(self) -> None:
        tool = discovery_payload()["tools"][0]

        def duplicate(method: str, path: str, body: bytes):
            return json_response(
                200, {"protocol_version": 1, "tools": [tool, tool]}
            )

        with FakeServer(duplicate) as server:
            with self.assertRaises(ProtocolError):
                HadesClient(server.url).list_tools()

    def test_response_size_and_content_type_are_bounded(self) -> None:
        def huge(method: str, path: str, body: bytes):
            return 200, b"x" * 5000, "application/json"

        with FakeServer(huge) as server:
            with self.assertRaises(ProtocolError):
                HadesClient(server.url, max_response_bytes=1024).get_system_metrics()

    def test_base_url_must_be_loopback(self) -> None:
        for url in ("http://example.com:8765", "https://127.0.0.1:8765", "http://127.0.0.1:8765/path"):
            with self.assertRaises(ValueError):
                HadesClient(url)

    def test_incident_payload_is_validated_and_typed(self) -> None:
        incident = {
            "id": "inc-1",
            "kind": "high_cpu",
            "subject": None,
            "severity": "warning",
            "state": "active",
            "started_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:05Z",
            "last_observed_at": "2026-01-01T00:00:05Z",
            "recovered_at": None,
            "summary": "CPU is high",
            "evidence": {"kind": "high_cpu", "observed": {"latest_percent": 95.0}},
        }

        def responder(method: str, path: str, body: bytes):
            return envelope(
                body,
                {"incidents": [incident], "total_known": 1},
            )

        with FakeServer(responder) as server:
            page = HadesClient(server.url).get_recent_incidents()
            self.assertEqual(page.incidents[0].id, "inc-1")
            self.assertEqual(page.incidents[0].severity, "warning")


if __name__ == "__main__":
    unittest.main()
