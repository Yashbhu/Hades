# Hades

Local-first system health monitoring and diagnosis for a future AI OS agent.

Hades is built in milestones. The current repository implements:

| Milestone | Status | What it adds |
| --- | --- | --- |
| **M1** | Complete | Rust CPU, memory, disk, and process collection; `os-agent status` and `processes` |
| **M2** | Complete | Background monitoring, bounded rolling history, deterministic anomaly detection, incident lifecycle, JSON Lines events, `os-agent monitor` |
| **M3** | Complete | Loopback JSON HTTP bridge exposing five registered read-only diagnostic tools, plus a typed Python client |
| **M4** | Complete | Local Ollama + LangChain diagnostic agent with bounded tool calling and Pydantic-validated reports; the optional live Ollama test requires a local model |
| **M5** | Complete | LangGraph investigation workflow with typed state, explicit transitions, SQLite checkpoints, resumable investigations, and deterministic evidence/termination handling |
| **M6** | Complete | Durable, versioned SQLite incident history; deterministic historical retrieval; verification/resolution tracking; bounded history context in the M5 graph |

M6 adds structured local incident memory, not a vector database, RAG framework,
fine-tuning, automatic remediation, multi-agent workflow, dashboard, or cloud
storage. There is no remote LLM provider in this repository.

> **Implementation note:** the M4 code in this repository uses Ollama
> (`ChatOllama`) for local inference, not a direct `llama.cpp` binding. M5 and M6
> keep that working inference layer; LangGraph controls workflow state and SQLite
> stores checkpoints and incident history. No hosted provider or remote fallback
> is introduced.

## Design principles

- **Local first.** The Rust bridge binds only to loopback. The Python agent
  accepts only loopback Ollama and bridge endpoints. There is no fallback to a
  hosted model.
- **Read-only diagnostics.** The only callable operations are explicitly
  registered. There is no shell tool, command endpoint, executable-path input,
  or filesystem mutation.
- **Facts are separate from hypotheses.** M2 incidents contain measurements and
  derived values. M4/M5 reports and workflow state keep tool observations
  separate from uncertain causes. M6 historical records are labelled as prior
  context and are never merged into the current evidence list.
- **Missing data is not zero.** Unavailable metrics are `null` or omitted, with
  an issue entry. Monitoring and tools never invent values.
- **Bounded state and bounded work.** Runtime history, incidents, graph steps,
  tool calls, evidence summaries, checkpoint transitions, historical retrieval,
  and prompt context are capped; the bridge limits request bodies and result
  sizes.
- **Durable but local.** M5 checkpoints and its write-ahead tool journal live in
  a local SQLite database by default. M6 incident history is a separate local
  SQLite database with an explicit schema version. A resumed investigation
  reuses completed read-only results; an uncertain in-flight call is repeated
  only as an explicit fresh read-only check.
- **Deterministic detection.** Anomaly detection is ordinary Rust code, not an
  LLM.

## Platform support

- Built, tested, and manually verified on **macOS 15.5 (arm64)**.
- Rust uses `sysinfo`'s cross-platform APIs. Linux is the target platform, but
  **Linux has not been verified in this repository**.
- Windows is not verified.
- The Rust agent does not require root. Processes or fields hidden from the
  current user are reported as unavailable, not collected by escalating
  privileges.
- Python 3.10 or newer is required.

## Quick start

### 1. Build the Rust agent

```sh
cargo build --release
./target/release/os-agent --help
```

### 2. Install the Python packages

The Python package contains the M3 client (`hades_client`) and the
M4/M5 agent (`hades_agent`).

```sh
cd python
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd ..
```

### 3. Start Ollama and obtain a tool-calling model

Install Ollama from <https://ollama.com>, then:

```sh
ollama serve
ollama pull qwen2.5:7b
```

`qwen2.5:7b` is the documented default because it supports tool calling and runs
on a typical developer machine. Any installed Ollama model with reliable tool
calling can be selected with `--model` or `HADES_MODEL`; report generation also
requires structured-output support.

### 4. Start the Rust diagnostic service

```sh
./target/release/os-agent serve --bind 127.0.0.1:8765
```

`serve` starts the same monitoring engine as `monitor` and exposes its incident
state and fresh diagnostics over loopback HTTP.

### 5. Ask a question

```sh
python/.venv/bin/hades-agent ask "Why is memory usage high?"
```

Or investigate the newest incident known to the Rust monitor:

```sh
python/.venv/bin/hades-agent incident --latest
```

The Python entry point is named `hades-agent`, not `os-agent`, so the existing
Rust CLI commands remain unchanged.

## Architecture

```text
┌───────────────────────────── local machine ─────────────────────────────┐
│                                                                         │
│  Rust: os-agent                                                         │
│  ├── sysinfo Collector ──► Snapshot                                     │
│  ├── Monitor ──► incidents/events ──► bounded in-memory state           │
│  ├── CLI: status / processes / monitor / serve                          │
│  └── HTTP bridge (127.0.0.1 only)                                       │
│         GET  /v1/tools                                                  │
│         POST /v1/tools/call                                             │
│                                                                         │
│  Python: hades_client                                                   │
│  └── typed HTTP client, response validation, timeouts                   │
│                                                                         │
│  Python: hades_agent                                                    │
│  ├── DiagnosticToolbox: allowlist + Pydantic argument validation        │
│  ├── LangGraph: initialize → retrieve_history → plan → validate         │
│  │              → execute → assess → finalize/terminate, bounded loops   │
│  ├── LangChain ChatOllama: local planning and report generation         │
│  ├── SQLite checkpoints + read-only tool journal                        │
│  ├── SQLite incident memory + deterministic historical retrieval        │
│  └── Pydantic DiagnosticReport and workflow outcome models              │
│                                                                         │
│  Ollama (127.0.0.1:11434) ──► local tool-calling model                  │
└─────────────────────────────────────────────────────────────────────────┘
```

