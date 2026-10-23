# Hades

Local-first system health monitoring for a future AI OS agent.

**Current milestone: M1 — Rust system-monitoring foundation.** Hades collects
real CPU, memory, filesystem, and process metrics and exposes them through a
typed Rust library and an `os-agent` CLI. There is no AI, no remediation, no
daemon, no database, and no network service in this milestone.

## Why

The long-term project is a Linux system agent that watches system health,
detects anomalies, investigates them with a local language model, remembers
past incidents, and proposes remediations that a user must approve. Reliable,
structured metrics are the foundation for all of that. M1 builds and tests that
foundation only.

## Platform support

- Built, tested, and manually verified on **macOS 15.5 (arm64)** with the stable
  Rust toolchain (1.91.1 at the time of writing).
- The implementation uses `sysinfo`'s cross-platform APIs, so Linux is expected
  to work, but **Linux has not been verified in this milestone**.
- Windows is not verified.
- No root privileges are required. Metrics that the operating system denies to
  an unprivileged user are reported as unavailable or omitted.

## Build and install

Requirements: Rust 1.85 or newer (the crate uses edition 2024).

```sh
cargo build --release
./target/release/os-agent --help

# Or install the binary into ~/.cargo/bin:
cargo install --path .
```

## CLI usage

```text
os-agent status [--json]
os-agent processes [--sort cpu|memory] [--limit 1..100]
```

| Command | Description |
| --- | --- |
| `os-agent status` | Human-readable snapshot of CPU, memory, disks, and the top 10 processes (sorted by CPU). |
| `os-agent status --json` | The same snapshot as JSON on stdout. |
| `os-agent processes --sort cpu` | Top processes by CPU utilization. |
| `os-agent processes --sort memory` | Top processes by resident memory. |
| `os-agent processes --limit N` | Number of processes to show. Default `10`, valid range `1..=100`. |

Examples:

```sh
os-agent status
os-agent status --json
os-agent processes --sort cpu
os-agent processes --sort memory --limit 5
```

`--sort` defaults to `cpu`. `--limit` is validated by the CLI: values outside
`1..=100` or non-numeric values are rejected with exit code `2` and a message on
stderr. Invalid subcommands and unknown flags also exit with code `2`.
Successful runs exit `0`; I/O or serialization failures exit `1`.

The `processes` command prints collection warnings (for example, unavailable
memory data) to stderr, keeping stdout a clean process table.

### Example human-readable output

This is unedited output from the development machine:

```text
Hades system health
Collected: 2026-10-23T00:17:01.106Z
CPU: 77.7% across 8 logical cores (200 ms sample)
  core 0: 71.9%, core 1: 58.8%, core 2: 55.9%, core 3: 50.0%, core 4: 97.1%, core 5: 97.0%, core 6: 97.0%, core 7: 94.1%
Memory: 13.5 GiB used / 16.0 GiB total (84.2%), 5.6 GiB available, 59.4 MiB free
Disks:
  / [apfs]: 201.2 GiB used / 228.3 GiB (88.1%), 27.1 GiB available
  /System/Volumes/Data [apfs]: 201.2 GiB used / 228.3 GiB (88.1%), 27.1 GiB available
  /Volumes/boringNotch 2.7.3 [apfs]: 20.2 MiB used / 32.5 MiB (62.2%), 12.3 MiB available
  /Volumes/Install OrbStack v2.2.3 [apfs]: 700.7 MiB used / 1.0 GiB (65.2%), 373.7 MiB available
  /Volumes/Raycast [hfs]: 233.4 MiB used / 267.0 MiB (87.4%), 33.6 MiB available
  /Volumes/Raycast 1 [hfs]: 233.4 MiB used / 267.0 MiB (87.4%), 33.6 MiB available
  /Volumes/Yomu [hfs]: 17.8 MiB used / 42.0 MiB (42.4%), 24.2 MiB available
  /Volumes/Dia [hfs]: 1.3 GiB used / 1.3 GiB (99.8%), 2.1 MiB available
Top processes (10 shown, sorted by cpu):
  PID     CPU%    MEMORY      NAME
  42645   169.2   495.5 MiB   Arc
  99987   83.3    645.7 MiB   com.apple.Virtualization.VirtualMachine
  3219    49.0    8.9 MiB     bash
  75646   39.8    662.5 MiB   opencode
  3352    34.0    165.3 MiB   Browser Helper (Renderer)
  32908   30.8    11.1 MiB    os-agent
  41420   22.7    5.6 MiB     git
  30171   22.6    760.9 MiB   Browser Helper (Renderer)
  69154   17.8    148.5 MiB   zed
  27286   8.4     63.3 MiB    siriinferenced
```

CPU utilization is a measurement over a real 200 ms interval, so values vary
between runs. Process CPU is relative to one logical core and may exceed 100%
for multi-threaded processes.

## JSON output

