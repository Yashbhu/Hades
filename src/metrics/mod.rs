//! Metric types shared by the collector and the CLI.

pub mod cpu;
pub mod disk;
pub mod memory;
pub mod processes;

pub use processes::ProcessSort;

/// Default number of processes included in a snapshot or process listing.
pub const DEFAULT_PROCESS_LIMIT: usize = 10;

/// Upper bound accepted by the CLI for `processes --limit`.
pub const MAX_PROCESS_LIMIT: usize = 100;

/// Calculate `used / total` as a percentage in the range `0.0..=100.0`.
///
/// Returns `None` when the total is zero, which means the metric is
/// unavailable rather than a legitimate `0%` reading. Values above the total
/// are clamped so a transient accounting inconsistency cannot produce a
/// nonsensical percentage.
pub fn utilization_percent(used: u64, total: u64) -> Option<f64> {
    if total == 0 {
        return None;
    }
    Some((used.min(total) as f64 / total as f64) * 100.0)
}

#[cfg(test)]
mod tests {
    use super::utilization_percent;

    #[test]
    fn calculates_percentage() {
        assert_eq!(utilization_percent(50, 200), Some(25.0));
        let third = utilization_percent(1, 3).expect("one third should be valid");
        assert!((third - 100.0 / 3.0).abs() < f64::EPSILON * 100.0);
    }

    #[test]
    fn zero_used_is_a_valid_zero_percent() {
        assert_eq!(utilization_percent(0, 100), Some(0.0));
    }

    #[test]
    fn zero_total_is_unavailable() {
        assert_eq!(utilization_percent(0, 0), None);
        assert_eq!(utilization_percent(10, 0), None);
    }

    #[test]
    fn used_above_total_is_clamped() {
        assert_eq!(utilization_percent(150, 100), Some(100.0));
    }

    #[test]
    fn full_usage_is_one_hundred_percent() {
        assert_eq!(utilization_percent(100, 100), Some(100.0));
    }
}
