# Hades Python packages

This directory contains the Python side of Hades:

- `hades_client` (M3): a typed, validated client for the Rust loopback
  diagnostic bridge.
- `hades_agent` (M4): a bounded local diagnostic agent using LangChain,
  Ollama, and Pydantic.

The packages require Python 3.10+ and the Rust service for real diagnostics.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

This installs `hades-agent`, `langchain-core`, `langchain-ollama`, `ollama`,
`pydantic`, and the `ruff` development tool.

## Run

Start Ollama and the Rust service:

```sh
ollama serve
ollama pull qwen2.5:7b
../target/release/os-agent serve --bind 127.0.0.1:8765
```

Then:

```sh
.venv/bin/hades-agent ask "Why is memory usage high?"
.venv/bin/hades-agent incident --latest
```

Configuration is via `HADES_MODEL`, `HADES_OLLAMA_URL`, `HADES_BRIDGE_URL`, and
the limits documented in the repository README. Both endpoints must be loopback.
All model inference is intended to run locally through Ollama; there is no
remote fallback.

The client can be used separately:

```python
from hades_client import HadesClient

client = HadesClient("http://127.0.0.1:8765", timeout=10)
print(client.get_system_metrics())
```

## Tests

From the repository root:

```sh
python/.venv/bin/ruff check python
PYTHONPATH=python/src python/.venv/bin/python -m unittest discover -s python/tests -v
```

The default suite uses fake HTTP services and fake model responses, and starts
the real Rust binary for bridge integration tests. It does not require Ollama.
The optional live test requires Ollama and a downloaded tool-calling model:

```sh
HADES_OLLAMA_INTEGRATION=1 \
  PYTHONPATH=python/src python/.venv/bin/python -m unittest discover \
  -s python/tests -p 'test_ollama_integration.py' -v
```

See the repository root `README.md` for architecture, protocol, security, and
limitations.
