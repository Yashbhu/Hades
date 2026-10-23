//! Mounted-filesystem capacity metrics.
//!
//! Only capacity information already reported by the operating system is
//! used. The collector never walks the directory tree. `used_bytes` is derived
//! as `total_bytes - available_bytes`, so on filesystems that reserve blocks
//! for privileged use it can be smaller than the space occupied by files.

use serde::{Deserialize, Serialize};
use sysinfo::Disk;

use super::utilization_percent;

/// Capacity information for one mounted filesystem.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiskMetrics {
    /// Mount point as reported by the operating system.
    pub mount_point: String,
    /// Filesystem type (for example `apfs`, `ext4`), when reported.
    pub file_system: Option<String>,
    /// Total capacity, in bytes.
    pub total_bytes: u64,
    /// Space currently available to unprivileged writes, in bytes.
    pub available_bytes: u64,
    /// Capacity not available for writes, in bytes.
    pub used_bytes: u64,
    /// `used_bytes / total_bytes` as a percentage.
    pub used_percent: f64,
}

impl DiskMetrics {
    /// Build disk metrics from raw capacity values.
    ///
    /// Returns `None` when the filesystem reports zero total capacity, which
    /// usually means its capacity could not be read.
    pub fn from_raw(
        mount_point: String,
        file_system: Option<String>,
        total_bytes: u64,
        available_bytes: u64,
    ) -> Option<Self> {
        let used_bytes = total_bytes.saturating_sub(available_bytes);
        Some(Self {
            mount_point,
            file_system,
            total_bytes,
            available_bytes,
            used_bytes,
            used_percent: utilization_percent(used_bytes, total_bytes)?,
        })
    }

    /// Read capacity metrics for a single mounted disk.
    pub fn from_disk(disk: &Disk) -> Option<Self> {
        let file_system = disk.file_system().to_string_lossy();
        let file_system = if file_system.is_empty() {
            None
        } else {
            Some(file_system.into_owned())
        };
        Self::from_raw(
            disk.mount_point().to_string_lossy().into_owned(),
            file_system,
            disk.total_space(),
            disk.available_space(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::DiskMetrics;

    #[test]
    fn derives_used_space_and_percentage() {
        let disk = DiskMetrics::from_raw("/".into(), Some("apfs".into()), 1_000, 250).unwrap();
        assert_eq!(disk.used_bytes, 750);
        assert_eq!(disk.available_bytes, 250);
        assert_eq!(disk.used_percent, 75.0);
    }

    #[test]
    fn zero_capacity_is_unavailable() {
        assert!(DiskMetrics::from_raw("/mnt/broken".into(), None, 0, 0).is_none());
    }

    #[test]
    fn available_above_total_does_not_underflow() {
        let disk = DiskMetrics::from_raw("/".into(), None, 100, 200).unwrap();
        assert_eq!(disk.used_bytes, 0);
        assert_eq!(disk.used_percent, 0.0);
    }
}