`os-agent status --json` emits one JSON object with `schema_version: 1`. The
same shape is available from the Rust API as `os_agent::Snapshot`.

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | integer | Currently `1`. |
| `collected_at` | string | RFC 3339 timestamp in UTC, millisecond precision. |
| `cpu` | object or `null` | `overall_percent` (0–100), `per_core_percent` array, `logical_core_count`, `sampling_interval_ms`. |
| `memory` | object or `null` | All sizes in **bytes**. `used_bytes`, `available_bytes`, `free_bytes`, `used_percent`. |
| `disks` | array | One object per mounted filesystem: `mount_point`, `file_system` (string or `null`), `total_bytes`, `available_bytes`, `used_bytes`, `used_percent`. |
| `processes` | object | `sorted_by` (`cpu`/`memory`), requested `limit`, and `items` (array). |
| `processes.items[]` | object | `pid`, `name`, `cpu_percent`, `memory_bytes` (bytes). |
| `issues` | array | Partial-collection problems: `component` and `message`. Empty when everything was collected. |

Important semantics:

- A metric that could not be collected is `null` (or an empty array plus an
  entry in `issues`), never a fake zero. A legitimate `0.0` percentage is a
  real measurement.
- `used_percent` is `used / total × 100`, clamped to `0..=100`.
- `memory.available_bytes` is memory usable by new applications without
  swapping (including reclaimable cache where applicable).
  `memory.free_bytes` is completely unused memory. They are intentionally
  distinct.
- `disks[].used_bytes` is `total_bytes - available_bytes`. On filesystems that
  reserve blocks for privileged use this can be lower than the space occupied
  by files.
- Process CPU is percent of one logical core, so `cpu_percent` can exceed 100.
- Process `name` is converted lossily to UTF-8. Command-line arguments,
  environment variables, and executable paths are deliberately not collected.

<details>
<summary>Full example JSON output (unedited, from the same macOS host)</summary>

```json
{
  "schema_version": 1,
  "collected_at": "2026-10-23T00:17:06.327Z",
  "cpu": {
    "overall_percent": 73.30728912353516,
    "per_core_percent": [
      64.70588684082031,
      53.125,
      47.05882263183594,
      42.42424392700195,
      94.11764526367188,
      96.96969604492188,
      94.11764526367188,
      93.93939208984375
    ],
    "logical_core_count": 8,
    "sampling_interval_ms": 200
  },
  "memory": {
    "total_bytes": 17179869184,
    "used_bytes": 14550122496,
    "available_bytes": 5687558144,
    "free_bytes": 68845568,
    "used_percent": 84.6928596496582
  },
  "disks": [
    {
      "mount_point": "/",
      "file_system": "apfs",
      "total_bytes": 245107195904,
      "available_bytes": 29103345664,
      "used_bytes": 216003850240,
      "used_percent": 88.1262785628706
    },
    {
      "mount_point": "/System/Volumes/Data",
      "file_system": "apfs",
      "total_bytes": 245107195904,
      "available_bytes": 29103345664,
      "used_bytes": 216003850240,
      "used_percent": 88.1262785628706
    },
    {
      "mount_point": "/Volumes/boringNotch 2.7.3",
      "file_system": "apfs",
      "total_bytes": 34086912,
      "available_bytes": 12877824,
      "used_bytes": 21209088,
      "used_percent": 62.22062004325883
    },
    {
      "mount_point": "/Volumes/Install OrbStack v2.2.3",
      "file_system": "apfs",
      "total_bytes": 1126653952,
      "available_bytes": 391888896,
      "used_bytes": 734765056,
      "used_percent": 65.21656935527263
    },
    {
      "mount_point": "/Volumes/Raycast",
      "file_system": "hfs",
      "total_bytes": 279969792,
      "available_bytes": 35246080,
      "used_bytes": 244723712,
      "used_percent": 87.41075608614233
    },
    {
      "mount_point": "/Volumes/Raycast 1",
      "file_system": "hfs",
      "total_bytes": 279969792,
      "available_bytes": 35246080,
      "used_bytes": 244723712,
      "used_percent": 87.41075608614233
    },
    {
      "mount_point": "/Volumes/Yomu",
      "file_system": "hfs",
      "total_bytes": 44040192,
      "available_bytes": 25370624,
      "used_bytes": 18669568,
      "used_percent": 42.392113095238095
    },
    {
      "mount_point": "/Volumes/Dia",
      "file_system": "hfs",
      "total_bytes": 1439592448,
      "available_bytes": 2228224,
      "used_bytes": 1437364224,
      "used_percent": 99.84521841559425
    }
  ],
  "processes": {
    "sorted_by": "cpu",
    "limit": 10,
    "items": [
      {
        "pid": 42645,
        "name": "Arc",
        "cpu_percent": 180.06991577148438,
        "memory_bytes": 514654208
      },
      {
        "pid": 30171,
        "name": "Browser Helper (Renderer)",
        "cpu_percent": 51.905643463134766,
        "memory_bytes": 619315200
      },
      {
        "pid": 3219,
        "name": "bash",
        "cpu_percent": 41.509639739990234,
        "memory_bytes": 9256960
      },
      {
        "pid": 3352,
        "name": "Browser Helper (Renderer)",
        "cpu_percent": 31.195941925048828,
        "memory_bytes": 176340992
      },
      {
        "pid": 33742,
        "name": "os-agent",
        "cpu_percent": 30.586528778076172,
        "memory_bytes": 11665408
      },
      {
        "pid": 41420,
        "name": "git",
        "cpu_percent": 20.585817337036133,
        "memory_bytes": 6406144
      },
      {
        "pid": 75646,
        "name": "opencode",
        "cpu_percent": 19.301647186279297,
        "memory_bytes": 279494656
      },
      {
        "pid": 69154,
        "name": "zed",
        "cpu_percent": 14.961385726928711,
        "memory_bytes": 157417472
      },
      {
        "pid": 99987,
        "name": "com.apple.Virtualization.VirtualMachine",
        "cpu_percent": 10.449288368225098,
        "memory_bytes": 961036288
      },
      {
        "pid": 30468,
        "name": "opencode",
        "cpu_percent": 8.108152389526367,
        "memory_bytes": 154288128
      }
    ]
  },
  "issues": []
}
```

