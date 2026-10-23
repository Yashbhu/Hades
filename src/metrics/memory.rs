//! Physical memory metrics.
//!
//! All sizes are bytes. `available_bytes` is the memory the operating system
//! can hand to a new application without swapping (this includes reclaimable
//! page cache where applicable). `free_bytes` is memory that is completely
//! unused. The two values differ on most operating systems, which is why both
//! are reported.

use serde::{Deserialize, Serialize};
use sysinfo::System;

use super::utilization_percent;

/// Physical memory usage.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryMetrics {
    /// Installed physical memory, in bytes.
    pub total_bytes: u64,
    /// Memory currently in use, in bytes.
    pub used_bytes: u64,
    /// Memory available to applications without swapping, in bytes.
    pub available_bytes: u64,
    /// Memory that is completely unused, in bytes.
    pub free_bytes: u64,
    /// `used_bytes / total_bytes` as a percentage.
    pub used_percent: f64,
}

impl MemoryMetrics {
    /// Build memory metrics from raw byte counts.
    ///
    /// Returns `None` when the total is zero, meaning the platform did not
    /// report usable memory information.
    pub fn from_raw(
        total_bytes: u64,
        used_bytes: u64,
        available_bytes: u64,
        free_bytes: u64,
    ) -> Option<Self> {
        Some(Self {
            total_bytes,
            used_bytes,
            available_bytes,
            free_bytes,
            used_percent: utilization_percent(used_bytes, total_bytes)?,
        })
    }

    /// Read memory metrics from a refreshed [`System`].
    pub fn from_system(system: &System) -> Option<Self> {
        Self::from_raw(
            system.total_memory(),
            system.used_memory(),
            system.available_memory(),
            system.free_memory(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::MemoryMetrics;

    #[test]
    fn calculates_usage_percentage() {
        let metrics = MemoryMetrics::from_raw(1_000, 250, 700, 100).unwrap();
        assert_eq!(metrics.total_bytes, 1_000);
        assert_eq!(metrics.used_bytes, 250);
        assert_eq!(metrics.available_bytes, 700);
        assert_eq!(metrics.free_bytes, 100);
        assert_eq!(metrics.used_percent, 25.0);
    }

    #[test]
    fn zero_total_is_unavailable() {
        assert!(MemoryMetrics::from_raw(0, 0, 0, 0).is_none());
    }

    #[test]
    fn preserves_available_and_free_as_distinct_values() {
        let metrics = MemoryMetrics::from_raw(1_000, 400, 300, 100).unwrap();
        assert_ne!(metrics.available_bytes, metrics.free_bytes);
    }
}
