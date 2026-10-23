//! Collection of all metrics into one serializable snapshot.

use std::collections::HashSet;
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use sysinfo::{Disks, MINIMUM_CPU_UPDATE_INTERVAL, ProcessesToUpdate, System};

use crate::metrics::cpu::CpuMetrics;
use crate::metrics::disk::DiskMetrics;
use crate::metrics::memory::MemoryMetrics;
use crate::metrics::processes::{ProcessSort, TopProcesses};
use crate::metrics::{DEFAULT_PROCESS_LIMIT, MAX_PROCESS_LIMIT};

/// Version of the serialized snapshot schema.
pub const SCHEMA_VERSION: u32 = 1;

/// A metric that could not be collected, or was only partially collected.
///
/// The presence of an issue never invalidates the rest of the snapshot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CollectionIssue {
    /// Component that reported the problem (`cpu`, `memory`, `disk`, `processes`).
    pub component: String,
    /// Human-readable explanation, safe to display.
    pub message: String,
}

impl CollectionIssue {
    fn new(component: &str, message: impl Into<String>) -> Self {
        Self {
            component: component.to_owned(),
            message: message.into(),
        }
    }
}

/// A point-in-time view of system health.
///
/// Optional metrics are `null` in JSON when they could not be collected;
/// `issues` explains why. A `0.0` percentage is always a real measurement.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    /// Schema version, currently [`SCHEMA_VERSION`].
    pub schema_version: u32,
    /// RFC 3339 timestamp in UTC, with millisecond precision.
    pub collected_at: String,
    /// CPU utilization, or `null` when unavailable.
    pub cpu: Option<CpuMetrics>,
    /// Physical memory usage, or `null` when unavailable.
    pub memory: Option<MemoryMetrics>,
    /// Mounted filesystems with readable capacity data.
    pub disks: Vec<DiskMetrics>,
    /// Bounded list of top processes.
    pub processes: TopProcesses,
    /// Partial-collection problems. Empty when everything was collected.
    pub issues: Vec<CollectionIssue>,
}

/// Stateful collector for live system metrics.
///
/// A collector keeps its `sysinfo` state between calls, which is what makes
/// CPU deltas meaningful. Construct it once and call [`Collector::collect`]
/// for each snapshot.
///
/// Construction performs an initial refresh. `collect` then refreshes again
/// after [`MINIMUM_CPU_UPDATE_INTERVAL`] so the first reported CPU value is a
/// real delta rather than an uninitialized reading.
pub struct Collector {
    system: System,
    disks: Disks,
    process_sort: ProcessSort,
    process_limit: usize,
    sample_interval: Duration,
}

impl Default for Collector {
    fn default() -> Self {
        Self::new()
    }
}

impl Collector {
    /// Create a collector using the default process ranking (CPU, 10 items).
    pub fn new() -> Self {
        Self::with_processes(ProcessSort::Cpu, DEFAULT_PROCESS_LIMIT)
    }

    /// Create a collector with a specific process ranking and limit.
    ///
    /// `limit` is truncated to [`MAX_PROCESS_LIMIT`] so library callers cannot
    /// accidentally request an unbounded list.
    pub fn with_processes(process_sort: ProcessSort, process_limit: usize) -> Self {
        let mut system = System::new_all();
        // Ensure the CPU baseline exists before the timed refresh in `collect`.
        system.refresh_cpu_usage();
        Self {
            system,
            disks: Disks::new_with_refreshed_list(),
            process_sort,
            process_limit: process_limit.min(MAX_PROCESS_LIMIT),
            sample_interval: MINIMUM_CPU_UPDATE_INTERVAL,
        }
    }