Rust collects and enforces access. Python never inspects the system directly
and never executes fallback shell commands.

### Rust source layout

```text
src/
├── main.rs              Rust CLI (status, processes, monitor, serve)
├── lib.rs               Public library surface
├── config.rs            Validated monitoring thresholds and limits
├── history.rs           Bounded metric history and trend calculations
├── incident.rs          Incident, evidence, lifecycle types
├── events.rs            Typed JSON Lines events
├── monitor.rs           Pure detector + cancellable background engine
├── snapshot.rs          Snapshot and live Collector
├── metrics/             CPU, memory, disk, process metric types
├── report.rs            Human-readable rendering
└── bridge/
    ├── mod.rs           Loopback HTTP service and request handling
    ├── protocol.rs      Versioned request/response/error envelopes
    └── tools.rs         Registered tools and bounded incident mirror
tests/
├── cli.rs               M1/M2 CLI integration tests
└── http_api.rs          M3 HTTP integration tests
```

### Python source layout

```text
python/src/
├── hades_client/        M3 typed client and validated result models
└── hades_agent/
    ├── config.py        Environment/CLI configuration, loopback enforcement
    ├── tools.py         Exact five-tool allowlist and argument schemas
    ├── model.py         Ollama preflight and ChatOllama construction
    ├── agent.py         M4 bounded loop, prompts, report validation
    ├── workflow.py      M5/M6 LangGraph state, transitions, checkpoints
    ├── incident_memory.py  M6 versioned SQLite history and retrieval
    ├── models.py        Pydantic incident/report/trace models
    ├── cli.py           `hades-agent` CLI (investigate/status/memory)
    └── errors.py        Actionable error types
```

## Rust CLI

### M1: one-shot inspection

```text
os-agent status [--json]
os-agent processes [--sort cpu|memory] [--limit 1..100]
```

`status` collects CPU, memory, disks, and the top 10 processes. `processes`
prints a bounded process table. Process CPU is percent of one logical core and
may exceed 100% for multi-threaded processes.

### M2: continuous monitoring

```text
os-agent monitor [--interval 5s] [--json-lines] [--duration 30s]
                 [--cpu-threshold 90] [--cpu-recovery 85] [--cpu-critical 95]
                 [--cpu-duration 30s]
                 [--memory-threshold 10] [--memory-recovery 15] [--memory-critical 5]
                 [--memory-duration 30s]
                 [--disk-threshold 10] [--disk-recovery 15] [--disk-critical 3]
                 [--history 60]
```

Defaults:

| Setting | Default |
| --- | --- |
| Sampling interval | `5s` |
| Rolling history | 60 samples (5 minutes at 5s) |
| Retained recovered incidents | 100 |
| CPU trigger / recovery / critical | `90% / 85% / 95%` |
| CPU sustained | 30 seconds (at least 6 valid samples at 5s) |
| Memory available trigger / recovery / critical | `10% / 15% / 5%` |
| Memory sustained | 30 seconds |
| Disk available trigger / recovery / critical | `10% / 15% / 3%` |
| Disk sustained | Immediate per mount (no transient timer) |

Memory pressure uses **available** memory, not `used`, because reclaimable cache
can make `used` misleading. Disk detection uses available space per mounted
filesystem and never recursively scans directories.

Monitoring never scans processes. This keeps the continuous path lightweight;
process inspection remains available through M1 and M3 tools.

#### Incident lifecycle

States are `active`, `acknowledged`, and `recovered`. Acknowledging an incident
does not mean it recovered. Recovery requires the metric to cross the hysteresis
threshold (for example CPU at or below 85% after being at or above 90%).

- One incident per condition per mount. Duplicate incidents are not created in
  one cycle.
- IDs are stable for the life of an open incident; a new incident gets a new ID
  after recovery.
- A recovery must cross the recovery threshold; values between trigger and
  recovery do not flap the state.
- Missing metrics are ignored for detection and are never treated as zero.
- Recovered incidents are retained up to the configured bound. Open incidents
  are not dropped.
- Disk incidents for a mount that disappears remain open; disappearance is not
  treated as recovery. This is a documented limitation.

#### Events

`--json-lines` prints one JSON object per line on stdout and nothing else. Human
output goes to stdout only when `--json-lines` is absent. Event types are
`incident_started`, `incident_updated`, `incident_recovered`, and
`collection_error`. Collection errors are emitted only when the issue set
changes, not every cycle.

Actual example (one record, unedited):

```json
{"event":"incident_started","timestamp":"2026-10-23T00:28:08.784021Z","incident":{"id":"inc-000001","kind":"low_disk_space","subject":"/Volumes/Dia","severity":"critical","state":"active","started_at":"2026-10-23T00:28:08.784021Z","updated_at":"2026-10-23T00:28:08.784021Z","last_observed_at":"2026-10-23T00:28:08.784021Z","recovered_at":null,"summary":"Filesystem '/Volumes/Dia' has 0.2% available, at or below the 10.0% threshold","evidence":{"kind":"low_disk_space","observed":{"mount_point":"/Volumes/Dia","file_system":"hfs","total_bytes":1439592448,"available_bytes":2228224,"used_bytes":1437364224,"available_percent":0.15478158440575537},"derived":{"available_threshold_percent":10.0,"recovery_available_percent":15.0,"critical_available_percent":3.0}}}}
```

