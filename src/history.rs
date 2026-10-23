//! Bounded rolling history for CPU and memory observations.
//!
//! Missing readings are stored as `None` alongside their timestamp. They are
//! never treated as zero, and rolling statistics ignore them. Disk history is
//! not retained: low-disk detection only needs the current capacity report and
//! open incidents, which keeps memory bounded even on hosts whose mounts
//! change frequently.

use std::collections::VecDeque;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// One observation of the metrics used by anomaly detection.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetricSample {
    /// When the snapshot was observed.
    pub observed_at: DateTime<Utc>,
    /// Overall CPU utilization, or `None` when unavailable.
    pub cpu_percent: Option<f64>,
    /// Available physical memory as a percentage of total, or `None`.
    pub memory_available_percent: Option<f64>,
    /// Used physical memory as a percentage of total, or `None`.
    pub memory_used_percent: Option<f64>,
}

/// A single timestamped metric value used in incident evidence.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetricPoint {
    /// Observation timestamp.
    pub observed_at: DateTime<Utc>,
    /// Observed value.
    pub value: f64,
}

/// Direction of the recent trend over the stored window.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Trend {
    /// The second half is more than one percentage point above the first half.
    Rising,
    /// The second half is more than one percentage point below the first half.
    Falling,
    /// No meaningful change between halves.
    Stable,
}

/// Rolling statistics over valid samples in the history window.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RollingStats {
    /// Number of valid (non-missing) samples used.
    pub samples: usize,
    /// Mean value.
    pub average: f64,
    /// Lowest value.
    pub minimum: f64,
    /// Highest value.
    pub maximum: f64,
    /// Derived trend, not an observed fact.
    pub trend: Trend,
}

/// Fixed-capacity history of [`MetricSample`] values.
#[derive(Debug, Clone)]
pub struct MetricHistory {
    capacity: usize,
    samples: VecDeque<MetricSample>,
}

impl MetricHistory {
    /// Create a history that holds at most `capacity` samples.
    ///
    /// A zero capacity is treated as one so that the history can never grow
    /// without bound or lose its most recent sample.
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity: capacity.max(1),
            samples: VecDeque::with_capacity(capacity.clamp(1, 1024)),
        }
    }

    /// Append a sample, evicting the oldest one when at capacity.
    pub fn push(&mut self, sample: MetricSample) {
        if self.samples.len() == self.capacity {
            self.samples.pop_front();
        }
        self.samples.push_back(sample);
    }

    /// Maximum number of samples retained.
    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Number of samples currently retained.
    pub fn len(&self) -> usize {
        self.samples.len()
    }

    /// Whether the history is empty.
    pub fn is_empty(&self) -> bool {
        self.samples.is_empty()
    }

    /// Samples in chronological order.
    pub fn samples(&self) -> &VecDeque<MetricSample> {
        &self.samples
    }

    /// Rolling CPU statistics over valid samples.
    pub fn cpu_stats(&self) -> Option<RollingStats> {
        stats_from(self.samples.iter().filter_map(|sample| sample.cpu_percent))
    }

    /// Rolling available-memory statistics over valid samples.
    pub fn memory_available_stats(&self) -> Option<RollingStats> {
        stats_from(
            self.samples
                .iter()
                .filter_map(|sample| sample.memory_available_percent),
        )
    }

    /// Most recent CPU points, newest last, capped at `limit`.
    pub fn recent_cpu(&self, limit: usize) -> Vec<MetricPoint> {
        recent_from(
            self.samples
                .iter()
                .filter_map(|sample| Some((sample.observed_at, sample.cpu_percent?))),
            limit,
        )
    }

    /// Most recent available-memory points, newest last, capped at `limit`.
    pub fn recent_memory_available(&self, limit: usize) -> Vec<MetricPoint> {
        recent_from(
            self.samples
                .iter()
                .filter_map(|sample| Some((sample.observed_at, sample.memory_available_percent?))),
            limit,
        )
    }
}

fn stats_from(values: impl Iterator<Item = f64>) -> Option<RollingStats> {
    let values: Vec<f64> = values.filter(|value| value.is_finite()).collect();
    if values.is_empty() {
        return None;
    }
    let count = values.len();
    let sum: f64 = values.iter().sum();
    let average = sum / count as f64;
    let minimum = values.iter().copied().fold(f64::INFINITY, f64::min);
    let maximum = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    Some(RollingStats {
        samples: count,
        average,
        minimum,
        maximum,
        trend: trend_of(&values),
    })
}

