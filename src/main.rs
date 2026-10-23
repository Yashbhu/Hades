use std::io::{self, Write as _};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand, ValueEnum};
use os_agent::metrics::{DEFAULT_PROCESS_LIMIT, MAX_PROCESS_LIMIT, ProcessSort};
use os_agent::report;
use os_agent::snapshot::{Collector, Snapshot};

/// Local-first system health monitor (M1).
#[derive(Debug, Parser)]
#[command(name = "os-agent", version, about, long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
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
    }
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

fn warn_about_issues(snapshot: &Snapshot) {
    let stderr = io::stderr();
    let mut handle = stderr.lock();
    for issue in &snapshot.issues {
        // A closed stderr should not turn a successful listing into a crash.
        let _ = writeln!(handle, "warning: {}: {}", issue.component, issue.message);
    }
}
