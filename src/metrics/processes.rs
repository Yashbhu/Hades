//! Bounded top-process collection.
//!
//! Only the fields needed for the health snapshot are exposed: PID, process
//! name, CPU utilization, and resident memory. Command-line arguments,
//! environment variables, and executable paths are deliberately not collected.
//!
//! `sysinfo` reports process CPU relative to a single logical core, so a busy
//! multi-threaded process can legitimately exceed 100%. The value is not
//! normalized by the core count; consumers should read it as "percent of one
//! core".

use serde::{Deserialize, Serialize};
use sysinfo::{Process, System};

/// Metric used to rank processes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessSort {
    /// Highest CPU utilization first.
    Cpu,
    /// Highest memory consumption first.
    Memory,
}

impl ProcessSort {
    /// Stable label used in JSON and human-readable output.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Cpu => "cpu",
            Self::Memory => "memory",
        }
    }
}

/// Resource usage for a single process.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProcessMetrics {
    /// Process identifier.
    pub pid: u32,
    /// Process name, lossily converted to UTF-8 when necessary.
    pub name: String,
    /// Percent of one logical core. May exceed 100 on multi-threaded processes.
    pub cpu_percent: f64,
    /// Resident memory, in bytes.
    pub memory_bytes: u64,
}

impl ProcessMetrics {
    /// Extract the reportable fields from a `sysinfo` process.
    ///
    /// Returns `None` if the process disappears or reports a non-finite CPU
    /// value while it is being read.
    pub fn from_process(process: &Process) -> Option<Self> {
        let cpu_percent = f64::from(process.cpu_usage());
        if !cpu_percent.is_finite() || cpu_percent < 0.0 {
            return None;
        }
        let name = process.name().to_string_lossy();
        let name = if name.is_empty() {
            "unknown".to_owned()
        } else {
            name.into_owned()
        };
        Some(Self {
            pid: process.pid().as_u32(),
            name,
            cpu_percent,
            memory_bytes: process.memory(),
        })
    }
}

/// The bounded, ordered process list included in a snapshot.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TopProcesses {
    /// Metric the list is sorted by.
    pub sorted_by: ProcessSort,
    /// Requested maximum number of items.
    pub limit: usize,
    /// Up to `limit` processes, highest resource use first.
    pub items: Vec<ProcessMetrics>,
}

impl TopProcesses {
    /// Sort and truncate an arbitrary set of process metrics.
    pub fn from_processes(
        processes: impl IntoIterator<Item = ProcessMetrics>,
        sorted_by: ProcessSort,
        limit: usize,
    ) -> Self {
        let mut items: Vec<ProcessMetrics> = processes.into_iter().collect();
        items.sort_by(|left, right| match sorted_by {
            ProcessSort::Cpu => right
                .cpu_percent
                .total_cmp(&left.cpu_percent)
                .then_with(|| left.name.cmp(&right.name))
                .then_with(|| left.pid.cmp(&right.pid)),
            ProcessSort::Memory => right
                .memory_bytes
                .cmp(&left.memory_bytes)
                .then_with(|| left.name.cmp(&right.name))
                .then_with(|| left.pid.cmp(&right.pid)),
        });
        items.truncate(limit);
        Self {
            sorted_by,
            limit,
            items,
        }
    }

    /// Collect the top processes from an already-refreshed [`System`].
    pub fn from_system(system: &System, sorted_by: ProcessSort, limit: usize) -> Self {
        Self::from_processes(
            system
                .processes()
                .values()
                .filter_map(ProcessMetrics::from_process),
            sorted_by,
            limit,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::{ProcessMetrics, ProcessSort, TopProcesses};

    fn process(pid: u32, name: &str, cpu_percent: f64, memory_bytes: u64) -> ProcessMetrics {
        ProcessMetrics {
            pid,
            name: name.to_owned(),
            cpu_percent,
            memory_bytes,
        }
    }

    fn sample() -> Vec<ProcessMetrics> {
        vec![
            process(1, "alpha", 10.0, 900),
            process(2, "bravo", 50.0, 100),
            process(3, "charlie", 0.0, 500),
            process(4, "delta", 50.0, 300),
        ]
    }

    #[test]
    fn sorts_by_cpu_descending() {
        let top = TopProcesses::from_processes(sample(), ProcessSort::Cpu, 4);
        let pids: Vec<u32> = top.items.iter().map(|item| item.pid).collect();
        assert_eq!(pids, vec![2, 4, 1, 3]);
    }

    #[test]
    fn sorts_by_memory_descending() {
        let top = TopProcesses::from_processes(sample(), ProcessSort::Memory, 4);
        let pids: Vec<u32> = top.items.iter().map(|item| item.pid).collect();
        assert_eq!(pids, vec![1, 3, 4, 2]);
    }

    #[test]
    fn truncates_to_limit_and_records_requested_limit() {
        let top = TopProcesses::from_processes(sample(), ProcessSort::Cpu, 2);
        assert_eq!(top.items.len(), 2);
        assert_eq!(top.limit, 2);
        assert_eq!(top.sorted_by, ProcessSort::Cpu);
    }

    #[test]
    fn zero_limit_produces_empty_list() {
        let top = TopProcesses::from_processes(sample(), ProcessSort::Memory, 0);
        assert!(top.items.is_empty());
    }

    #[test]
    fn ties_are_broken_by_name_then_pid() {
        let ties = vec![
            process(9, "zulu", 5.0, 10),
            process(8, "alpha", 5.0, 10),
            process(7, "alpha", 5.0, 10),
        ];
        let top = TopProcesses::from_processes(ties, ProcessSort::Cpu, 3);
        let pids: Vec<u32> = top.items.iter().map(|item| item.pid).collect();
        assert_eq!(pids, vec![7, 8, 9]);
    }

    #[test]
    fn limit_above_available_items_keeps_all() {
        let top = TopProcesses::from_processes(sample(), ProcessSort::Cpu, 100);
        assert_eq!(top.items.len(), 4);
    }
}