    /// Collect a complete snapshot. Never panics on missing metrics.
    pub fn collect(&mut self) -> Snapshot {
        let mut issues = Vec::new();

        std::thread::sleep(self.sample_interval);
        self.system.refresh_cpu_usage();
        self.system.refresh_memory();
        let refreshed_processes = self.system.refresh_processes(ProcessesToUpdate::All, true);
        self.disks.refresh(true);

        let cpu = CpuMetrics::from_system(
            &self.system,
            u64::try_from(self.sample_interval.as_millis()).unwrap_or(u64::MAX),
        );
        match &cpu {
            None => issues.push(CollectionIssue::new(
                "cpu",
                "CPU utilization is unavailable; the platform reported no CPU data",
            )),
            Some(metrics) if metrics.per_core_percent.is_empty() => issues.push(
                CollectionIssue::new("cpu", "Per-core CPU utilization is unavailable"),
            ),
            Some(_) => {}
        }

        let memory = MemoryMetrics::from_system(&self.system);
        if memory.is_none() {
            issues.push(CollectionIssue::new(
                "memory",
                "Memory utilization is unavailable; total memory was reported as zero",
            ));
        }

        let mut disks = Vec::new();
        let mut seen_mounts = HashSet::new();
        for disk in self.disks.list() {
            let mount_point = disk.mount_point().to_string_lossy().into_owned();
            if !seen_mounts.insert(mount_point.clone()) {
                continue;
            }
            match DiskMetrics::from_disk(disk) {
                Some(metrics) => disks.push(metrics),
                None => issues.push(CollectionIssue::new(
                    "disk",
                    format!("Capacity information is unavailable for filesystem '{mount_point}'"),
                )),
            }
        }
        if disks.is_empty() {
            issues.push(CollectionIssue::new(
                "disk",
                "No mounted filesystem with readable capacity information was reported",
            ));
        }

        if refreshed_processes == 0 {
            issues.push(CollectionIssue::new(
                "processes",
                "The platform reported no running processes",
            ));
        }
        let processes =
            TopProcesses::from_system(&self.system, self.process_sort, self.process_limit);

        Snapshot {
            schema_version: SCHEMA_VERSION,
            collected_at: Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true),
            cpu,
            memory,
            disks,
            processes,
            issues,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{CollectionIssue, SCHEMA_VERSION, Snapshot};
    use crate::metrics::ProcessSort;
    use crate::metrics::cpu::CpuMetrics;
    use crate::metrics::disk::DiskMetrics;
    use crate::metrics::memory::MemoryMetrics;
    use crate::metrics::processes::{ProcessMetrics, TopProcesses};

    fn complete_snapshot() -> Snapshot {
        Snapshot {
            schema_version: SCHEMA_VERSION,
            collected_at: "2026-01-02T03:04:05.678Z".to_owned(),
            cpu: Some(CpuMetrics::new(12.5, vec![10.0, 15.0], 200).unwrap()),
            memory: Some(MemoryMetrics::from_raw(1_000, 400, 500, 100).unwrap()),
            disks: vec![
                DiskMetrics::from_raw("/".into(), Some("apfs".into()), 1_000, 250).unwrap(),
            ],
            processes: TopProcesses::from_processes(
                vec![ProcessMetrics {
                    pid: 42,
                    name: "worker".into(),
                    cpu_percent: 20.0,
                    memory_bytes: 1_024,
                }],
                ProcessSort::Cpu,
                10,
            ),
            issues: vec![],
        }
    }

    #[test]
    fn serializes_documented_schema() {
        let value = serde_json::to_value(complete_snapshot()).unwrap();
        assert_eq!(value["schema_version"], SCHEMA_VERSION);
        assert_eq!(value["collected_at"], "2026-01-02T03:04:05.678Z");
        assert_eq!(value["cpu"]["overall_percent"], 12.5);
        assert_eq!(value["cpu"]["logical_core_count"], 2);
        assert_eq!(value["cpu"]["sampling_interval_ms"], 200);
        assert_eq!(value["memory"]["total_bytes"], 1_000);
        assert_eq!(value["memory"]["used_percent"], 40.0);
        assert_eq!(value["disks"][0]["mount_point"], "/");
        assert_eq!(value["disks"][0]["used_percent"], 75.0);
        assert_eq!(value["processes"]["sorted_by"], "cpu");
        assert_eq!(value["processes"]["items"][0]["pid"], 42);
        assert_eq!(value["issues"], serde_json::json!([]));
    }

    #[test]
    fn unavailable_metrics_serialize_as_null_not_zero() {
        let snapshot = Snapshot {
            schema_version: SCHEMA_VERSION,
            collected_at: "2026-01-02T03:04:05.678Z".to_owned(),
            cpu: None,
            memory: None,
            disks: vec![],
            processes: TopProcesses::from_processes(vec![], ProcessSort::Cpu, 10),
            issues: vec![CollectionIssue::new("cpu", "unavailable")],
        };
        let value = serde_json::to_value(snapshot).unwrap();
        assert!(value["cpu"].is_null());
        assert!(value["memory"].is_null());
        assert_eq!(value["issues"][0]["component"], "cpu");
    }

    #[test]
    fn snapshot_round_trips_through_json() {
        let snapshot = complete_snapshot();
        let json = serde_json::to_string(&snapshot).unwrap();
        let decoded: Snapshot = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded, snapshot);
    }

    #[test]
    fn timestamp_is_rfc3339() {
        let snapshot = complete_snapshot();
        let parsed = chrono::DateTime::parse_from_rfc3339(&snapshot.collected_at);
        assert!(parsed.is_ok());
    }
}
