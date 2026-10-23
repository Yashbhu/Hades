use std::fmt::Write as _;
use std::io::{self, Write as _};
use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::SecondsFormat;
use clap::{Args, Parser, Subcommand, ValueEnum};
use os_agent::bridge::{AgentService, ServerConfig};
use os_agent::config::MonitorConfig;
use os_agent::events::MonitorEvent;
use os_agent::incident::Incident;
use os_agent::metrics::{
    DEFAULT_PROCESS_LIMIT, MAX_PROCESS_LIMIT, ProcessSort, utilization_percent,
};
use os_agent::monitor::Monitor;
use os_agent::report;
use os_agent::snapshot::{Collector, Snapshot};

/// Local-first system health monitor.
#[derive(Debug, Parser)]
#[command(name = "os-agent", version, about, long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
#[allow(clippy::large_enum_variant)] // clap argument structs are inherently large
enum Command {
    /// Print a one-shot system-health snapshot.
    Status {
        /// Emit the snapshot as JSON instead of a text report.
        #[arg(long)]
        json: bool,
    },
    /// List the top resource-consuming processes.
    Processes {
        /// Metric used to rank processes.
        #[arg(long, value_enum, default_value_t = SortArg::Cpu)]
        sort: SortArg,
        /// Number of processes to show (1-100).
        #[arg(long, default_value_t = DEFAULT_PROCESS_LIMIT, value_parser = parse_limit)]
        limit: usize,
    },
    /// Continuously monitor and report deterministic anomalies.
    Monitor(MonitorArgs),
    /// Serve the read-only diagnostic tools over loopback HTTP.
    Serve(ServeArgs),
}

/// Options for `os-agent serve`.
#[derive(Debug, Args)]
struct ServeArgs {
    /// Loopback address to bind. Default: 127.0.0.1:8765.
    #[arg(long, default_value = "127.0.0.1:8765", value_parser = parse_bind_addr)]
    bind: SocketAddr,
    /// Monitoring sampling interval (for example 5s, 500ms, 1m). Default: 5s.
    #[arg(long, value_parser = parse_duration_arg)]
    interval: Option<Duration>,
    /// Run for a bounded duration, then shut down cleanly (for example 30s).
    #[arg(long, value_parser = parse_duration_arg)]
    duration: Option<Duration>,
}