Incident evidence separates `observed` measurements from `derived` thresholds,
averages, counts, durations, and trends. The detector does not claim a root
cause.

### M3: diagnostic service

```text
os-agent serve [--bind 127.0.0.1:8765] [--interval 5s] [--duration 30s]
```

Only loopback addresses are accepted. `--duration` is useful for bounded tests;
the default is to run until Ctrl+C.

Endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/tools` | Discovery: names, descriptions, input schemas, schema versions |
| `POST` | `/v1/tools/call` | Invoke one registered tool with a versioned envelope |

There is deliberately no endpoint for commands, executables, scripts, file
writes, or arbitrary paths.

## M3 protocol

Protocol version: `1`. Tool output schemas: version `1`. Request bodies are
limited to 64 KiB by default (configurable within 1 KiB–1 MiB in the library).

Request:

```json
{
  "protocol_version": 1,
  "request_id": "req-1",
  "tool": "inspect_process",
  "arguments": { "pid": 123 }
}
```

Success:

```json
{
  "protocol_version": 1,
  "request_id": "req-1",
  "status": "ok",
  "result": {
    "schema_version": 1,
    "data": {
      "pid": 123,
      "name": "example",
      "cpu_percent": 1.5,
      "memory_bytes": 4096,
      "status": "sleep",
      "run_time_seconds": 10,
      "executable_path_available": true
    }
  }
}
```

Structured error:

```json
{
  "protocol_version": 1,
  "request_id": "req-2",
  "status": "error",
  "error": {
    "code": "unknown_tool",
    "message": "unknown tool 'run_shell'",
    "details": { "tool": "run_shell" }
  }
}
```

`request_id` is echoed when it could be parsed. Malformed JSON cannot supply one,
so it is `null`. Unknown tools are never dispatched.

### Tools and contracts

All tools are read-only. Names are exact and stable.

| Tool | Input | Output (`data`) |
| --- | --- | --- |
| `get_system_metrics` | `{}` | One M1 snapshot: `schema_version`, `collected_at`, nullable `cpu`/`memory`, `disks`, `processes`, `issues`. Process collection is disabled, so `processes.items` is empty and `limit` is `0`. |
| `list_top_processes` | `{"sort":"cpu"|"memory","limit":1..100}`; defaults `cpu`, `10` | `sorted_by`, `limit`, `items` (`pid`, `name`, `cpu_percent`, `memory_bytes`), `issues`. |
| `inspect_process` | `{"pid":1..4294967295}` | `pid`, `name`, `cpu_percent`, `memory_bytes`, `status`, `run_time_seconds`, `executable_path_available`. Command line, environment, user, and executable path are never returned. |
| `get_disk_usage` | `{}` | `disks` (mount point, filesystem, total/available/used bytes, used percent) and `issues`. No recursive scan. |
| `get_recent_incidents` | `{"limit":1..100}`; default `20` | `incidents` (full state, timestamps, severity, summary, evidence) and `total_known`. Empty list is valid. |

Stable error codes include `invalid_request`, `unsupported_protocol_version`,
`unknown_tool`, `invalid_arguments`, `process_unavailable`,
`request_too_large`, `unsupported_media_type`, `method_not_allowed`,
`not_found`, and `internal_error`.

HTTP status mapping:

| Condition | Status |
| --- | --- |
| Valid tool success | `200` |
| Malformed JSON/envelope or unsupported protocol | `400` |
| Unknown tool, missing process, unknown path | `404` |
| Wrong method | `405` |
| Body too large | `413` |
| Non-JSON content type | `415` |
| Invalid arguments | `422` |
| Internal failure | `500` |

### Transport rationale and limits

- **Loopback HTTP + JSON** was chosen because the project had no existing IPC
  layer, Python standard-library HTTP support is sufficient, and the protocol is
  easy to inspect. A custom binary IPC layer or gRPC would add dependencies and
  complexity without a demonstrated need.
- The server polls for shutdown at least every 100 ms. The monitor uses short
  cancellation slices, so shutdown is prompt even with a long sampling interval.
- Tools perform one bounded collection cycle (about 200–300 ms on the verified
  host) or a single process refresh. `get_recent_incidents` reads bounded
  in-memory state (sub-millisecond in local measurements). There are no
  unbounded loops or retries server-side.
- Request bodies and response result sets are bounded. Client timeouts are
  configurable; the Python client defaults to 10 seconds per tool request.
- Client disconnects do not crash the service. Requests are processed
  sequentially.

### Security assumptions

Loopback binding is **not** a complete security boundary. Any local process can
connect to the port. This milestone has:

- no authentication or authorization;
- no TLS;
- no per-user access control;
- no request logging of process data;
- no sensitive process fields (command line, environment, executable paths).

Do not bind to a non-loopback interface; the service and client reject it. If
stronger isolation is needed later, add OS-level access controls or authenticated
local transport. The service is read-only but can disclose system resource and
process-name information to any local user who can reach the port.

## Python client (M3)

```python
from hades_client import HadesClient, RemoteToolError

client = HadesClient("http://127.0.0.1:8765", timeout=10)

