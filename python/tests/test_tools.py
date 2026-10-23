from __future__ import annotations

import json
import unittest

from agent_fakes import FakeHadesClient, tool_specs
from hades_agent.errors import (
    ToolAllowlistError,
    ToolArgumentError,
    ToolCatalogError,
    ToolServiceError,
)
from hades_agent.tools import EXPECTED_TOOLS, DiagnosticToolbox
from hades_client import AgentConnectionError, ProtocolError, RemoteToolError, ToolSpec


class ToolboxTests(unittest.TestCase):
    def test_discovers_exactly_the_allowlist_and_ignores_extras(self) -> None:
        client = FakeHadesClient(tools=tool_specs(extra=True))
        toolbox = DiagnosticToolbox.from_client(client)
        self.assertEqual(toolbox.allowed_names, EXPECTED_TOOLS)
        names = tuple(tool.name for tool in toolbox.langchain_tools)
        self.assertEqual(names, EXPECTED_TOOLS)
        self.assertNotIn("delete_everything", names)
        with self.assertRaises(ToolAllowlistError):
            toolbox.execute("delete_everything", {})

    def test_missing_required_tool_fails_at_construction(self) -> None:
        client = FakeHadesClient(tools=tool_specs()[:2])
        with self.assertRaises(ToolCatalogError):
            DiagnosticToolbox.from_client(client)

    def test_unsupported_schema_versions_fail_at_construction(self) -> None:
        specs = tool_specs()
        specs[0] = ToolSpec(
            name=specs[0].name,
            description=specs[0].description,
            input_schema=specs[0].input_schema,
            input_schema_version=2,
            output_schema_version=1,
        )
        with self.assertRaises(ToolCatalogError):
            DiagnosticToolbox.from_client(FakeHadesClient(tools=specs))

    def test_discovery_failures_are_terminal_service_errors(self) -> None:
        client = FakeHadesClient(failures={"list_tools": AgentConnectionError("down")})
        with self.assertRaises(ToolServiceError):
            DiagnosticToolbox.from_client(client)

        client = FakeHadesClient(failures={"list_tools": ProtocolError("bad json")})
        with self.assertRaises(ToolServiceError):
            DiagnosticToolbox.from_client(client)

    def test_every_registered_tool_dispatches_with_validated_arguments(self) -> None:
        client = FakeHadesClient()
        toolbox = DiagnosticToolbox.from_client(client)

        metrics = toolbox.execute("get_system_metrics", {})
        self.assertTrue(metrics.ok)
        payload = json.loads(metrics.output or "{}")
        self.assertEqual(payload["tool"], "get_system_metrics")
        self.assertEqual(payload["data"]["cpu"]["overall_percent"], 12.5)

        processes = toolbox.execute("list_top_processes", {"sort": "memory", "limit": 2})
        self.assertEqual(processes.arguments, {"sort": "memory", "limit": 2})
        self.assertTrue(processes.ok)

        inspected = toolbox.execute("inspect_process", {"pid": 321})
        self.assertTrue(inspected.ok)
        self.assertEqual(json.loads(inspected.output or "{}")["data"]["pid"], 321)

        self.assertTrue(toolbox.execute("get_disk_usage", {}).ok)
        self.assertTrue(toolbox.execute("get_recent_incidents", {"limit": 3}).ok)
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "list_tools",
                "get_system_metrics",
                "list_top_processes",
                "inspect_process",
                "get_disk_usage",
                "get_recent_incidents",
            ],
        )

    def test_invalid_arguments_never_reach_the_service(self) -> None:
        client = FakeHadesClient()
        toolbox = DiagnosticToolbox.from_client(client)
        client.calls.clear()
        for name, arguments in (
            ("get_system_metrics", {"extra": True}),
            ("list_top_processes", {"limit": 0}),
            ("list_top_processes", {"sort": "disk"}),
            ("inspect_process", {"pid": 0}),
            ("inspect_process", {}),
            ("get_recent_incidents", {"limit": 101}),
        ):
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(ToolArgumentError):
                    toolbox.execute(name, arguments)
        self.assertEqual(client.calls, [])

    def test_remote_tool_errors_become_structured_tool_failures(self) -> None:
        client = FakeHadesClient(
            failures={
                "inspect_process": RemoteToolError(
                    "process_unavailable", "process is gone", http_status=404
                )
            }
        )
        toolbox = DiagnosticToolbox.from_client(client)
        execution = toolbox.execute("inspect_process", {"pid": 999})
        self.assertFalse(execution.ok)
        self.assertEqual(execution.error["code"], "process_unavailable")  # type: ignore[index]
        payload = json.loads(execution.output or "{}")
        self.assertFalse(payload["ok"])

    def test_transport_and_protocol_failures_are_terminal(self) -> None:
        for error in (AgentConnectionError("down"), ProtocolError("bad response")):
            with self.subTest(error=type(error).__name__):
                client = FakeHadesClient(failures={"get_system_metrics": error})
                toolbox = DiagnosticToolbox.from_client(client)
                with self.assertRaises(ToolServiceError):
                    toolbox.execute("get_system_metrics", {})

    def test_langchain_tools_use_pydantic_argument_schemas(self) -> None:
        toolbox = DiagnosticToolbox.from_client(FakeHadesClient())
        tools = {tool.name: tool for tool in toolbox.langchain_tools}
        schema = tools["list_top_processes"].args_schema
        self.assertIsNotNone(schema)
        assert schema is not None
        self.assertEqual(schema.model_json_schema()["properties"]["limit"]["maximum"], 100)


if __name__ == "__main__":
    unittest.main()
