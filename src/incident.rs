//! Incident types, evidence, and lifecycle transitions.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::history::{MetricPoint, Trend};

/// Category of detected condition.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IncidentKind {
    /// Sustained high CPU utilization.
    HighCpu,
    /// Sustained low available memory.
    MemoryPressure,
    /// Low available space on one filesystem.
    LowDiskSpace,
}

impl IncidentKind {
    /// Stable label used in logs and JSON.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::HighCpu => "high_cpu",
            Self::MemoryPressure => "memory_pressure",
            Self::LowDiskSpace => "low_disk_space",
        }
    }
}

/// Severity derived from configured trigger and critical thresholds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    /// Condition crossed the trigger threshold but not the critical threshold.
    Warning,
    /// Condition is at or beyond the configured critical threshold.
    Critical,
}

/// Explicit incident lifecycle state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IncidentState {
    /// Condition currently meets the detection rule.
    Active,
    /// Condition remains open and has been seen by the user. This is not
    /// recovery.
    Acknowledged,
    /// Condition returned to the configured recovery range.
    Recovered,
}

impl IncidentState {
    /// Whether the incident still represents an ongoing condition.
    pub fn is_open(self) -> bool {
        matches!(self, Self::Active | Self::Acknowledged)
    }
}

/// Latest measurements and recent samples for a CPU incident.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CpuObserved {
    /// Latest overall CPU utilization in percent.
    pub latest_percent: f64,
    /// Recent valid CPU samples, oldest first.
    pub recent_samples: Vec<MetricPoint>,
}

/// Derived values for a CPU incident.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CpuDerived {
    /// Trigger threshold that was crossed.
    pub threshold_percent: f64,
    /// Recovery threshold.
    pub recovery_threshold_percent: f64,
    /// Critical threshold.
    pub critical_threshold_percent: f64,
    /// Consecutive valid samples above the trigger threshold.
    pub consecutive_samples: usize,
    /// Seconds between the first qualifying sample and the latest one.
    pub condition_duration_seconds: u64,
    /// Average CPU utilization across the retained history window.
    pub recent_average_percent: f64,
    /// Derived direction over the history window.
    pub trend: Trend,
}

/// Latest measurements and recent samples for a memory incident.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryObserved {
    /// Installed memory in bytes.
    pub total_bytes: u64,
    /// Used memory in bytes.
    pub used_bytes: u64,
    /// Available memory in bytes.
    pub available_bytes: u64,
    /// Completely free memory in bytes.
    pub free_bytes: u64,
    /// Used memory percentage reported by the collector.
    pub latest_used_percent: f64,
    /// Available memory percentage derived from available and total bytes.
    pub latest_available_percent: f64,
    /// Recent valid available-memory percentages, oldest first.
    pub recent_available_percent: Vec<MetricPoint>,
}

/// Derived values for a memory incident.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryDerived {
    /// Available-memory trigger threshold.
    pub available_threshold_percent: f64,
    /// Available-memory recovery threshold.
    pub recovery_available_percent: f64,
    /// Available-memory critical threshold.
    pub critical_available_percent: f64,
    /// Consecutive valid samples at or below the trigger threshold.
    pub consecutive_samples: usize,
    /// Seconds between the first qualifying sample and the latest one.
    pub condition_duration_seconds: u64,
    /// Average available memory across the retained history window.
    pub recent_average_available_percent: f64,
    /// Derived direction over the history window.
    pub trend: Trend,
}

/// Latest measurements for a filesystem incident.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DiskObserved {
    /// Mount point of the affected filesystem.
    pub mount_point: String,
    /// Filesystem type when reported.
    pub file_system: Option<String>,
    /// Total capacity in bytes.
    pub total_bytes: u64,
    /// Available capacity in bytes.
    pub available_bytes: u64,
    /// Capacity not available for unprivileged writes.
    pub used_bytes: u64,
    /// Available capacity as a percentage of total.
    pub available_percent: f64,
}

/// Derived values for a filesystem incident.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DiskDerived {
    /// Available-space trigger threshold.
    pub available_threshold_percent: f64,
    /// Available-space recovery threshold.
    pub recovery_available_percent: f64,
    /// Available-space critical threshold.
    pub critical_available_percent: f64,
}

