//! Validated configuration for continuous monitoring and anomaly detection.
//!
//! All thresholds live here rather than inside detector code. Defaults are
//! chosen for a five-second cadence.

use std::fmt;
use std::time::Duration;

/// Default delay between monitoring cycles.
pub const DEFAULT_SAMPLING_INTERVAL: Duration = Duration::from_secs(5);
/// Lowest accepted monitoring interval. CPU deltas need at least a 200 ms
/// window, and a smaller cadence would collect almost continuously.
pub const MIN_SAMPLING_INTERVAL: Duration = Duration::from_millis(250);
/// Highest accepted monitoring interval.
pub const MAX_SAMPLING_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

/// Default number of rolling samples retained for evidence and statistics.
pub const DEFAULT_HISTORY_LIMIT: usize = 60;
/// Lowest accepted history limit.
pub const MIN_HISTORY_LIMIT: usize = 2;
/// Highest accepted history limit.
pub const MAX_HISTORY_LIMIT: usize = 10_000;

/// Default number of recovered incidents kept in memory.
pub const DEFAULT_MAX_RETAINED_INCIDENTS: usize = 100;
/// Highest accepted recovered-incident retention limit.
pub const MAX_RETAINED_INCIDENTS: usize = 10_000;

/// Number of recent samples included in incident evidence.
pub const EVIDENCE_SAMPLE_LIMIT: usize = 10;

/// CPU utilization (percent) that starts a sustained-CPU incident.
pub const DEFAULT_CPU_THRESHOLD_PERCENT: f64 = 90.0;
/// CPU utilization (percent) that recovers an active CPU incident.
pub const DEFAULT_CPU_RECOVERY_PERCENT: f64 = 85.0;
/// CPU utilization (percent) that marks a CPU incident critical.
pub const DEFAULT_CPU_CRITICAL_PERCENT: f64 = 95.0;
/// How long CPU must remain at or above the trigger threshold.
pub const DEFAULT_CPU_SUSTAINED: Duration = Duration::from_secs(30);

/// Available memory (percent) that starts a memory-pressure incident.
pub const DEFAULT_MEMORY_AVAILABLE_THRESHOLD_PERCENT: f64 = 10.0;
/// Available memory (percent) that recovers an active memory incident.
pub const DEFAULT_MEMORY_RECOVERY_PERCENT: f64 = 15.0;
/// Available memory (percent) that marks a memory incident critical.
pub const DEFAULT_MEMORY_CRITICAL_PERCENT: f64 = 5.0;
/// How long available memory must remain at or below the trigger threshold.
pub const DEFAULT_MEMORY_SUSTAINED: Duration = Duration::from_secs(30);

/// Available disk space (percent) that starts a low-disk incident.
pub const DEFAULT_DISK_AVAILABLE_THRESHOLD_PERCENT: f64 = 10.0;
/// Available disk space (percent) that recovers a low-disk incident.
pub const DEFAULT_DISK_RECOVERY_PERCENT: f64 = 15.0;
/// Available disk space (percent) that marks a low-disk incident critical.
pub const DEFAULT_DISK_CRITICAL_PERCENT: f64 = 3.0;

/// Complete configuration for [`Monitor`](crate::monitor::Monitor).
#[derive(Debug, Clone, PartialEq)]
pub struct MonitorConfig {
    /// Delay between the starts of consecutive monitoring cycles.
    pub sampling_interval: Duration,
    /// Number of samples retained in the rolling history.
    pub history_limit: usize,
    /// Maximum number of recovered incidents kept in memory.
    pub max_retained_incidents: usize,
    /// CPU detection thresholds and duration.
    pub cpu: CpuDetectionConfig,
    /// Memory detection thresholds and duration.
    pub memory: MemoryDetectionConfig,
    /// Disk detection thresholds.
    pub disk: DiskDetectionConfig,
}

/// Sustained high CPU detection settings.
#[derive(Debug, Clone, PartialEq)]
pub struct CpuDetectionConfig {
    /// Start an incident when utilization is at or above this percent.
    pub threshold_percent: f64,
    /// Recover an incident when utilization is at or below this percent.
    pub recovery_threshold_percent: f64,
    /// Severity becomes `critical` at or above this percent.
    pub critical_threshold_percent: f64,
    /// Required time above the trigger threshold. Converted to consecutive
    /// valid samples using the sampling interval.
    pub sustained_for: Duration,
}

