//! CPU utilization metrics.
//!
//! CPU usage reported by `sysinfo` is a delta between two refreshes. The
//! collector therefore performs an explicit warm-up refresh, waits for at
//! least [`sysinfo::MINIMUM_CPU_UPDATE_INTERVAL`], and refreshes again before
//! reading values. A reading taken before that interval is not treated as a
//! valid measurement.

use serde::{Deserialize, Serialize};
use sysinfo::System;

/// CPU utilization for the whole machine and, when available, per logical core.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CpuMetrics {
    /// Average utilization across all logical cores, in percent (`0.0..=100.0`).
    pub overall_percent: f64,
    /// Utilization per logical core. Empty when per-core data was not reported.
    pub per_core_percent: Vec<f64>,
    /// Number of logical cores (for example, 8 on a 4-core machine with SMT).
    pub logical_core_count: usize,
    /// How long the collector waited between the two CPU samples.
    pub sampling_interval_ms: u64,
}

impl CpuMetrics {
    /// Build CPU metrics from already-sampled values.
    ///
    /// Returns `None` when the overall value is not a finite, non-negative
    /// number. Per-core values are included only when every core value is
    /// valid; otherwise the vector is left empty rather than reporting zeros
    /// for missing data.
    pub fn new(
        overall_percent: f64,
        per_core_percent: Vec<f64>,
        sampling_interval_ms: u64,
    ) -> Option<Self> {
        if !valid_percent(overall_percent) {
            return None;
        }
        let per_core_percent = if per_core_percent.iter().all(|value| valid_percent(*value)) {
            per_core_percent
        } else {
            Vec::new()
        };
        let logical_core_count = per_core_percent.len();
        Some(Self {
            overall_percent: overall_percent.min(100.0),
            per_core_percent: per_core_percent
                .into_iter()
                .map(|value| value.min(100.0))
                .collect(),
            logical_core_count,
            sampling_interval_ms,
        })
    }

    /// Read CPU metrics from a system that has already been sampled twice.
    pub fn from_system(system: &System, sampling_interval_ms: u64) -> Option<Self> {
        let cpus = system.cpus();
        if cpus.is_empty() {
            return None;
        }
        let per_core: Vec<f64> = cpus.iter().map(|cpu| f64::from(cpu.cpu_usage())).collect();
        let mut metrics = Self::new(
            f64::from(system.global_cpu_usage()),
            per_core,
            sampling_interval_ms,
        )?;
        metrics.logical_core_count = cpus.len();
        Some(metrics)
    }
}

fn valid_percent(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

#[cfg(test)]
mod tests {
    use super::CpuMetrics;

    #[test]
    fn accepts_valid_values_and_sets_core_count_later() {
        let metrics = CpuMetrics::new(12.5, vec![10.0, 15.0], 200).unwrap();
        assert_eq!(metrics.overall_percent, 12.5);
        assert_eq!(metrics.per_core_percent, vec![10.0, 15.0]);
        assert_eq!(metrics.sampling_interval_ms, 200);
    }

    #[test]
    fn rejects_uninitialized_or_invalid_overall_reading() {
        assert!(CpuMetrics::new(f64::NAN, vec![], 200).is_none());
        assert!(CpuMetrics::new(f64::INFINITY, vec![], 200).is_none());
        assert!(CpuMetrics::new(-1.0, vec![], 200).is_none());
    }

    #[test]
    fn drops_per_core_values_if_any_core_is_invalid() {
        let metrics = CpuMetrics::new(5.0, vec![1.0, f64::NAN], 200).unwrap();
        assert!(metrics.per_core_percent.is_empty());
    }

    #[test]
    fn clamps_values_above_one_hundred() {
        let metrics = CpuMetrics::new(120.0, vec![150.0], 200).unwrap();
        assert_eq!(metrics.overall_percent, 100.0);
        assert_eq!(metrics.per_core_percent, vec![100.0]);
    }
}