tools = client.list_tools()
metrics = client.get_system_metrics()
processes = client.list_top_processes(sort="memory", limit=5)
process = client.inspect_process(pid=123)
disks = client.get_disk_usage()
incidents = client.get_recent_incidents(limit=20)

try:
    client.inspect_process(pid=999999)
except RemoteToolError as error:
    print(error.code, error.message)
```

The client validates protocol version, request IDs, response shapes, and tool
payload fields. It distinguishes:

- connection failures (`AgentConnectionError`);
- timeouts (`AgentTimeoutError`);
- protocol mismatches and malformed responses (`ProtocolError`,
  `ProtocolVersionError`);
- structured tool failures (`RemoteToolError`).

It does not retry requests and does not fall back to local commands. Methods are
synchronous and stateless; the client can later be adapted as a LangChain tool
without changing the Rust protocol.

## M4 diagnostic agent

### Model configuration

All model traffic is intended for a local Ollama instance. A non-loopback
Ollama URL is rejected.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `HADES_MODEL` | `qwen2.5:7b` | Ollama model name |
| `HADES_OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint; `OLLAMA_BASE_URL` is a fallback |
| `HADES_BRIDGE_URL` | `http://127.0.0.1:8765` | Rust service endpoint |
| `HADES_MODEL_TIMEOUT` | `120` | Model request timeout, seconds |
| `HADES_TOOL_TIMEOUT` | `10` | Tool request timeout, seconds |
| `HADES_MAX_TOOL_CALLS` | `8` | Tool-call budget per investigation (1–32) |
| `HADES_MAX_ITERATIONS` | `6` | Legacy M4 model turns per investigation (1–16) |
| `HADES_MAX_STEPS` | `12` | M5 graph planning steps per investigation (1–64) |
| `HADES_MAX_CONSECUTIVE_TOOL_FAILURES` | `3` | Stop after this many consecutive rejected/failed actions (1–10) |
| `HADES_CHECKPOINT_DB` | `~/.hades/investigations.sqlite` | Local SQLite checkpoint and tool-journal path |
| `HADES_INCIDENT_DB` | `~/.hades/incident_history.sqlite` | Local SQLite incident-history path |
| `HADES_MAX_HISTORY_RESULTS` | `5` | Historical incidents supplied to planning (1–20) |
| `HADES_MAX_HISTORY_CHARS` | `8000` | Bounded serialized historical context (1,000–64,000 characters) |
| `HADES_MAX_CONTEXT_CHARS` | `24000` | Bounded planner context (4,096–131,072 characters) |
| `HADES_NUM_CTX` | `16384` | Ollama context window |
| `HADES_NUM_PREDICT` | `1024` | Maximum generated tokens per response |
| `HADES_TEMPERATURE` | `0.0` | Sampling temperature |
| `HADES_SEED` | `0` | Sampling seed |

CLI flags (`--model`, `--ollama-url`, `--bridge-url`, `--max-tool-calls`, and so
on) override environment values and are validated again.

### Investigation flow

1. The CLI loads a question, or a structured incident from `--file`/`--latest`.
2. It discovers M3 tools and checks that all five expected names exist. Extra
   server tools are not exposed.
3. The agent binds only those five LangChain `StructuredTool` schemas to the
   local chat model and supplies a diagnostic system prompt.
4. The model may request tools. **Application code**, not the prompt, enforces
   the allowlist and re-validates arguments with Pydantic before dispatch.
5. Each successful or failed call is executed through `hades_client`; results are
   returned to the model as untrusted data. Tool results are truncated at a
   bounded size (4,096 characters by default) with an explicit marker; the trace
   records whether truncation occurred.
6. The original M4 loop stops when the model returns no tool call, the
   tool-call budget is exhausted, the iteration limit is reached, or a
   transport/protocol failure is terminal. It remains available as
   `DiagnosticAgent` for callers and tests; the CLI uses the M5 LangGraph
   workflow, which reuses the same prompts and report validation.
7. A second structured-output call produces a Pydantic-validated
   `DiagnosticReport`. Evidence citations are checked against successful tool
   calls; invalid reports get one repair attempt.
8. The result includes the report plus an independent tool trace containing the
   actual arguments, outputs, errors, and durations.

The tool executor never constructs shell commands. Model-generated strings are
only used as validated tool arguments (`sort`, `limit`, `pid`).

### Report shape

`DiagnosticReport` contains:

- `summary`: what appears to be happening;
- `evidence[]`: `tool` plus a factual `finding`; only successful calls may be
  cited;
- `likely_causes[]`: hypotheses with confidence, rationale, and indices into the
  evidence list;
- `confidence`: qualitative level (`low`/`medium`/`high`) and explanation;
- `next_checks[]`: additional read-only checks;
- `data_gaps[]`: what the investigation could not obtain.

The observations (`evidence`) and hypotheses (`likely_causes`) are separate
fields on purpose. A hypothesis is rejected unless at least one observation is
present, and every evidence item must reference a successful tool call. This
does not prove that every phrase is accurate; the tool trace remains the ground
truth for what was actually measured.

### CLI examples

```sh
hades-agent ask "Why is memory usage high?"
hades-agent ask "Is the low disk space on /Volumes/Dia critical?" --json
hades-agent incident --latest
hades-agent incident --file /tmp/incident.json
hades-agent ask "What changed?" --model llama3.1:8b --max-tool-calls 4
```

Illustrative text output (model-dependent; this is not a live transcript):

