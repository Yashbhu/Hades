//! Hades M1: a local-first system monitoring library.
//!
//! This crate collects CPU, memory, disk, and process metrics into a typed
//! [`Snapshot`](snapshot::Snapshot). Collection is separated from rendering:
//! [`snapshot::Collector`] is the only type that talks to the operating system
//! through `sysinfo`, while [`report`] turns a snapshot into human-readable
//! text.
//!
//! No AI, remediation, daemon, or network functionality is part of this
//! milestone.

pub mod metrics;
pub mod report;
pub mod snapshot;

pub use metrics::ProcessSort;
pub use snapshot::{Collector, SCHEMA_VERSION, Snapshot};
