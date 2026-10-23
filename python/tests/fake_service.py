"""Small deterministic HTTP fixtures used by the client tests."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[str, str, bytes], tuple[int, bytes, str]]


class FakeServer:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer._handle(self)

            def do_POST(self) -> None:  # noqa: N802
                outer._handle(self)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length)
        self.requests.append(
            {
                "method": handler.command,
                "path": handler.path,
                "body": body,
                "headers": dict(handler.headers),
            }
        )
        status, payload, content_type = self.responder(handler.command, handler.path, body)
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        try:
            handler.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The timeout test intentionally closes the client early.
            pass

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def __enter__(self) -> FakeServer:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def json_response(status: int, payload: Any) -> tuple[int, bytes, str]:
    return status, json.dumps(payload).encode(), "application/json"


def envelope(request_body: bytes, data: Any, *, schema_version: int = 1) -> tuple[int, bytes, str]:
    request = json.loads(request_body)
    return json_response(
        200,
        {
            "protocol_version": 1,
            "request_id": request["request_id"],
            "status": "ok",
            "result": {"schema_version": schema_version, "data": data},
        },
    )


def error_envelope(
    request_body: bytes,
    code: str,
    message: str,
    *,
    status: int = 400,
) -> tuple[int, bytes, str]:
    try:
        request_id = json.loads(request_body).get("request_id")
    except Exception:
        request_id = None
    return json_response(
        status,
        {
            "protocol_version": 1,
            "request_id": request_id,
            "status": "error",
            "error": {"code": code, "message": message},
        },
    )


def discovery_payload(tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if tools is None:
        tools = [
            {
                "name": "get_system_metrics",
                "description": "snapshot",
                "input_schema": {"type": "object"},
                "input_schema_version": 1,
                "output_schema_version": 1,
            },
            {
                "name": "inspect_process",
                "description": "process",
                "input_schema": {"type": "object"},
                "input_schema_version": 1,
                "output_schema_version": 1,
            },
        ]
    return {"protocol_version": 1, "tools": tools}


def sample_system_metrics() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "collected_at": "2026-01-01T00:00:00.000Z",
        "cpu": {
            "overall_percent": 12.5,
            "per_core_percent": [10.0, 15.0],
            "logical_core_count": 2,
            "sampling_interval_ms": 200,
        },
        "memory": {
            "total_bytes": 1000,
            "used_bytes": 500,
            "available_bytes": 500,
            "free_bytes": 100,
            "used_percent": 50.0,
        },
        "disks": [
            {
                "mount_point": "/",
                "file_system": "apfs",
                "total_bytes": 1000,
                "available_bytes": 200,
                "used_bytes": 800,
                "used_percent": 80.0,
            }
        ],
        "processes": {"sorted_by": "cpu", "limit": 0, "items": []},
        "issues": [],
    }


def sample_process() -> dict[str, Any]:
    return {
        "pid": 123,
        "name": "example",
        "cpu_percent": 1.5,
        "memory_bytes": 4096,
        "status": "sleep",
        "run_time_seconds": 10,
        "executable_path_available": True,
    }


def tool_payload(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    if tool == "get_system_metrics":
        return sample_system_metrics()
    if tool == "list_top_processes":
        return {
            "sorted_by": arguments.get("sort", "cpu"),
            "limit": arguments.get("limit", 2),
            "items": [],
            "issues": [],
        }
    if tool == "inspect_process":
        process = sample_process()
        process["pid"] = arguments.get("pid", process["pid"])
        return process
    if tool == "get_disk_usage":
        return {"disks": sample_system_metrics()["disks"], "issues": []}
    if tool == "get_recent_incidents":
        return {"incidents": [], "total_known": 0}
    raise AssertionError(f"unexpected tool {tool}")


def slow_responder(delay: float) -> Responder:
    def respond(method: str, path: str, body: bytes) -> tuple[int, bytes, str]:
        time.sleep(delay)
        return json_response(200, {})

    return respond