/// Memory pressure detection settings, based on **available** memory.
#[derive(Debug, Clone, PartialEq)]
pub struct MemoryDetectionConfig {
    /// Start an incident when available memory is at or below this percent.
    pub available_threshold_percent: f64,
    /// Recover an incident when available memory is at or above this percent.
    pub recovery_available_percent: f64,
    /// Severity becomes `critical` at or below this percent.
    pub critical_available_percent: f64,
    /// Required time at or below the trigger threshold.
    pub sustained_for: Duration,
}

/// Low disk space detection settings, based on **available** space.
#[derive(Debug, Clone, PartialEq)]
pub struct DiskDetectionConfig {
    /// Start an incident when available space is at or below this percent.
    pub available_threshold_percent: f64,
    /// Recover an incident when available space is at or above this percent.
    pub recovery_available_percent: f64,
    /// Severity becomes `critical` at or below this percent.
    pub critical_available_percent: f64,
}

impl Default for MonitorConfig {
    fn default() -> Self {
        Self {
            sampling_interval: DEFAULT_SAMPLING_INTERVAL,
            history_limit: DEFAULT_HISTORY_LIMIT,
            max_retained_incidents: DEFAULT_MAX_RETAINED_INCIDENTS,
            cpu: CpuDetectionConfig {
                threshold_percent: DEFAULT_CPU_THRESHOLD_PERCENT,
                recovery_threshold_percent: DEFAULT_CPU_RECOVERY_PERCENT,
                critical_threshold_percent: DEFAULT_CPU_CRITICAL_PERCENT,
                sustained_for: DEFAULT_CPU_SUSTAINED,
            },
            memory: MemoryDetectionConfig {
                available_threshold_percent: DEFAULT_MEMORY_AVAILABLE_THRESHOLD_PERCENT,
                recovery_available_percent: DEFAULT_MEMORY_RECOVERY_PERCENT,
                critical_available_percent: DEFAULT_MEMORY_CRITICAL_PERCENT,
                sustained_for: DEFAULT_MEMORY_SUSTAINED,
            },
            disk: DiskDetectionConfig {
                available_threshold_percent: DEFAULT_DISK_AVAILABLE_THRESHOLD_PERCENT,
                recovery_available_percent: DEFAULT_DISK_RECOVERY_PERCENT,
                critical_available_percent: DEFAULT_DISK_CRITICAL_PERCENT,
            },
        }
    }
}