</details>

## How collection works

1. `Collector::with_processes` creates a `sysinfo::System` with an initial
   refresh and starts a `sysinfo::Disks` listing. The disk listing is a bounded
   list of mounts; the code never walks directories or scans files.
2. `Collector::collect` sleeps for `sysinfo::MINIMUM_CPU_UPDATE_INTERVAL`
   (200 ms on macOS and Linux), then refreshes CPU, memory, processes, and
   disks. This second CPU sample is what makes utilization meaningful; an
   uninitialized first reading is never reported.
3. CPU and memory are converted into typed metric structs. A missing value
   becomes `None` plus a `CollectionIssue`; valid metrics are retained.
4. Mount points are deduplicated. Filesystems reporting zero total capacity are
   skipped and reported as issues. Permission errors from the OS simply leave
   that filesystem out.
5. Processes are filtered to PID, name, CPU, and resident memory, sorted by the
   requested metric with deterministic tie-breaking (name, then PID), and
   truncated to the limit.
6. The snapshot is timestamped and returned. Rendering (`report`) and JSON
   serialization are separate from collection.

## Architecture

```text
src/
├── main.rs              CLI parsing, output, exit behavior
├── lib.rs               Public library surface
├── metrics/
│   ├── mod.rs           Shared percentage calculation and limits
│   ├── cpu.rs           CPU metric type and conversion
│   ├── memory.rs        Memory metric type and conversion
│   ├── disk.rs          Filesystem capacity type and conversion
│   └── processes.rs     Process type, sorting, truncation, collection
├── snapshot.rs          Typed snapshot, issues, live Collector
└── report.rs            Pure human-readable rendering
tests/
└── cli.rs               End-to-end CLI behavior
```

The library has no CLI dependencies and can be reused by a future background
monitor or the planned Python orchestrator through a stable interface. Pure
calculations and sorting live in `metrics/*` and are tested without touching
live system state; only `Collector` reads the OS.

## Testing and linting

```sh
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
```

The test suite has 30 unit tests and 11 CLI integration tests. Unit tests cover
percentage edge cases, unavailable metrics, byte formatting, process sorting
and limits, snapshot serialization/round-tripping, and report rendering. CLI
tests use `assert_cmd` for valid human and JSON output, schema shape, limits,
sorting, and invalid arguments. Live tests assert ranges and structural
properties only; they do not depend on exact CPU percentages, memory sizes, or
specific running processes.

Each CLI test that collects a snapshot waits for the CPU sampling interval, so
the integration suite takes about 1.5 seconds on a warm build.

## Known limitations

- **One-shot only.** Every command collects a fresh snapshot. There is no
  history, persistence, or background sampling in M1.
- **CPU needs a sampling window.** Each snapshot adds at least ~200 ms of
  latency by design. Extremely short-lived spikes can be missed.
- **Process CPU exceeds 100%.** It is measured per logical core and is not
  normalized by core count.
- **Memory values are OS-reported.** `used` and `available` semantics depend on
  the platform (for example, macOS and Linux account for cache differently).
- **Disk usage is derived.** Reserved blocks, snapshots, and shared APFS
  containers can make usage differ from figures reported by other tools.
  Mounted disk images (`.dmg`) also appear as filesystems.
- **Platform coverage is limited.** macOS is verified; Linux is expected but
  unverified; Windows is unverified.
- **Permissions and races.** A process can exit between refreshes, and some
  fields may be hidden for other users' processes. Such entries are skipped;
  monitoring never escalates privileges.
- **Lossy names.** Non-UTF-8 process names are lossily converted. Control
  characters are replaced in the human report (JSON keeps the lossy name).
- **No sensitive process details.** Arguments, environment variables, users,
  and executable paths are not collected.
- **No stable API promise yet.** The Rust API and JSON schema may evolve in
  later milestones; `schema_version` exists for that reason.

## Roadmap

M1 (this milestone) is complete: metrics, snapshot, CLI, errors, tests, docs.

Future milestones are **not implemented**:

- M2: continuous background monitoring and time-series capture
- M3: baseline/anomaly detection
- M4: local-LLM investigation and incident memory
- M5: user-approved remediation with auditability

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Hades contributors.