fn trend_of(values: &[f64]) -> Trend {
    if values.len() < 2 {
        return Trend::Stable;
    }
    let middle = values.len() / 2;
    let first = values[..middle].iter().sum::<f64>() / middle as f64;
    let second = values[middle..].iter().sum::<f64>() / (values.len() - middle) as f64;
    let delta = second - first;
    const TREND_THRESHOLD_PERCENT: f64 = 1.0;
    if delta > TREND_THRESHOLD_PERCENT {
        Trend::Rising
    } else if delta < -TREND_THRESHOLD_PERCENT {
        Trend::Falling
    } else {
        Trend::Stable
    }
}

fn recent_from(
    points: impl Iterator<Item = (DateTime<Utc>, f64)>,
    limit: usize,
) -> Vec<MetricPoint> {
    let mut points: Vec<MetricPoint> = points
        .filter(|(_, value)| value.is_finite())
        .map(|(observed_at, value)| MetricPoint { observed_at, value })
        .collect();
    if points.len() > limit {
        points.drain(..points.len() - limit);
    }
    points
}

#[cfg(test)]
mod tests {
    use super::{MetricHistory, MetricSample, Trend};
    use chrono::{Duration, TimeZone, Utc};

    fn at(seconds: i64) -> chrono::DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap() + Duration::seconds(seconds)
    }

    fn sample(seconds: i64, cpu: Option<f64>, available: Option<f64>) -> MetricSample {
        MetricSample {
            observed_at: at(seconds),
            cpu_percent: cpu,
            memory_available_percent: available,
            memory_used_percent: available.map(|value| 100.0 - value),
        }
    }

    #[test]
    fn bounds_history_to_capacity() {
        let mut history = MetricHistory::new(3);
        for second in 0..5 {
            history.push(sample(second, Some(second as f64), Some(50.0)));
        }
        assert_eq!(history.len(), 3);
        assert_eq!(history.capacity(), 3);
        assert_eq!(history.samples().front().unwrap().observed_at, at(2));
        assert_eq!(history.samples().back().unwrap().observed_at, at(4));
    }

    #[test]
    fn ignores_missing_values_in_statistics() {
        let mut history = MetricHistory::new(10);
        history.push(sample(0, Some(10.0), None));
        history.push(sample(1, None, Some(20.0)));
        history.push(sample(2, Some(30.0), Some(40.0)));

        let cpu = history.cpu_stats().unwrap();
        assert_eq!(cpu.samples, 2);
        assert_eq!(cpu.average, 20.0);
        assert_eq!(cpu.minimum, 10.0);
        assert_eq!(cpu.maximum, 30.0);
        assert_eq!(cpu.trend, Trend::Rising);

        let memory = history.memory_available_stats().unwrap();
        assert_eq!(memory.samples, 2);
        assert_eq!(memory.average, 30.0);
    }

    #[test]
    fn reports_no_stats_when_every_sample_is_missing() {
        let mut history = MetricHistory::new(5);
        history.push(sample(0, None, None));
        assert!(history.cpu_stats().is_none());
        assert!(history.memory_available_stats().is_none());
        assert!(history.recent_cpu(10).is_empty());
    }

    #[test]
    fn recent_points_are_newest_last_and_capped() {
        let mut history = MetricHistory::new(10);
        for second in 0..5 {
            history.push(sample(second, Some(second as f64), None));
        }
        let recent = history.recent_cpu(2);
        assert_eq!(recent.len(), 2);
        assert_eq!(recent[0].value, 3.0);
        assert_eq!(recent[1].value, 4.0);
        assert_eq!(recent[1].observed_at, at(4));
    }

    #[test]
    fn computes_trend_direction() {
        let mut rising = MetricHistory::new(10);
        let mut falling = MetricHistory::new(10);
        for (index, value) in [10.0, 11.0, 12.0, 13.0].into_iter().enumerate() {
            rising.push(sample(index as i64, Some(value), None));
        }
        for (index, value) in [20.0, 19.0, 18.0, 17.0].into_iter().enumerate() {
            falling.push(sample(index as i64, Some(value), None));
        }
        assert_eq!(rising.cpu_stats().unwrap().trend, Trend::Rising);
        assert_eq!(falling.cpu_stats().unwrap().trend, Trend::Falling);
    }
}