impl MonitorConfig {
    /// Validate the whole configuration, including cross-field constraints.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if !(MIN_SAMPLING_INTERVAL..=MAX_SAMPLING_INTERVAL).contains(&self.sampling_interval) {
            return Err(ConfigError::new(format!(
                "sampling interval must be between {:?} and {:?}, got {:?}",
                MIN_SAMPLING_INTERVAL, MAX_SAMPLING_INTERVAL, self.sampling_interval
            )));
        }
        if !(MIN_HISTORY_LIMIT..=MAX_HISTORY_LIMIT).contains(&self.history_limit) {
            return Err(ConfigError::new(format!(
                "history limit must be between {MIN_HISTORY_LIMIT} and {MAX_HISTORY_LIMIT}, got {}",
                self.history_limit
            )));
        }
        if !(1..=MAX_RETAINED_INCIDENTS).contains(&self.max_retained_incidents) {
            return Err(ConfigError::new(format!(
                "retained incident limit must be between 1 and {MAX_RETAINED_INCIDENTS}, got {}",
                self.max_retained_incidents
            )));
        }

        let cpu = &self.cpu;
        validate_percent("CPU threshold", cpu.threshold_percent, true, true)?;
        validate_percent(
            "CPU recovery threshold",
            cpu.recovery_threshold_percent,
            false,
            true,
        )?;
        validate_percent(
            "CPU critical threshold",
            cpu.critical_threshold_percent,
            false,
            true,
        )?;
        if cpu.recovery_threshold_percent >= cpu.threshold_percent {
            return Err(ConfigError::new(format!(
                "CPU recovery threshold ({}) must be below the trigger threshold ({})",
                cpu.recovery_threshold_percent, cpu.threshold_percent
            )));
        }
        if cpu.critical_threshold_percent < cpu.threshold_percent {
            return Err(ConfigError::new(format!(
                "CPU critical threshold ({}) must be at or above the trigger threshold ({})",
                cpu.critical_threshold_percent, cpu.threshold_percent
            )));
        }
        validate_sustained("CPU", cpu.sustained_for, self.sampling_interval)?;

        let memory = &self.memory;
        validate_percent(
            "memory available threshold",
            memory.available_threshold_percent,
            true,
            false,
        )?;
        validate_percent(
            "memory recovery threshold",
            memory.recovery_available_percent,
            false,
            true,
        )?;
        validate_percent(
            "memory critical threshold",
            memory.critical_available_percent,
            false,
            true,
        )?;
        if memory.recovery_available_percent <= memory.available_threshold_percent {
            return Err(ConfigError::new(format!(
                "memory recovery threshold ({}) must be above the trigger threshold ({})",
                memory.recovery_available_percent, memory.available_threshold_percent
            )));
        }
        if memory.critical_available_percent > memory.available_threshold_percent {
            return Err(ConfigError::new(format!(
                "memory critical threshold ({}) must be at or below the trigger threshold ({})",
                memory.critical_available_percent, memory.available_threshold_percent
            )));
        }
        validate_sustained("memory", memory.sustained_for, self.sampling_interval)?;

        let disk = &self.disk;
        validate_percent(
            "disk available threshold",
            disk.available_threshold_percent,
            true,
            false,
        )?;
        validate_percent(
            "disk recovery threshold",
            disk.recovery_available_percent,
            false,
            true,
        )?;
        validate_percent(
            "disk critical threshold",
            disk.critical_available_percent,
            false,
            true,
        )?;
        if disk.recovery_available_percent <= disk.available_threshold_percent {
            return Err(ConfigError::new(format!(
                "disk recovery threshold ({}) must be above the trigger threshold ({})",
                disk.recovery_available_percent, disk.available_threshold_percent
            )));
        }
        if disk.critical_available_percent > disk.available_threshold_percent {
            return Err(ConfigError::new(format!(
                "disk critical threshold ({}) must be at or below the trigger threshold ({})",
                disk.critical_available_percent, disk.available_threshold_percent
            )));
        }

        Ok(())
    }

    /// Consecutive valid CPU samples required to start an incident.
    pub fn cpu_required_samples(&self) -> usize {
        samples_for(self.cpu.sustained_for, self.sampling_interval)
    }

    /// Consecutive valid memory samples required to start an incident.
    pub fn memory_required_samples(&self) -> usize {
        samples_for(self.memory.sustained_for, self.sampling_interval)
    }
}

/// Number of whole or partial sampling intervals that fit in `duration`,
/// with a minimum of one. Configuration validation guarantees at least two.
pub fn samples_for(duration: Duration, interval: Duration) -> usize {
    let duration_ms = duration.as_millis();
    let interval_ms = interval.as_millis();
    if interval_ms == 0 {
        return 1;
    }
    let samples = duration_ms.div_ceil(interval_ms);
    usize::try_from(samples).unwrap_or(usize::MAX).max(1)
}

fn validate_percent(
    name: &str,
    value: f64,
    exclusive_lower: bool,
    allow_hundred: bool,
) -> Result<(), ConfigError> {
    if !value.is_finite() {
        return Err(ConfigError::new(format!(
            "{name} must be a finite number, got {value}"
        )));
    }
    let lower_ok = if exclusive_lower {
        value > 0.0
    } else {
        value >= 0.0
    };
    let upper_ok = if allow_hundred {
        value <= 100.0
    } else {
        value < 100.0
    };
    if !lower_ok || !upper_ok {
        let lower = if exclusive_lower {
            "0 (exclusive)"
        } else {
            "0"
        };
        let upper = if allow_hundred {
            "100"
        } else {
            "100 (exclusive)"
        };
        return Err(ConfigError::new(format!(
            "{name} must be between {lower} and {upper}, got {value}"
        )));
    }
    Ok(())
}