/// Options for `os-agent monitor`.
#[derive(Debug, Args)]
struct MonitorArgs {
    /// Sampling interval (for example 5s, 500ms, 1m). Default: 5s.
    #[arg(long, value_parser = parse_duration_arg)]
    interval: Option<Duration>,
    /// CPU trigger percent. Default: 90.
    #[arg(long, value_parser = parse_percent_arg)]
    cpu_threshold: Option<f64>,
    /// CPU recovery percent. Default: 85.
    #[arg(long, value_parser = parse_percent_arg)]
    cpu_recovery: Option<f64>,
    /// CPU critical percent. Default: 95.
    #[arg(long, value_parser = parse_percent_arg)]
    cpu_critical: Option<f64>,
    /// CPU duration above the trigger (for example 30s). Default: 30s.
    #[arg(long, value_parser = parse_duration_arg)]
    cpu_duration: Option<Duration>,
    /// Available-memory trigger percent. Default: 10.
    #[arg(long, value_parser = parse_percent_arg)]
    memory_threshold: Option<f64>,
    /// Available-memory recovery percent. Default: 15.
    #[arg(long, value_parser = parse_percent_arg)]
    memory_recovery: Option<f64>,
    /// Available-memory critical percent. Default: 5.
    #[arg(long, value_parser = parse_percent_arg)]
    memory_critical: Option<f64>,
    /// Memory duration at or below the trigger. Default: 30s.
    #[arg(long, value_parser = parse_duration_arg)]
    memory_duration: Option<Duration>,
    /// Available-disk trigger percent. Default: 10.
    #[arg(long, value_parser = parse_percent_arg)]
    disk_threshold: Option<f64>,
    /// Available-disk recovery percent. Default: 15.
    #[arg(long, value_parser = parse_percent_arg)]
    disk_recovery: Option<f64>,
    /// Available-disk critical percent. Default: 3.
    #[arg(long, value_parser = parse_percent_arg)]
    disk_critical: Option<f64>,
    /// Number of rolling history samples. Default: 60.
    #[arg(long, value_parser = parse_usize_arg)]
    history: Option<usize>,
    /// Emit only JSON Lines events on stdout.
    #[arg(long)]
    json_lines: bool,
    /// Run for a bounded duration, then exit cleanly (for example 30s).
    #[arg(long, value_parser = parse_duration_arg)]
    duration: Option<Duration>,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum SortArg {
    Cpu,
    Memory,
}

impl From<SortArg> for ProcessSort {
    fn from(value: SortArg) -> Self {
        match value {
            SortArg::Cpu => Self::Cpu,
            SortArg::Memory => Self::Memory,
        }
    }
}

fn parse_limit(raw: &str) -> Result<usize, String> {
    let limit: usize = raw
        .parse()
        .map_err(|_| format!("'{raw}' is not a valid integer"))?;
    if (1..=MAX_PROCESS_LIMIT).contains(&limit) {
        Ok(limit)
    } else {
        Err(format!(
            "limit must be between 1 and {MAX_PROCESS_LIMIT}, got {limit}"
        ))
    }
}

fn parse_bind_addr(raw: &str) -> Result<SocketAddr, String> {
    let address: SocketAddr = raw
        .parse()
        .map_err(|_| format!("'{raw}' is not a valid socket address (use IP:PORT)"))?;
    if !address.ip().is_loopback() {
        return Err(format!(
            "bind address must be loopback, got {address}; use 127.0.0.1 or [::1]"
        ));
    }
    Ok(address)
}

fn parse_usize_arg(raw: &str) -> Result<usize, String> {
    raw.parse()
        .map_err(|_| format!("'{raw}' is not a valid non-negative integer"))
}

fn parse_percent_arg(raw: &str) -> Result<f64, String> {
    let value: f64 = raw
        .parse()
        .map_err(|_| format!("'{raw}' is not a valid percentage"))?;
    if value.is_finite() && (0.0..=100.0).contains(&value) {
        Ok(value)
    } else {
        Err(format!("percentage must be between 0 and 100, got {raw}"))
    }
}

fn parse_duration_arg(raw: &str) -> Result<Duration, String> {
    let raw = raw.trim();
    let (number, milliseconds_per_unit) = if let Some(value) = raw.strip_suffix("ms") {
        (value, 1_u64)
    } else if let Some(value) = raw.strip_suffix('s') {
        (value, 1_000)
    } else if let Some(value) = raw.strip_suffix('m') {
        (value, 60_000)
    } else if let Some(value) = raw.strip_suffix('h') {
        (value, 3_600_000)
    } else {
        (raw, 1_000)
    };
    let amount: u64 = number
        .parse()
        .map_err(|_| format!("'{raw}' is not a valid duration (use ms, s, m, or h)"))?;
    let milliseconds = amount
        .checked_mul(milliseconds_per_unit)
        .ok_or_else(|| format!("duration '{raw}' is too large"))?;
    if milliseconds == 0 {
        return Err("duration must be greater than zero".into());
    }
    Ok(Duration::from_millis(milliseconds))
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Status { json } => {
            let snapshot = Collector::new().collect();
            if json {
                let encoded =
                    serde_json::to_string_pretty(&snapshot).context("failed to encode snapshot")?;
                write_stdout(&encoded)
            } else {
                write_stdout(&report::render_status(&snapshot))
            }
        }
        Command::Processes { sort, limit } => {
            let snapshot = Collector::with_processes(ProcessSort::from(sort), limit).collect();
            warn_about_issues(&snapshot);
            write_stdout(&report::render_processes(&snapshot.processes))
        }
        Command::Monitor(args) => run_monitor(args),
        Command::Serve(args) => run_serve(args),
    }
}

