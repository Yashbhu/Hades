from __future__ import annotations

import unittest
from pathlib import Path

from hades_agent.config import AgentConfig
from hades_agent.errors import ConfigurationError, ModelNotFoundError, ModelUnavailableError
from hades_agent.model import OllamaModelProvider


class ConfigTests(unittest.TestCase):
    def test_defaults_are_local_and_bounded(self) -> None:
        config = AgentConfig()
        self.assertEqual(config.ollama_url, "http://127.0.0.1:11434")
        self.assertEqual(config.bridge_url, "http://127.0.0.1:8765")
        self.assertLessEqual(config.max_tool_calls, 32)
        self.assertLessEqual(config.max_iterations, 16)
        self.assertLessEqual(config.max_steps, 64)
        self.assertLessEqual(config.max_consecutive_tool_failures, 10)
        self.assertTrue(str(config.checkpoint_db).endswith("investigations.sqlite"))
        self.assertTrue(str(config.incident_db).endswith("incident_history.sqlite"))
        self.assertLessEqual(config.max_history_results, 20)

    def test_environment_values_override_defaults(self) -> None:
        config = AgentConfig.from_env(
            {
                "HADES_MODEL": "llama3.1:8b",
                "HADES_OLLAMA_URL": "http://localhost:11434",
                "HADES_BRIDGE_URL": "http://127.0.0.1:9999",
                "HADES_MAX_TOOL_CALLS": "4",
                "HADES_MAX_ITERATIONS": "3",
                "HADES_MAX_STEPS": "5",
                "HADES_MAX_CONSECUTIVE_TOOL_FAILURES": "2",
                "HADES_CHECKPOINT_DB": "/tmp/hades-test.sqlite",
                "HADES_INCIDENT_DB": "/tmp/hades-incidents.sqlite",
                "HADES_MAX_HISTORY_RESULTS": "3",
            }
        )
        self.assertEqual(config.model, "llama3.1:8b")
        self.assertEqual(config.ollama_url, "http://localhost:11434")
        self.assertEqual(config.bridge_url, "http://127.0.0.1:9999")
        self.assertEqual(config.max_tool_calls, 4)
        self.assertEqual(config.max_iterations, 3)
        self.assertEqual(config.max_steps, 5)
        self.assertEqual(config.max_consecutive_tool_failures, 2)
        self.assertEqual(config.checkpoint_db, Path("/tmp/hades-test.sqlite"))
        self.assertEqual(config.incident_db, Path("/tmp/hades-incidents.sqlite"))
        self.assertEqual(config.max_history_results, 3)

    def test_non_loopback_endpoints_are_rejected(self) -> None:
        for key, value in (
            ("ollama_url", "http://203.0.113.10:11434"),
            ("bridge_url", "https://127.0.0.1:8765"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ConfigurationError):
                    AgentConfig().merged(**{key: value})

    def test_invalid_limits_are_rejected(self) -> None:
        for key, value in (
            ("max_tool_calls", 0),
            ("max_tool_calls", 100),
            ("max_iterations", 0),
            ("max_steps", 0),
            ("max_steps", 100),
            ("max_consecutive_tool_failures", 0),
            ("tool_timeout", 0),
            ("max_history_results", 0),
            ("max_history_results", 100),
            ("max_history_chars", 100),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ConfigurationError):
                    AgentConfig().merged(**{key: value})


class FakeOllamaClient:
    def __init__(self, models: list[str] | None = None, error: Exception | None = None) -> None:
        self.models = models or []
        self.error = error

    def list(self):
        if self.error:
            raise self.error
        return {"models": [{"model": name} for name in self.models]}


def provider(client: FakeOllamaClient) -> OllamaModelProvider:
    config = AgentConfig()
    return OllamaModelProvider(config, client_factory=lambda _url, _timeout: client)


class ModelProviderTests(unittest.TestCase):
    def test_preflight_accepts_installed_model(self) -> None:
        OllamaModelProvider(
            AgentConfig(), client_factory=lambda _u, _t: FakeOllamaClient(["qwen2.5:7b"])
        ).preflight()

    def test_missing_model_has_actionable_error(self) -> None:
        with self.assertRaises(ModelNotFoundError) as raised:
            provider(FakeOllamaClient(["llama3.1:8b"])).preflight()
        self.assertIn("ollama pull qwen2.5:7b", str(raised.exception))

    def test_unavailable_daemon_has_actionable_error(self) -> None:
        with self.assertRaises(ModelUnavailableError) as raised:
            provider(FakeOllamaClient(error=ConnectionError("refused"))).preflight()
        self.assertIn("ollama serve", str(raised.exception))

    def test_untagged_model_matches_latest(self) -> None:
        config = AgentConfig(model="qwen2.5")
        OllamaModelProvider(
            config, client_factory=lambda _u, _t: FakeOllamaClient(["qwen2.5:latest"])
        ).preflight()

    def test_chat_model_is_langchain_ollama_and_supports_tools(self) -> None:
        config = AgentConfig()
        provider = OllamaModelProvider(
            config,
            client_factory=lambda _u, _t: FakeOllamaClient(["qwen2.5:7b"]),
        )
        model = provider.create_chat_model()
        from langchain_ollama import ChatOllama

        self.assertIsInstance(model, ChatOllama)
        self.assertEqual(model.model, "qwen2.5:7b")
        self.assertIsNotNone(model.bind_tools([]))


if __name__ == "__main__":
    unittest.main()