```text
Diagnosis
=========
Available memory is low while disk capacity is stable.

Observed evidence
-----------------
1. [get_system_metrics] Memory available was 8.2% of 16 GiB.
2. [list_top_processes] The five largest resident-memory processes were
   browser and editor processes.

Likely causes (hypotheses)
--------------------------
1. Several large applications are resident at the same time (confidence: medium)
   The snapshot shows high resident memory in user applications, but no
   historical baseline is available.
   Supporting evidence: 1, 2

Confidence: medium
Only one snapshot was collected; process memory can be shared.

Next read-only checks
---------------------
- Collect metrics over several minutes to see whether available memory recovers.

Note: hypotheses are model-generated and may be wrong. Verify them before
acting. Hades performs no remediation.
```

JSON output contains `question`, `incident`, `report`, `tool_calls`, `model`,
`iterations`, `budget_exhausted`, `iteration_limit_reached`, and `limits`.

### Privacy and untrusted input

- Prompts, incident text, and tool results are sent only to the configured
  loopback Ollama endpoint and the loopback Rust bridge. This project does not
  intentionally send prompts to a remote provider and has no remote fallback.
- Ollama may download a model when the user runs `ollama pull`; that is a
  separate network action. Operating-system, proxy, or Ollama configuration
  could cause other traffic. Verify with your own network monitoring if needed;
  this README does not claim zero network traffic under all configurations.
- Incident text and tool output are treated as untrusted data in the prompt.
  Application code does not interpret them as instructions.
- Environment variables and secrets are never added to prompts.
- The model is not trusted. It can be wrong, overconfident, or manipulated by
  data it reads. Reports are advisory, not authoritative, and never trigger
  remediation.

## M5 stateful investigation workflow

M5 adds an explicit LangGraph workflow around the existing local inference and
read-only tools. It is intentionally a small graph, not a multi-agent system.
The M4 `DiagnosticAgent` remains available; the graph reuses its tool-binding,
prompt, and report-validation code.

### Workflow state

The checkpointed `InvestigationState` contains only the bounded information
needed to resume:

- a generated investigation ID and execution ID;
- the original question and optional incident context;
- the current stage;
- planning-step and tool-call counts;
- associated incident IDs;
- successful evidence records (tool, validated arguments, bounded result
  summary, collection time, investigation/incident IDs, call ID, duration, and
  whether a result came from a fresh check);
- structured tool/action errors with codes and whether a retry is plausible;
- current hypotheses, missing-evidence notes, detected contradictory repeated
  observations, and suggested next checks;
- the validated final report, if one exists;
- stop reason, termination status, and termination reason;
- bounded transition history and completed call IDs.

Tool outputs are summaries, not a full conversation history or a second copy of
every system snapshot. Model-generated hypotheses are stored separately from
evidence. The report schema is unchanged from M4.

### Graph and safety boundaries

```text
START
  │
initialize
  │
retrieve_history  (M6; empty when memory is unavailable)
  │
plan ───────────────► terminate
  │  (valid tool action)   (model/state error)
validate ──► execute ──► assess
  │                         │
  └──── invalid/failed ◄────┘
          action               │
                               ├──► plan (more evidence)
                               └──► finalize ──► terminate ──► END
```

- `plan` asks the local model for at most one next read-only tool call. If the
  model replies without a tool call, the graph finalizes.
- `validate` checks the tool name against the exact five-tool allowlist and
  re-validates arguments with the existing Pydantic schemas. No request is sent
  for an unknown tool or invalid arguments.
- `execute` is ordinary application code. It never delegates execution to the
  model, never constructs shell commands, and never exposes an unregistered
  tool.
- `assess` records evidence, failures, missing data, and repeated-observation
  disagreements. It does not treat model confidence as a calibrated
  probability.
- `finalize` calls the M4 structured report generator. Evidence citations must
  reference successful tool calls; hypotheses require observations. If the
  report cannot be validated, the graph returns an explicit inconclusive report
  with low confidence and data gaps.
- `terminate` always writes a final status and report, including for model or
  service failures.

The graph has hard limits on planning steps and tool-call attempts. A
`recursion_limit` is also set as a backstop. All edges and termination
conditions are explicit in `workflow.py`.

### Checkpointing and recovery

Checkpoints use `langgraph-checkpoint-sqlite` with a local SQLite database. The
LangGraph thread ID is the investigation ID; the state also carries an execution
ID and each outcome includes the latest checkpoint ID.

- Checkpoints are written after completed graph nodes.
- `hades-agent resume <id>` resumes from the latest checkpoint.
- Completed graph runs are not re-executed by `resume`.
- A separate SQLite write-ahead journal records each tool call as `started`,
  `completed`, or `failed`.
- If the process dies after a tool result is journaled but before the graph
  checkpoint, the journaled completed result is reused.
- If the journal shows an uncertain in-flight call, the graph performs a fresh
  read-only check and marks the evidence record `fresh_check`. It never assumes
  the previous call completed, and it never performs a mutating operation.
- A state schema version is stored in every checkpoint. Missing or unsupported
  versions, malformed reports, and unreadable SQLite files produce actionable
  `CheckpointError`/`CheckpointIncompatibleError` messages rather than an unsafe
  resume.
- The checkpoint directory is created with user-only permissions where the
  platform allows it. No checkpoint is sent over the network.

This milestone does not implement semantic memory or RAG. Checkpoints are
per-investigation recovery state; M6 adds separate structured incident memory.

### CLI