/// Typed evidence for an incident.
///
/// `observed` holds measurements. `derived` holds thresholds, averages,
/// counts, durations, and trends computed from them. A detector never claims a
/// root cause from this evidence.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum IncidentEvidence {
    /// Sustained high CPU evidence.
    HighCpu {
        /// Latest measurements.
        observed: CpuObserved,
        /// Derived values.
        derived: CpuDerived,
    },
    /// Memory pressure evidence.
    MemoryPressure {
        /// Latest measurements.
        observed: MemoryObserved,
        /// Derived values.
        derived: MemoryDerived,
    },
    /// Low disk space evidence.
    LowDiskSpace {
        /// Latest measurements.
        observed: DiskObserved,
        /// Derived values.
        derived: DiskDerived,
    },
}

/// A detected condition with stable identity and explicit state.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Incident {
    /// Stable identifier, unique within one monitor run.
    pub id: String,
    /// Condition category.
    pub kind: IncidentKind,
    /// Mount point for disk incidents; `None` otherwise.
    pub subject: Option<String>,
    /// Latest severity.
    pub severity: Severity,
    /// Lifecycle state.
    pub state: IncidentState,
    /// Time of the first qualifying measurement.
    pub started_at: DateTime<Utc>,
    /// Time of the most recent state or severity update.
    pub updated_at: DateTime<Utc>,
    /// Time of the most recent valid observation.
    pub last_observed_at: DateTime<Utc>,
    /// Set when the incident recovered.
    pub recovered_at: Option<DateTime<Utc>>,
    /// Short, factual description of the latest observation.
    pub summary: String,
    /// Measurements and derived values supporting the incident.
    pub evidence: IncidentEvidence,
}

impl Incident {
    /// Whether the condition is still considered open.
    pub fn is_open(&self) -> bool {
        self.state.is_open()
    }

    /// Mark an active incident as acknowledged.
    ///
    /// Acknowledging does not recover the incident and does not stop updates.
    /// Returns `false` for already-acknowledged or recovered incidents.
    pub fn acknowledge_at(&mut self, at: DateTime<Utc>) -> bool {
        if self.state == IncidentState::Active {
            self.state = IncidentState::Acknowledged;
            self.updated_at = at;
            true
        } else {
            false
        }
    }

    /// Mark an open incident as recovered. Returns `false` if already recovered.
    pub fn recover_at(&mut self, at: DateTime<Utc>) -> bool {
        if self.state.is_open() {
            self.state = IncidentState::Recovered;
            self.recovered_at = Some(at);
            self.updated_at = at;
            true
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CpuDerived, CpuObserved, Incident, IncidentEvidence, IncidentKind, IncidentState, Severity,
    };
    use crate::history::Trend;
    use chrono::{TimeZone, Utc};

    fn at(seconds: u32) -> chrono::DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, seconds).unwrap()
    }

    fn incident() -> Incident {
        Incident {
            id: "inc-000001".into(),
            kind: IncidentKind::HighCpu,
            subject: None,
            severity: Severity::Warning,
            state: IncidentState::Active,
            started_at: at(0),
            updated_at: at(0),
            last_observed_at: at(0),
            recovered_at: None,
            summary: "CPU utilization is 95.0%".into(),
            evidence: IncidentEvidence::HighCpu {
                observed: CpuObserved {
                    latest_percent: 95.0,
                    recent_samples: vec![],
                },
                derived: CpuDerived {
                    threshold_percent: 90.0,
                    recovery_threshold_percent: 85.0,
                    critical_threshold_percent: 95.0,
                    consecutive_samples: 6,
                    condition_duration_seconds: 25,
                    recent_average_percent: 94.0,
                    trend: Trend::Stable,
                },
            },
        }
    }

    #[test]
    fn active_can_be_acknowledged_without_recovering() {
        let mut incident = incident();
        assert!(incident.acknowledge_at(at(1)));
        assert_eq!(incident.state, IncidentState::Acknowledged);
        assert!(incident.is_open());
        assert!(incident.recovered_at.is_none());
        assert_eq!(incident.updated_at, at(1));
    }

    #[test]
    fn acknowledged_incident_can_recover() {
        let mut incident = incident();
        assert!(incident.acknowledge_at(at(1)));
        assert!(incident.recover_at(at(2)));
        assert_eq!(incident.state, IncidentState::Recovered);
        assert!(!incident.is_open());
        assert_eq!(incident.recovered_at, Some(at(2)));
    }

    #[test]
    fn invalid_transitions_are_rejected() {
        let mut incident = incident();
        assert!(incident.recover_at(at(1)));
        assert!(!incident.recover_at(at(2)));
        assert!(!incident.acknowledge_at(at(2)));
    }

    #[test]
    fn recovered_incident_cannot_be_acknowledged() {
        let mut incident = incident();
        incident.recover_at(at(1));
        assert!(!incident.acknowledge_at(at(2)));
        assert_eq!(incident.state, IncidentState::Recovered);
    }
}
