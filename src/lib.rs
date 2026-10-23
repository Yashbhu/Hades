//! Hades: a local-first system monitoring library.
//!
//! M1 collects CPU, memory, disk, and process metrics into a typed
//! [`Snapshot`](snapshot::Snapshot). M2 adds a deterministic monitoring engine:
//! [`Monitor`] consumes snapshots, maintains bounded [`MetricHistory`], detects
//! sustained resource conditions, and emits typed [`MonitorEvent`] values with
//! an explicit [`Incident`] lifecycle. M3 exposes read-only diagnostics over a
//! loopback JSON HTTP bridge ([`bridge::AgentService`]).
//!
//! Collection ([`snapshot::Collector`]) is separate from detection
//! ([`monitor::Monitor`]), presentation ([`report`]), and transport
//! ([`bridge`]). No AI, remediation, daemon, or external-network functionality
//! is part of these milestones.

pub mod bridge;
pub mod config;
pub mod events;
pub mod history;
pub mod incident;
pub mod metrics;
pub mod monitor;
pub mod report;
pub mod snapshot;

pub use bridge::{AgentService, ServerConfig};
pub use config::{ConfigError, MonitorConfig};
pub use events::MonitorEvent;
pub use history::{MetricHistory, MetricSample, RollingStats, Trend};
pub use incident::{Incident, IncidentKind, IncidentState, Severity};
pub use metrics::ProcessSort;
pub use monitor::{Monitor, MonitorHandle};
pub use snapshot::{Collector, SCHEMA_VERSION, Snapshot};