The existing CLI is extended rather than replaced:

```sh
# Start with a generated ID; the ID is printed in text and JSON output.
hades-agent ask "Why is memory usage high?"

# Use a stable ID and a custom local checkpoint database.
hades-agent ask "Why is CPU high?" --investigation-id inv-42 \
  --checkpoint-db ~/.hades/investigations.sqlite

# Investigate the newest incident, a file, or a retained incident ID.
hades-agent incident --latest
hades-agent incident --file /tmp/incident.json
hades-agent incident --id inc-123

# Inspect the saved stage without loading a model or the Rust service.
hades-agent status inv-42
hades-agent status inv-42 --json

# Resume after Ctrl+C, a crash, or a stop before finalization.
hades-agent resume inv-42

# Machine-readable output.
hades-agent ask "Check disk space" --json
```

Text output starts with the investigation ID, stage, termination status, and
reason, followed by the same evidence/hypothesis/confidence sections as M4.
JSON output includes the M5 fields (`investigation_id`, `execution_id`, `stage`,
`step_count`, `tool_call_count`, `evidence`, `tool_errors`, `hypotheses`,
`missing_evidence`, `contradictions`, `next_checks`, `termination_status`,
`termination_reason`, `checkpoint_id`, `next_nodes`, `resumable`,
`historical_incidents`, `memory_errors`, and `memory_status`) plus the
M4-compatible `report`, `model`, `limits`, `iterations`, `tool_calls`,
`budget_exhausted`, and `iteration_limit_reached` keys.

Exit codes: `0` for a completed or honestly inconclusive investigation, `2` for
a configuration/checkpoint error or an unrecoverable model/service failure, and
`130` for an interrupt. A model inference failure still produces a low-confidence
inconclusive report instead of fabricated evidence.

### Security and trust boundaries

- Only the five registered read-only tools can be called. The model receives
  their schemas, but `DiagnosticToolbox.validate_call` and `execute` enforce the
  allowlist and arguments again.
- Model-generated strings are used only as validated `sort`, `limit`, or `pid`
  arguments. There is no command, path, environment, or filesystem-write tool.
- Tool output, incident text, and checkpointed model text are marked as
  untrusted data in prompts. They are not interpreted as application
  instructions.
- Persisted state is local by default. It can contain process names and resource
  measurements, so treat the checkpoint file as sensitive user data.
- The Rust bridge still has no authentication or TLS; loopback is not a
  complete security boundary. See the M3 limitations.
- The model remains fallible. Reports are advisory; the tool trace and evidence
  records are the ground truth for what was actually collected.

### Migration

M4 had no durable investigation checkpoints, so there is no M4-to-M5 checkpoint
migration. M5 uses state version `1`; a future incompatible state version will be
rejected with a message rather than resumed as if it were current.

## M6 incident memory and historical retrieval

M6 makes completed incidents durable and searchable without turning an old
diagnosis into a current fact. It uses ordinary SQLite queries; there are no
embeddings, vector indexes, or RAG framework dependencies.

### Data model and migration design

Incident memory lives in `~/.hades/incident_history.sqlite` by default. It is a
separate database from M5's `investigations.sqlite`; deleting or rotating one
does not silently destroy the other.

The schema is versioned with SQLite `PRAGMA user_version`, currently **1**. The
version is stored in every export and is checked before any read or write.
Migrations run inside an immediate transaction and update the version only after
all schema statements succeed. A database from a newer build is rejected with
an actionable `MemorySchemaError`; it is not modified, reset, or overwritten. A
corrupt file produces `MemoryStoreError` instead of silently creating a new
history.

Schema version 1 contains:

| Table | Purpose |
| --- | --- |
| `incidents` | Canonical M2 fields (`incident_id`, kind, severity, subject, state, summary, timestamps), resolution status, creation/update metadata |
| `incident_metrics` | Allowlisted numerical measurements used for explicit similarity comparisons |
| `incident_tags` | Bounded user/agent tags |
| `investigations` | Investigation/execution IDs, associated incident, question, model, stage, termination status, validated report JSON |
| `evidence` | Current investigation evidence with tool, redacted validated arguments, bounded summary, collection time, call ID, truncation/fresh-check flags, duration |
| `diagnoses` | Final report, diagnosis summary, confidence label/explanation, verification status, supporting evidence IDs |
| `diagnosis_verifications` | Append-only confirmed/disproved/unknown verification events |
| `incident_actions` | Caller-supplied proposed/approved/executed/rejected remediation records and outcomes; M6 never executes them |
| `incident_events` | Append-only incident/investigation lifecycle events |

Indexes cover the common filters: incident kind and update time, state, resolution
status, investigation association, evidence timestamps, metric name/time,
diagnosis history, event time, and tags. Writes use parameterized SQL and
`BEGIN IMMEDIATE`; foreign keys, WAL, a busy timeout, and cascade deletion keep
multi-record updates consistent under concurrent local access.

Incident creation/update is idempotent. A monitor-observed `recovered` state sets
`resolution_status=recovered` only while the record is still open. A diagnosis
produced by the LLM is stored as `unverified`; it never marks the incident
resolved. Verification and resolution are separate and independently auditable.

### Retrieval strategy and ranking rules

Given the current incident, retrieval:

1. Selects up to 200 prior incidents with the same `kind`, excluding the current
   incident ID.
2. Extracts only allowlisted numerical measurements from M2 evidence, such as
   CPU percent, memory percentages, or disk available percent. It does not store
   full environment dumps or arbitrary tool output as metrics.