fn run_serve(args: ServeArgs) -> Result<()> {
    let mut config = ServerConfig {
        bind: args.bind,
        ..ServerConfig::default()
    };
    if let Some(interval) = args.interval {
        config.monitor.sampling_interval = interval;
    }
    config
        .validate()
        .map_err(|error| anyhow::anyhow!("invalid configuration: {error}"))?;

    let service = AgentService::bind(config).context("failed to start diagnostic service")?;
    let address = service
        .local_addr()
        .context("failed to read bound address")?;

    let shutdown = Arc::new(AtomicBool::new(false));
    let signal_flag = Arc::clone(&shutdown);
    ctrlc::set_handler(move || signal_flag.store(true, Ordering::SeqCst))
        .context("failed to install Ctrl+C handler")?;

    if let Some(duration) = args.duration {
        let timer_flag = Arc::clone(&shutdown);
        std::thread::spawn(move || {
            std::thread::sleep(duration);
            timer_flag.store(true, Ordering::SeqCst);
        });
    }

    let mut banner = String::new();
    let _ = writeln!(banner, "Hades diagnostic service");
    let _ = writeln!(banner, "Listening on http://{address}");
    let _ = writeln!(banner, "Discovery: GET http://{address}/v1/tools");
    let _ = writeln!(banner, "Calls:     POST http://{address}/v1/tools/call");
    match args.duration {
        Some(duration) => {
            let _ = writeln!(banner, "Stopping after {duration:?}.");
        }
        None => banner.push_str("Press Ctrl+C to stop.\n"),
    }
    write_stdout(&banner)?;

    service
        .serve(&shutdown)
        .context("diagnostic service stopped with an error")?;
    let _ = write_stdout_best_effort("Service stopped.");
    Ok(())
}

