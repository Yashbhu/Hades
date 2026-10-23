"""Local Ollama model construction and availability checks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import ollama
from langchain_ollama import ChatOllama

from .config import AgentConfig
from .errors import ModelNotFoundError, ModelUnavailableError

ClientFactory = Callable[[str, float], Any]


def _default_client(url: str, timeout: float) -> Any:
    return ollama.Client(host=url, timeout=timeout)


class OllamaModelProvider:
    """Create a local chat model without any remote fallback."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        client_factory: ClientFactory = _default_client,
    ) -> None:
        self.config = config
        self._client_factory = client_factory

    def preflight(self) -> None:
        """Verify the daemon is reachable and the configured model is installed."""
        try:
            client = self._client_factory(self.config.ollama_url, self.config.model_timeout)
            response = client.list()
        except Exception as error:
            raise ModelUnavailableError(
                f"Ollama is unavailable at {self.config.ollama_url}. "
                "Start it with `ollama serve` and try again."
            ) from error

        installed = _installed_models(response)
        if not any(_model_matches(self.config.model, name) for name in installed):
            available = ", ".join(installed[:8]) if installed else "none"
            raise ModelNotFoundError(
                f"model '{self.config.model}' is not installed. "
                f"Run `ollama pull {self.config.model}`. "
                f"Installed models: {available}."
            )

    def create_chat_model(self) -> ChatOllama:
        """Return a configured LangChain chat model after a local preflight."""
        self.preflight()
        try:
            return ChatOllama(
                model=self.config.model,
                base_url=self.config.ollama_url,
                temperature=self.config.temperature,
                seed=self.config.seed,
                num_ctx=self.config.num_ctx,
                num_predict=self.config.num_predict,
                client_kwargs={"timeout": self.config.model_timeout},
                validate_model_on_init=False,
            )
        except Exception as error:
            raise ModelUnavailableError(
                f"could not initialize local model '{self.config.model}'"
            ) from error


def _installed_models(response: Any) -> list[str]:
    models = getattr(response, "models", None)
    if models is None and isinstance(response, dict):
        models = response.get("models", [])
    names: list[str] = []
    for model in models or []:
        name = getattr(model, "model", None)
        if name is None and isinstance(model, dict):
            name = model.get("model") or model.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _model_matches(configured: str, installed: str) -> bool:
    if configured == installed:
        return True
    if ":" not in configured:
        return installed == f"{configured}:latest"
    return False