3. Compares common metric names using
   `abs(current - prior) / max(abs(current), abs(prior), 1e-9)`.
4. Ranks deterministically by:
   - same subject (for example the same mount point);
   - the presence of comparable metric samples;
   - lower mean relative metric difference;
   - more recent update time;
   - incident ID as a final stable tie-breaker.
5. Returns at most `HADES_MAX_HISTORY_RESULTS` records (default 5), bounded again
   by `HADES_MAX_HISTORY_CHARS` before entering the graph state.

Each record includes a transparent `ranking_reason`, metric comparisons, the
prior diagnosis with its verification status, and up to five prior actions. If
no same-kind history exists, the result is an explicit empty list. There is no
opaque similarity score.

The model receives history in a separate `historical_incidents` key. It never
appears in the current `evidence` array. Prior diagnoses are labelled
`source=historical_diagnosis` and `is_current_fact=false`; prior actions are
labelled `source=historical_action`. Prompt instructions state that historical
records are not current observations, cannot be cited as current evidence, and
must not be repeated blindly.

### LangGraph integration and persistence

M6 inserts one explicit node and stage between initialization and planning:

```text
initialize → retrieve_history → plan → validate → execute → assess
                                      ↑                         │
                                      └────── bounded loop ─────┘
                                                          finalize → terminate
```

- `initialize` idempotently upserts the current incident and associates the
  investigation before any model planning.
- `retrieve_history` calls deterministic SQLite retrieval and copies bounded
  historical context into the checkpointed state. It excludes the current
  incident.
- Planning and report generation reuse M5/M4 prompts and validation, with
  historical context clearly separated from current evidence.
- `terminate` persists the report, current evidence, diagnosis, and lifecycle
  event. Persistence calls are idempotent so a crash between a database write and
  a graph checkpoint does not duplicate the report or evidence.
- A memory query/write failure is recorded in `memory_errors` and surfaced in
  text/JSON output; the investigation does not fabricate historical evidence or
  claim that persistence succeeded. An unreadable or incompatible database is
  rejected when opened.

The M5 checkpoint still carries recovery state for one investigation. Incident
memory survives process restarts in a separate database and is queried by later
investigations.

### Database location and data-management commands

Default path: `~/.hades/incident_history.sqlite`. Override with
`HADES_INCIDENT_DB` or `--incident-db`. The directory and database are created
with user-only permissions where the platform allows it.

```sh
# List recent incidents and filter by structured fields.
hades-agent history
hades-agent history --kind high_cpu --resolution open --limit 20 --json

# Show one incident with metrics, investigations, evidence, diagnoses,
# verification events, actions, and lifecycle events.
hades-agent history-show inc-123

# Append verification evidence. Missing outcomes are "unknown"/"unverified";
# contradictory outcomes remain in the append-only history.
hades-agent history-verify inc-123 --status confirmed --note "observed after restart"
hades-agent history-verify inc-123 --status disproved --note "symptom returned"

# Record resolution separately from diagnosis verification.
hades-agent history-resolve inc-123 --resolution resolved

# Write a bounded local JSON export. The file is created with user-only
# permissions; use --force to replace an existing file.
hades-agent history-export --output /tmp/hades-history.json

# Permanently delete one incident and dependent records.
hades-agent history-delete inc-123 --yes

# Retention: preview or apply deletion of old incidents/investigations.
hades-agent history-prune --older-than-days 90 --dry-run
hades-agent history-prune --older-than-days 90 --yes
```

`history`, `history-show`, `history-export`, `history-delete`, and
`history-prune` do not load Ollama or the Rust service. They only open the local
incident database. Exports contain bounded structured records and no external
telemetry is sent.

### M6 security and privacy

- Only canonical incident fields, allowlisted metrics, redacted arguments,
  bounded summaries, and validated reports are stored. Sensitive top-level
  argument keys are stored as `[redacted]`.
- Incident text and historical records are treated as untrusted data in prompts.
  They cannot add tools, alter limits, or issue shell commands.
- Historical diagnoses are advisory. A confirmed old cause is still not proof of
  the current cause, and a prior remediation is never executed automatically.
- The database is local but **not encrypted**. It can contain process names,
  incident text, evidence summaries, and resource measurements. Protect it as
  sensitive user data.
- There is no cloud synchronization, telemetry, or vector database.

M5 state version remains `1`; M6 adds optional history fields that older
checkpoints default to empty. Incident-memory schema version `1` is independent
of the LangGraph state version. A future incident-memory schema change will use a
new version and an explicit migration; incompatible databases are rejected with
backup guidance rather than discarded.

## Testing and linting

Rust:

```sh
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
```

Python (from the repository root, with the installed virtual environment):

```sh
cargo build --bin os-agent
python/.venv/bin/ruff check python
PYTHONPATH=python/src python/.venv/bin/python -m unittest discover -s python/tests -v
```

The Python suite includes deterministic unit tests with fake model responses and
fake HTTP services, plus integration tests that start the real Rust service and
exercise all five tools. Workflow tests cover graph stages, allowlist/argument
rejection, evidence retention, step and tool budgets, malformed/timeout model
behavior, tool service failures, inconclusive reports, checkpoint creation and
resumption, simulated process interruption, corrupt/incompatible checkpoints,
and duplicate-tool-call prevention. M6 adds tests for schema creation/versioning,
incident updates, filtered listing and transparent ranking, empty history,
append-only verification, resolution/verification separation, incident/
investigation/report association, redaction, export/deletion/pruning,
concurrent access, corrupt/incompatible databases, history injection into the
graph, and persistence across store reopens. The default suite does **not**
require Ollama. The M1–M5 tests remain in the same suite.