fn validate_sustained(
    label: &str,
    sustained_for: Duration,
    interval: Duration,
) -> Result<(), ConfigError> {
    let minimum = interval.saturating_mul(2);
    if sustained_for < minimum {
        return Err(ConfigError::new(format!(
            "{label} duration must be at least two sampling intervals ({minimum:?}), got {sustained_for:?}"
        )));
    }
    if sustained_for > MAX_SAMPLING_INTERVAL {
        return Err(ConfigError::new(format!(
            "{label} duration must not exceed {MAX_SAMPLING_INTERVAL:?}, got {sustained_for:?}"
        )));
    }
    Ok(())
}

/// Configuration validation error with a user-facing message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError {
    message: String,
}

impl ConfigError {
    pub(crate) fn new(message: String) -> Self {
        Self { message }
    }

    /// The complete validation message.
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::{
        DEFAULT_CPU_SUSTAINED, DEFAULT_HISTORY_LIMIT, DEFAULT_MEMORY_SUSTAINED,
        DEFAULT_SAMPLING_INTERVAL, MonitorConfig, samples_for,
    };
    use std::time::Duration;

    #[test]
    fn defaults_are_valid_and_use_six_samples() {
        let config = MonitorConfig::default();
        config.validate().unwrap();
        assert_eq!(config.sampling_interval, DEFAULT_SAMPLING_INTERVAL);
        assert_eq!(config.history_limit, DEFAULT_HISTORY_LIMIT);
        assert_eq!(config.cpu.sustained_for, DEFAULT_CPU_SUSTAINED);
        assert_eq!(config.memory.sustained_for, DEFAULT_MEMORY_SUSTAINED);
        assert_eq!(config.cpu_required_samples(), 6);
        assert_eq!(config.memory_required_samples(), 6);
    }

    #[test]
    fn rounds_required_samples_up() {
        assert_eq!(
            samples_for(Duration::from_secs(30), Duration::from_secs(5)),
            6
        );
        assert_eq!(
            samples_for(Duration::from_secs(31), Duration::from_secs(5)),
            7
        );
        assert_eq!(
            samples_for(Duration::from_secs(2), Duration::from_secs(5)),
            1
        );
    }

    #[test]
    fn rejects_zero_or_extreme_sampling_interval() {
        let zero = MonitorConfig {
            sampling_interval: Duration::ZERO,
            ..MonitorConfig::default()
        };
        assert!(zero.validate().is_err());

        let oversized = MonitorConfig {
            sampling_interval: Duration::from_secs(48 * 60 * 60),
            ..MonitorConfig::default()
        };
        assert!(oversized.validate().is_err());
    }

    #[test]
    fn rejects_a_duration_shorter_than_two_samples() {
        let mut config = MonitorConfig::default();
        config.cpu.sustained_for = Duration::from_secs(5);
        let error = config.validate().unwrap_err();
        assert!(error.message().contains("at least two sampling intervals"));
    }

    #[test]
    fn rejects_cpu_recovery_at_or_above_trigger() {
        let mut config = MonitorConfig::default();
        config.cpu.recovery_threshold_percent = 95.0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_cpu_critical_below_trigger() {
        let mut config = MonitorConfig::default();
        config.cpu.critical_threshold_percent = 80.0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_memory_recovery_below_trigger() {
        let mut config = MonitorConfig::default();
        config.memory.recovery_available_percent = 8.0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_disk_critical_above_trigger() {
        let mut config = MonitorConfig::default();
        config.disk.critical_available_percent = 12.0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_non_finite_and_out_of_range_percentages() {
        let mut config = MonitorConfig::default();
        config.cpu.threshold_percent = f64::NAN;
        assert!(config.validate().is_err());

        config = MonitorConfig::default();
        config.memory.available_threshold_percent = 100.0;
        assert!(config.validate().is_err());

        config = MonitorConfig::default();
        config.disk.available_threshold_percent = -1.0;
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_unbounded_history_and_retention() {
        let no_history = MonitorConfig {
            history_limit: 0,
            ..MonitorConfig::default()
        };
        assert!(no_history.validate().is_err());

        let no_retention = MonitorConfig {
            max_retained_incidents: 0,
            ..MonitorConfig::default()
        };
        assert!(no_retention.validate().is_err());
    }
}
