//! Structured events emitted by the monitoring engine.
//!
//! Events are the library boundary for future orchestration. They contain only
//! typed, serializable data and never depend on terminal output.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::incident::Incident;
use crate::snapshot::CollectionIssue;

/// An event produced by one monitoring cycle.
///
/// Serialize with `serde_json::to_string` to obtain one JSON Lines record.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum MonitorEvent {
    /// A condition newly met its detection rule.
    IncidentStarted {
        /// When the event was produced.
        timestamp: DateTime<Utc>,
        /// Newly created incident.
        incident: Incident,
    },
    /// An open incident was observed again.
    IncidentUpdated {
        /// When the event was produced.
        timestamp: DateTime<Utc>,
        /// Updated incident, including its current state and severity.
        incident: Incident,
    },
    /// An incident returned to its recovery range.
    IncidentRecovered {
        /// When the event was produced.
        timestamp: DateTime<Utc>,
        /// Recovered incident.
        incident: Incident,
    },
    /// A snapshot had partial collection failures.
    ///
    /// Emitted when the set of issues changes, not on every cycle with the same
    /// issues.
    CollectionError {
        /// When the event was produced.
        timestamp: DateTime<Utc>,
        /// Collection issues from the snapshot.
        issues: Vec<CollectionIssue>,
    },
}

impl MonitorEvent {
    /// Timestamp shared by the event and the observation that produced it.
    pub fn timestamp(&self) -> &DateTime<Utc> {
        match self {
            Self::IncidentStarted { timestamp, .. }
            | Self::IncidentUpdated { timestamp, .. }
            | Self::IncidentRecovered { timestamp, .. }
            | Self::CollectionError { timestamp, .. } => timestamp,
        }
    }

    /// Stable event label (`incident_started`, etc.).
    pub fn kind(&self) -> &'static str {
        match self {
            Self::IncidentStarted { .. } => "incident_started",
            Self::IncidentUpdated { .. } => "incident_updated",
            Self::IncidentRecovered { .. } => "incident_recovered",
            Self::CollectionError { .. } => "collection_error",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::MonitorEvent;
    use crate::history::Trend;
    use crate::incident::{
        CpuDerived, CpuObserved, Incident, IncidentEvidence, IncidentKind, IncidentState, Severity,
    };
    use crate::snapshot::CollectionIssue;
    use chrono::{TimeZone, Utc};

    fn incident() -> Incident {
        Incident {
            id: "inc-000001".into(),
            kind: IncidentKind::HighCpu,
            subject: None,
            severity: Severity::Warning,
            state: IncidentState::Active,
            started_at: Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap(),
            updated_at: Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 30).unwrap(),
            last_observed_at: Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 30).unwrap(),
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
                    trend: Trend::Rising,
                },
            },
        }
    }

    #[test]
    fn serializes_with_an_event_tag() {
        let timestamp = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 30).unwrap();
        let event = MonitorEvent::IncidentStarted {
            timestamp,
            incident: incident(),
        };
        let value = serde_json::to_value(&event).unwrap();
        assert_eq!(value["event"], "incident_started");
        assert_eq!(value["incident"]["id"], "inc-000001");
        assert_eq!(value["incident"]["state"], "active");
        assert_eq!(value["incident"]["kind"], "high_cpu");
        assert_eq!(value["incident"]["evidence"]["kind"], "high_cpu");
        assert_eq!(event.kind(), "incident_started");
        assert_eq!(event.timestamp(), &timestamp);
    }

    #[test]
    fn collection_error_serializes_as_one_line() {
        let event = MonitorEvent::CollectionError {
            timestamp: Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap(),
            issues: vec![CollectionIssue {
                component: "memory".into(),
                message: "unavailable".into(),
            }],
        };
        let encoded = serde_json::to_string(&event).unwrap();
        assert!(!encoded.contains('\n'));
        let value: serde_json::Value = serde_json::from_str(&encoded).unwrap();
        assert_eq!(value["event"], "collection_error");
        assert_eq!(value["issues"][0]["component"], "memory");
    }

    #[test]
    fn events_round_trip_through_json() {
        let event = MonitorEvent::IncidentUpdated {
            timestamp: Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 30).unwrap(),
            incident: incident(),
        };
        let encoded = serde_json::to_string(&event).unwrap();
        let decoded: MonitorEvent = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded, event);
    }
}