fn run_monitor(args: MonitorArgs) -> Result<()> {
    let mut config = MonitorConfig::default();
    if let Some(value) = args.interval {
        config.sampling_interval = value;
    }
    if let Some(value) = args.cpu_threshold {
        config.cpu.threshold_percent = value;
    }
    if let Some(value) = args.cpu_recovery {
        config.cpu.recovery_threshold_percent = value;
    }
    if let Some(value) = args.cpu_critical {
        config.cpu.critical_threshold_percent = value;
    }
    if let Some(value) = args.cpu_duration {
        config.cpu.sustained_for = value;
    }
    if let Some(value) = args.memory_threshold {
        config.memory.available_threshold_percent = value;
    }
    if let Some(value) = args.memory_recovery {
        config.memory.recovery_available_percent = value;
    }
    if let Some(value) = args.memory_critical {
        config.memory.critical_available_percent = value;
    }
    if let Some(value) = args.memory_duration {
        config.memory.sustained_for = value;
    }
    if let Some(value) = args.disk_threshold {
        config.disk.available_threshold_percent = value;
    }
    if let Some(value) = args.disk_recovery {
        config.disk.recovery_available_percent = value;
    }
    if let Some(value) = args.disk_critical {
        config.disk.critical_available_percent = value;
    }
    if let Some(value) = args.history {
        config.history_limit = value;
    }
    config
        .validate()
        .map_err(|error| anyhow::anyhow!("invalid configuration: {error}"))?;

    let json_lines = args.json_lines;
    if !json_lines {
        print_monitor_banner(&config, args.duration);
    }

    let shutdown = Arc::new(AtomicBool::new(false));
    let signal_flag = Arc::clone(&shutdown);
    ctrlc::set_handler(move || signal_flag.store(true, Ordering::SeqCst))
        .context("failed to install Ctrl+C handler")?;

    let mut collector = Collector::without_processes();
    let handle = Monitor::new(config)
        .map_err(|error| anyhow::anyhow!("invalid configuration: {error}"))?
        .start(move || {
            let snapshot = collector.collect();
            if !json_lines {
                print_cycle(&snapshot);
            }
            snapshot
        })
        .context("failed to start monitor thread")?;

    let started = Instant::now();
    let poll = Duration::from_millis(200);
    loop {
        if shutdown.load(Ordering::SeqCst) {
            break;
        }
        let wait = match args.duration {
            Some(duration) => {
                let remaining = duration.saturating_sub(started.elapsed());
                if remaining.is_zero() {
                    break;
                }
                remaining.min(poll)
            }
            None => poll,
        };
        match handle.recv_timeout(wait) {
            Ok(event) => {
                if json_lines {
                    write_event_json(&event)?;
                } else {
                    print_event(&event);
                }
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }

    handle
        .stop()
        .map_err(|_| anyhow::anyhow!("monitor thread panicked"))?;
    if !json_lines {
        let _ = write_stdout_best_effort("Monitor stopped.");
    }
    Ok(())
}

fn print_monitor_banner(config: &MonitorConfig, duration: Option<Duration>) {
    let cpu_samples = config.cpu_required_samples();
    let memory_samples = config.memory_required_samples();
    let mut output = String::new();
    output.push_str("Hades monitor\n");
    let _ = writeln!(
        output,
        "Sampling every {:?}; history {:?} samples.",
        config.sampling_interval, config.history_limit
    );
    let _ = writeln!(
        output,
        "CPU: >= {:.1}% for {} samples ({:?}); recovery at <= {:.1}%.",
        config.cpu.threshold_percent,
        cpu_samples,
        config.cpu.sustained_for,
        config.cpu.recovery_threshold_percent
    );
    let _ = writeln!(
        output,
        "Memory: available <= {:.1}% for {} samples ({:?}); recovery at >= {:.1}%.",
        config.memory.available_threshold_percent,
        memory_samples,
        config.memory.sustained_for,
        config.memory.recovery_available_percent
    );
    let _ = writeln!(
        output,
        "Disk: available <= {:.1}%; recovery at >= {:.1}%.",
        config.disk.available_threshold_percent, config.disk.recovery_available_percent
    );
    match duration {
        Some(duration) => {
            let _ = writeln!(output, "Stopping after {duration:?}.");
        }
        None => output.push_str("Press Ctrl+C to stop.\n"),
    }
    let _ = write_stdout_best_effort(&output);
}

fn print_cycle(snapshot: &Snapshot) {
    let cpu = snapshot
        .cpu
        .as_ref()
        .map(|cpu| format!("{:.1}%", cpu.overall_percent))
        .unwrap_or_else(|| "n/a".into());
    let memory = snapshot
        .memory
        .as_ref()
        .and_then(|memory| {
            utilization_percent(memory.available_bytes, memory.total_bytes)
                .map(|available| format!("{available:.1}% available"))
        })
        .unwrap_or_else(|| "n/a".into());
    let line = format!(
        "[{}] CPU {} | memory {} | filesystems {} | collection issues {}",
        snapshot.collected_at,
        cpu,
        memory,
        snapshot.disks.len(),
        snapshot.issues.len()
    );
    let _ = write_stdout_best_effort(&line);
}

fn print_event(event: &MonitorEvent) {
    let timestamp = event.timestamp().to_rfc3339_opts(SecondsFormat::Secs, true);
    let line = match event {
        MonitorEvent::IncidentStarted { incident, .. } => {
            format_incident("INCIDENT STARTED", incident)
        }
        MonitorEvent::IncidentUpdated { incident, .. } => {
            format_incident("incident updated", incident)
        }
        MonitorEvent::IncidentRecovered { incident, .. } => {
            format_incident("INCIDENT RECOVERED", incident)
        }
        MonitorEvent::CollectionError { issues, .. } => {
            let details = issues
                .iter()
                .map(|issue| format!("{}: {}", issue.component, issue.message))
                .collect::<Vec<_>>()
                .join("; ");
            format!("collection error: {details}")
        }
    };
    let _ = write_stdout_best_effort(&format!("[{timestamp}] {line}"));
}

fn format_incident(label: &str, incident: &Incident) -> String {
    let severity = match incident.severity {
        os_agent::incident::Severity::Warning => "warning",
        os_agent::incident::Severity::Critical => "critical",
    };
    format!(
        "{label} [{}] {} {}: {}",
        severity,
        incident.id,
        incident.kind.as_str(),
        incident.summary
    )
}

fn write_event_json(event: &MonitorEvent) -> Result<()> {
    let encoded = serde_json::to_string(event).context("failed to encode monitor event")?;
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    handle
        .write_all(encoded.as_bytes())
        .and_then(|()| handle.write_all(b"\n"))
        .and_then(|()| handle.flush())
        .context("failed to write monitor event")
}

fn write_stdout(text: &str) -> Result<()> {
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    handle
        .write_all(text.as_bytes())
        .context("failed to write to stdout")?;
    if !text.ends_with('\n') {
        handle
            .write_all(b"\n")
            .context("failed to write to stdout")?;
    }
    handle.flush().context("failed to flush stdout")
}

fn write_stdout_best_effort(text: &str) -> io::Result<()> {
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    handle.write_all(text.as_bytes())?;
    if !text.ends_with('\n') {
        handle.write_all(b"\n")?;
    }
    handle.flush()
}

fn warn_about_issues(snapshot: &Snapshot) {
    let stderr = io::stderr();
    let mut handle = stderr.lock();
    for issue in &snapshot.issues {
        // A closed stderr should not turn a successful listing into a crash.
        let _ = writeln!(handle, "warning: {}: {}", issue.component, issue.message);
    }
}