Optional live LLM integration tests (require Ollama, the model, and the Rust
service; the test starts its own Rust service and covers both the M4 loop and the
M5/M6 graph):

```sh
HADES_OLLAMA_INTEGRATION=1 \
  PYTHONPATH=python/src python/.venv/bin/python -m unittest discover \
  -s python/tests -p 'test_ollama_integration.py' -v
```

Test suites do not depend on a specific CPU load or a particular unrelated
process. Live process tests inspect the test process itself.

## Performance measurements

Measured with a release build on the verified development host (macOS, arm64)
using `/usr/bin/time -l`. CPU percentages are approximate:
`(user + sys) / real`, normalized to one core, not a benchmark under controlled
load. Short runs only.

| Workload | Real | User | Sys | Max RSS (MB) | Approx. CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| `monitor --interval 1s --duration 20s --json-lines` | 20.79 s | 0.13 s | 1.11 s | 9.4 MiB | 6.0% |
| `monitor --interval 5s --duration 30s --json-lines` | 30.33 s | 0.06 s | 0.45 s | 8.9 MiB | 1.7% |
| `serve --interval 5s --duration 20s` (idle) | 20.33 s | 0.04 s | 0.30 s | 9.4 MiB | 1.7% |
| `status --json` (one snapshot) | 0.33 s | 0.01 s | 0.10 s | 11.0 MiB | 33% over 0.33 s |

The one-shot `status` figure is dominated by the intentional CPU sampling
window, so its percentage is not a useful sustained-overhead number.

Local tool latency (real Rust service, 3–5 calls each, Python client):

| Tool | Mean | Range |
| --- | ---: | ---: |
| `get_system_metrics` | 292.5 ms | 282–304 ms |
| `get_disk_usage` | 287.2 ms | 262–300 ms |
| `list_top_processes` | 300.4 ms | 291–305 ms |
| `inspect_process` | 206.4 ms | 206–207 ms |
| `get_recent_incidents` | 0.9 ms | 0.4–1.7 ms |

The collection tools include the same CPU sampling interval as M1. Monitoring
and incident queries do not scan processes.

## Known limitations

- **LLM diagnoses are unreliable.** The local model can hallucinate, miss
  evidence, or overstate confidence. The report validation checks structure and
  citations, not the truth of every phrase. Use the tool trace.
- **Structured memory, not semantic memory.** M6 retrieves by incident kind,
  subject, explicit metric comparisons, and recency. It does not understand
  paraphrased symptoms or free-text similarity. Adding embeddings/RAG would need
  evidence that structured retrieval is insufficient.
- **Historical diagnoses are not proof.** A prior confirmed diagnosis is
  context for comparison, not a current fact. The model can still over-generalize
  from history; the current evidence trace remains authoritative.
- **Bounded investigation.** The graph stops at configured step, tool-call, and
  consecutive-failure limits. Some problems need more evidence.
- **Small context window.** Tool results, evidence, transitions, and planner
  context are bounded/truncated; the model may not see every detail.
- **Checkpoints and incident memory are local but not encrypted.** SQLite files
  can contain process names, incident text, evidence summaries, and resource
  measurements. Protect them as user data; no checkpoint or history record
  leaves the machine by design.
- **Retention is explicit.** Nothing is deleted automatically. Use
  `history-prune` with a retention window and `--dry-run` first. Deleting an
  incident cascades to its associated investigations, evidence, diagnoses,
  metrics, actions, and events.
- **Read-only recovery only.** Duplicate prevention and fresh checks are safe
  because all tools are read-only. A future mutating tool would require a
  stronger transactional idempotency design before it could be used here.
- **Bridge authentication.** Loopback is not a trust boundary. Any local process
  can call the read-only tools. The service does not log process data, but tool
  results can reveal process names and resource use.
- **No remediation.** Acknowledging an incident or producing a diagnosis changes
  nothing on the system.
- **Disk disappearance.** An open disk incident is not recovered when its mount
  disappears. This is conservative but can leave an incident open.
- **CPU duration is wall-clock from the first qualifying sample.** At a 5-second
  interval, six samples cover about 25 seconds of elapsed time even though the
  configured duration is 30 seconds. The configured duration determines the
  required sample count.
- **Platform coverage.** macOS is verified; Linux is expected but unverified;
  Windows is unverified. Some metadata is platform-dependent.
- **Process CPU semantics.** Values are percent of one logical core and can
  exceed 100%; memory is resident memory.
- **Metric semantics vary by OS.** Memory availability and filesystem reserved
  blocks differ across platforms.
- **No root.** Hidden processes or metrics are unavailable rather than
  collected by escalating privileges.
- **Schemas may evolve.** Protocol and output schema versions exist for future
  compatibility but are not yet a long-term stability promise.

## Roadmap

Completed: M1 metrics/CLI, M2 monitoring/detection/events, M3 bridge/client, M4
local LLM diagnosis, M5 stateful LangGraph investigations with local recovery,
M6 durable structured incident memory and deterministic historical retrieval.

Potential next milestone (not implemented): user-approved remediation with an
audit trail, and semantic retrieval only if tests demonstrate that structured
retrieval is insufficient. These are proposals, not current functionality.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Hades contributors.
