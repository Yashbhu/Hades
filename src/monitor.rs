//! Continuous monitoring engine and deterministic anomaly detection.
//!
//! [`Monitor`] is the pure state machine: feed it snapshots with explicit
//! timestamps and it returns typed events. [`Monitor::start`] runs the same
//! state machine on a background thread with cancellation.

use std::io;
use std::sync::mpsc::{self, Receiver, RecvError, RecvTimeoutError, Sender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};

use crate::config::{ConfigError, EVIDENCE_SAMPLE_LIMIT, MonitorConfig};
use crate::events::MonitorEvent;
use crate::history::{MetricHistory, MetricSample, Trend};
use crate::incident::{
    CpuDerived, CpuObserved, DiskDerived, DiskObserved, Incident, IncidentEvidence, IncidentKind,
    IncidentState, MemoryDerived, MemoryObserved, Severity,
};
use crate::metrics::disk::DiskMetrics;
use crate::metrics::memory::MemoryMetrics;
use crate::metrics::utilization_percent;
use crate::snapshot::Snapshot;

/// Deterministic in-memory monitor.
///
/// The monitor owns bounded history, detection streaks, and incident state.
/// It performs no I/O. Call [`Monitor::observe`] directly for deterministic
/// tests or [`Monitor::start`] for continuous collection.
#[derive(Debug)]
pub struct Monitor {
    config: MonitorConfig,
    cpu_required_samples: usize,
    memory_required_samples: usize,
    history: MetricHistory,
    cpu_streak: usize,
    cpu_streak_started_at: Option<DateTime<Utc>>,
    memory_streak: usize,
    memory_streak_started_at: Option<DateTime<Utc>>,
    incidents: Vec<Incident>,
    next_incident_number: u64,
    last_collection_issues: Vec<(String, String)>,
}

impl Monitor {
    /// Create a monitor after validating the configuration.
    pub fn new(config: MonitorConfig) -> Result<Self, ConfigError> {
        config.validate()?;
        let cpu_required_samples = config.cpu_required_samples();
        let memory_required_samples = config.memory_required_samples();
        let history = MetricHistory::new(config.history_limit);
        Ok(Self {
            config,
            cpu_required_samples,
            memory_required_samples,
            history,
            cpu_streak: 0,
            cpu_streak_started_at: None,
            memory_streak: 0,
            memory_streak_started_at: None,
            incidents: Vec::new(),
            next_incident_number: 1,
            last_collection_issues: Vec::new(),
        })
    }

    /// Validated configuration in use.
    pub fn config(&self) -> &MonitorConfig {
        &self.config
    }

    /// Bounded metric history.
    pub fn history(&self) -> &MetricHistory {
        &self.history
    }

    /// Retained incidents: all open incidents plus the most recent recovered
    /// incidents up to `max_retained_incidents`.
    pub fn incidents(&self) -> &[Incident] {
        &self.incidents
    }

    /// Consecutive CPU samples required by this configuration.
    pub fn cpu_required_samples(&self) -> usize {
        self.cpu_required_samples
    }

    /// Consecutive memory samples required by this configuration.
    pub fn memory_required_samples(&self) -> usize {
        self.memory_required_samples
    }

    /// Acknowledge an incident at the current time.
    pub fn acknowledge(&mut self, incident_id: &str) -> bool {
        self.acknowledge_at(incident_id, Utc::now())
    }

    /// Acknowledge an incident at an explicit time. Only active incidents can
    /// be acknowledged; acknowledgement never implies recovery.
    pub fn acknowledge_at(&mut self, incident_id: &str, at: DateTime<Utc>) -> bool {
        self.incidents
            .iter_mut()
            .find(|incident| incident.id == incident_id)
            .is_some_and(|incident| incident.acknowledge_at(at))
    }

    /// Process one snapshot and return the events it produced.
    ///
    /// Detection never invents values for missing metrics. An open incident is
    /// left open when its metric disappears, because recovery cannot be
    /// established without an observation.
    pub fn observe(
        &mut self,
        snapshot: &Snapshot,
        observed_at: DateTime<Utc>,
    ) -> Vec<MonitorEvent> {
        let mut events = Vec::new();

        let issues: Vec<(String, String)> = snapshot
            .issues
            .iter()
            .map(|issue| (issue.component.clone(), issue.message.clone()))
            .collect();
        if !issues.is_empty() && issues != self.last_collection_issues {
            events.push(MonitorEvent::CollectionError {
                timestamp: observed_at,
                issues: snapshot.issues.clone(),
            });
        }
        self.last_collection_issues = issues;

        let cpu = snapshot
            .cpu
            .as_ref()
            .and_then(|cpu| valid_percent(cpu.overall_percent));
        let memory = snapshot.memory.as_ref();
        let memory_available = memory
            .and_then(|memory| utilization_percent(memory.available_bytes, memory.total_bytes));
        let memory_used =
            memory.and_then(|memory| utilization_percent(memory.used_bytes, memory.total_bytes));

        self.history.push(MetricSample {
            observed_at,
            cpu_percent: cpu,
            memory_available_percent: memory_available,
            memory_used_percent: memory_used,
        });

        self.detect_cpu(cpu, observed_at, &mut events);
        self.detect_memory(memory, memory_available, observed_at, &mut events);
        self.detect_disks(&snapshot.disks, observed_at, &mut events);

        events
    }

    fn detect_cpu(&mut self, cpu: Option<f64>, at: DateTime<Utc>, events: &mut Vec<MonitorEvent>) {
        let Some(cpu) = cpu else {
            self.cpu_streak = 0;
            self.cpu_streak_started_at = None;
            return;
        };
        let threshold = self.config.cpu.threshold_percent;
        let recovery = self.config.cpu.recovery_threshold_percent;
        let critical = self.config.cpu.critical_threshold_percent;

        if cpu >= threshold {
            if self.cpu_streak == 0 {
                self.cpu_streak_started_at = Some(at);
            }
            self.cpu_streak = self.cpu_streak.saturating_add(1);
            let severity = if cpu >= critical {
                Severity::Critical
            } else {
                Severity::Warning
            };

            if let Some(index) = self.find_open(IncidentKind::HighCpu, None) {
                let started_at = self.incidents[index].started_at;
                let evidence = self.cpu_evidence(cpu, self.cpu_streak, at, started_at);
                let summary = cpu_summary(cpu, threshold, recovery);
                self.update_incident(index, severity, summary, evidence, at, events);
            } else if self.cpu_streak >= self.cpu_required_samples {
                let started_at = self.cpu_streak_started_at.unwrap_or(at);
                let evidence = self.cpu_evidence(cpu, self.cpu_streak, at, started_at);
                let summary = cpu_summary(cpu, threshold, recovery);
                self.start_incident(
                    IncidentKind::HighCpu,
                    None,
                    severity,
                    started_at,
                    at,
                    summary,
                    evidence,
                    events,
                );
            }
            return;
        }

        self.cpu_streak = 0;
        self.cpu_streak_started_at = None;
        let Some(index) = self.find_open(IncidentKind::HighCpu, None) else {
            return;
        };
        let started_at = self.incidents[index].started_at;
        let evidence = self.cpu_evidence(cpu, 0, at, started_at);
        let severity = if cpu >= critical {
            Severity::Critical
        } else {
            Severity::Warning
        };
        let summary = cpu_summary(cpu, threshold, recovery);
        if cpu <= recovery {
            self.recover_incident(index, severity, summary, evidence, at, events);
        } else {
            self.update_incident(index, severity, summary, evidence, at, events);
        }
    }

    fn detect_memory(
        &mut self,
        memory: Option<&MemoryMetrics>,
        available: Option<f64>,
        at: DateTime<Utc>,
        events: &mut Vec<MonitorEvent>,
    ) {
        let (Some(memory), Some(available)) = (memory, available) else {
            self.memory_streak = 0;
            self.memory_streak_started_at = None;
            return;
        };
        let threshold = self.config.memory.available_threshold_percent;
        let recovery = self.config.memory.recovery_available_percent;
        let critical = self.config.memory.critical_available_percent;

        if available <= threshold {
            if self.memory_streak == 0 {
                self.memory_streak_started_at = Some(at);
            }
            self.memory_streak = self.memory_streak.saturating_add(1);
            let severity = if available <= critical {
                Severity::Critical
            } else {
                Severity::Warning
            };

            if let Some(index) = self.find_open(IncidentKind::MemoryPressure, None) {
                let started_at = self.incidents[index].started_at;
                let evidence =
                    self.memory_evidence(memory, available, self.memory_streak, at, started_at);
                let summary = memory_summary(available, threshold, recovery);
                self.update_incident(index, severity, summary, evidence, at, events);
            } else if self.memory_streak >= self.memory_required_samples {
                let started_at = self.memory_streak_started_at.unwrap_or(at);
                let evidence =
                    self.memory_evidence(memory, available, self.memory_streak, at, started_at);
                let summary = memory_summary(available, threshold, recovery);
                self.start_incident(
                    IncidentKind::MemoryPressure,
                    None,
                    severity,
                    started_at,
                    at,
                    summary,
                    evidence,
                    events,
                );
            }
            return;
        }

        self.memory_streak = 0;
        self.memory_streak_started_at = None;
        let Some(index) = self.find_open(IncidentKind::MemoryPressure, None) else {
            return;
        };
        let started_at = self.incidents[index].started_at;
        let evidence = self.memory_evidence(memory, available, 0, at, started_at);
        let severity = if available <= critical {
            Severity::Critical
        } else {
            Severity::Warning
        };
        let summary = memory_summary(available, threshold, recovery);
        if available >= recovery {
            self.recover_incident(index, severity, summary, evidence, at, events);
        } else {
            self.update_incident(index, severity, summary, evidence, at, events);
        }
    }

    fn detect_disks(
        &mut self,
        disks: &[DiskMetrics],
        at: DateTime<Utc>,
        events: &mut Vec<MonitorEvent>,
    ) {
        let threshold = self.config.disk.available_threshold_percent;
        let recovery = self.config.disk.recovery_available_percent;
        let critical = self.config.disk.critical_available_percent;

        for disk in disks {
            let Some(available) = utilization_percent(disk.available_bytes, disk.total_bytes)
            else {
                // Zero-capacity, inaccessible, or malformed filesystems are
                // ignored rather than treated as 0% available.
                continue;
            };
            let subject = disk.mount_point.clone();
            let open = self.find_open(IncidentKind::LowDiskSpace, Some(&subject));
            let severity = if available <= critical {
                Severity::Critical
            } else {
                Severity::Warning
            };

            if available <= threshold {
                let evidence = disk_evidence(disk, available, &self.config.disk);
                let summary = disk_summary(&disk.mount_point, available, threshold, recovery);
                if let Some(index) = open {
                    self.update_incident(index, severity, summary, evidence, at, events);
                } else {
                    self.start_incident(
                        IncidentKind::LowDiskSpace,
                        Some(subject),
                        severity,
                        at,
                        at,
                        summary,
                        evidence,
                        events,
                    );
                }
            } else if let Some(index) = open {
                let evidence = disk_evidence(disk, available, &self.config.disk);
                let summary = disk_summary(&disk.mount_point, available, threshold, recovery);
                if available >= recovery {
                    self.recover_incident(index, severity, summary, evidence, at, events);
                } else {
                    self.update_incident(index, severity, summary, evidence, at, events);
                }
            }
        }
    }

    fn cpu_evidence(
        &self,
        cpu: f64,
        consecutive_samples: usize,
        at: DateTime<Utc>,
        started_at: DateTime<Utc>,
    ) -> IncidentEvidence {
        let stats = self.history.cpu_stats();
        IncidentEvidence::HighCpu {
            observed: CpuObserved {
                latest_percent: cpu,
                recent_samples: self.history.recent_cpu(EVIDENCE_SAMPLE_LIMIT),
            },
            derived: CpuDerived {
                threshold_percent: self.config.cpu.threshold_percent,
                recovery_threshold_percent: self.config.cpu.recovery_threshold_percent,
                critical_threshold_percent: self.config.cpu.critical_threshold_percent,
                consecutive_samples,
                condition_duration_seconds: seconds_between(started_at, at),
                recent_average_percent: stats.as_ref().map_or(cpu, |stats| stats.average),
                trend: stats.map_or(Trend::Stable, |stats| stats.trend),
            },
        }
    }

    fn memory_evidence(
        &self,
        memory: &MemoryMetrics,
        available: f64,
        consecutive_samples: usize,
        at: DateTime<Utc>,
        started_at: DateTime<Utc>,
    ) -> IncidentEvidence {
        let stats = self.history.memory_available_stats();
        IncidentEvidence::MemoryPressure {
            observed: MemoryObserved {
                total_bytes: memory.total_bytes,
                used_bytes: memory.used_bytes,
                available_bytes: memory.available_bytes,
                free_bytes: memory.free_bytes,
                latest_used_percent: utilization_percent(memory.used_bytes, memory.total_bytes)
                    .unwrap_or(0.0),
                latest_available_percent: available,
                recent_available_percent: self
                    .history
                    .recent_memory_available(EVIDENCE_SAMPLE_LIMIT),
            },
            derived: MemoryDerived {
                available_threshold_percent: self.config.memory.available_threshold_percent,
                recovery_available_percent: self.config.memory.recovery_available_percent,
                critical_available_percent: self.config.memory.critical_available_percent,
                consecutive_samples,
                condition_duration_seconds: seconds_between(started_at, at),
                recent_average_available_percent: stats
                    .as_ref()
                    .map_or(available, |stats| stats.average),
                trend: stats.map_or(Trend::Stable, |stats| stats.trend),
            },
        }
    }

    fn find_open(&self, kind: IncidentKind, subject: Option<&str>) -> Option<usize> {
        self.incidents.iter().position(|incident| {
            incident.kind == kind && incident.subject.as_deref() == subject && incident.is_open()
        })
    }

    #[allow(clippy::too_many_arguments)]
    fn start_incident(
        &mut self,
        kind: IncidentKind,
        subject: Option<String>,
        severity: Severity,
        started_at: DateTime<Utc>,
        observed_at: DateTime<Utc>,
        summary: String,
        evidence: IncidentEvidence,
        events: &mut Vec<MonitorEvent>,
    ) {
        let incident = Incident {
            id: format!("inc-{:06}", self.next_incident_number),
            kind,
            subject,
            severity,
            state: IncidentState::Active,
            started_at,
            updated_at: observed_at,
            last_observed_at: observed_at,
            recovered_at: None,
            summary,
            evidence,
        };
        self.next_incident_number = self.next_incident_number.saturating_add(1);
        self.incidents.push(incident.clone());
        events.push(MonitorEvent::IncidentStarted {
            timestamp: observed_at,
            incident,
        });
    }

    fn update_incident(
        &mut self,
        index: usize,
        severity: Severity,
        summary: String,
        evidence: IncidentEvidence,
        at: DateTime<Utc>,
        events: &mut Vec<MonitorEvent>,
    ) {
        let incident = &mut self.incidents[index];
        incident.severity = severity;
        incident.updated_at = at;
        incident.last_observed_at = at;
        incident.summary = summary;
        incident.evidence = evidence;
        events.push(MonitorEvent::IncidentUpdated {
            timestamp: at,
            incident: incident.clone(),
        });
    }

    fn recover_incident(
        &mut self,
        index: usize,
        severity: Severity,
        summary: String,
        evidence: IncidentEvidence,
        at: DateTime<Utc>,
        events: &mut Vec<MonitorEvent>,
    ) {
        let incident = &mut self.incidents[index];
        incident.severity = severity;
        incident.last_observed_at = at;
        incident.summary = summary;
        incident.evidence = evidence;
        incident.recover_at(at);
        events.push(MonitorEvent::IncidentRecovered {
            timestamp: at,
            incident: incident.clone(),
        });
        self.prune_recovered_incidents();
    }

    fn prune_recovered_incidents(&mut self) {
        while self
            .incidents
            .iter()
            .filter(|incident| incident.state == IncidentState::Recovered)
            .count()
            > self.config.max_retained_incidents
        {
            let Some(index) = self
                .incidents
                .iter()
                .position(|incident| incident.state == IncidentState::Recovered)
            else {
                break;
            };
            self.incidents.remove(index);
        }
    }

    /// Start a background monitoring thread.
    ///
    /// The closure is invoked once per cycle and should return a snapshot. The
    /// first cycle starts immediately; later cycles are scheduled at
    /// `sampling_interval`. Collection never overlaps because it runs on one
    /// thread. Dropping or stopping the returned handle signals prompt
    /// cancellation and joins the thread.
    pub fn start<F>(self, mut collect: F) -> io::Result<MonitorHandle>
    where
        F: FnMut() -> Snapshot + Send + 'static,
    {
        let interval = self.config.sampling_interval;
        let (event_tx, event_rx) = mpsc::channel();
        let (stop_tx, stop_rx) = mpsc::channel::<()>();

        let thread = thread::Builder::new()
            .name("os-agent-monitor".into())
            .spawn(move || {
                let mut monitor = self;
                let mut next_tick = Instant::now();
                loop {
                    // Wait for the next tick in short slices so cancellation is
                    // prompt even when the sampling interval is large.
                    loop {
                        let now = Instant::now();
                        if now >= next_tick {
                            break;
                        }
                        let wait = (next_tick - now).min(Duration::from_millis(50));
                        match stop_rx.recv_timeout(wait) {
                            Err(RecvTimeoutError::Timeout) => {}
                            Ok(()) | Err(RecvTimeoutError::Disconnected) => return,
                        }
                    }

                    let snapshot = collect();
                    let events = monitor.observe(&snapshot, Utc::now());
                    let mut receiver_gone = false;
                    for event in events {
                        if event_tx.send(event).is_err() {
                            receiver_gone = true;
                            break;
                        }
                    }
                    if receiver_gone {
                        break;
                    }

                    let now = Instant::now();
                    next_tick += interval;
                    if next_tick <= now {
                        // Never run catch-up cycles back-to-back.
                        next_tick = now + interval;
                    }
                }
            })?;

        Ok(MonitorHandle {
            events: event_rx,
            stop_tx: Some(stop_tx),
            thread: Some(thread),
        })
    }
}

/// Handle for a background [`Monitor`].
///
/// Events are delivered through a standard channel. Dropping the handle stops
/// and joins the monitoring thread.
#[derive(Debug)]
pub struct MonitorHandle {
    events: Receiver<MonitorEvent>,
    stop_tx: Option<Sender<()>>,
    thread: Option<JoinHandle<()>>,
}

impl MonitorHandle {
    /// Block until the next event arrives.
    pub fn recv(&self) -> Result<MonitorEvent, RecvError> {
        self.events.recv()
    }

    /// Return an event if one is already available.
    pub fn try_recv(&self) -> Result<MonitorEvent, TryRecvError> {
        self.events.try_recv()
    }

    /// Wait at most `timeout` for the next event.
    pub fn recv_timeout(
        &self,
        timeout: std::time::Duration,
    ) -> Result<MonitorEvent, RecvTimeoutError> {
        self.events.recv_timeout(timeout)
    }

    /// Signal cancellation and wait for the monitoring thread to finish.
    pub fn stop(mut self) -> thread::Result<()> {
        if let Some(stop_tx) = self.stop_tx.take() {
            let _ = stop_tx.send(());
        }
        match self.thread.take() {
            Some(thread) => thread.join(),
            None => Ok(()),
        }
    }
}

impl Drop for MonitorHandle {
    fn drop(&mut self) {
        if let Some(stop_tx) = self.stop_tx.take() {
            let _ = stop_tx.send(());
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn valid_percent(value: f64) -> Option<f64> {
    if value.is_finite() && (0.0..=100.0).contains(&value) {
        Some(value)
    } else {
        None
    }
}

fn seconds_between(started_at: DateTime<Utc>, at: DateTime<Utc>) -> u64 {
    at.signed_duration_since(started_at).num_seconds().max(0) as u64
}

fn cpu_summary(cpu: f64, threshold: f64, recovery: f64) -> String {
    if cpu >= threshold {
        format!("CPU utilization is {cpu:.1}%, at or above the {threshold:.1}% threshold")
    } else if cpu <= recovery {
        format!("CPU utilization is {cpu:.1}%, at or below the {recovery:.1}% recovery threshold")
    } else {
        format!(
            "CPU utilization is {cpu:.1}%, between the {recovery:.1}% recovery and {threshold:.1}% trigger thresholds"
        )
    }
}

fn memory_summary(available: f64, threshold: f64, recovery: f64) -> String {
    if available <= threshold {
        format!("Available memory is {available:.1}%, at or below the {threshold:.1}% threshold")
    } else if available >= recovery {
        format!(
            "Available memory is {available:.1}%, at or above the {recovery:.1}% recovery threshold"
        )
    } else {
        format!(
            "Available memory is {available:.1}%, between the {threshold:.1}% trigger and {recovery:.1}% recovery thresholds"
        )
    }
}

fn disk_summary(mount_point: &str, available: f64, threshold: f64, recovery: f64) -> String {
    if available <= threshold {
        format!(
            "Filesystem '{mount_point}' has {available:.1}% available, at or below the {threshold:.1}% threshold"
        )
    } else if available >= recovery {
        format!(
            "Filesystem '{mount_point}' has {available:.1}% available, at or above the {recovery:.1}% recovery threshold"
        )
    } else {
        format!(
            "Filesystem '{mount_point}' has {available:.1}% available, between the {threshold:.1}% trigger and {recovery:.1}% recovery thresholds"
        )
    }
}

fn disk_evidence(
    disk: &DiskMetrics,
    available: f64,
    config: &crate::config::DiskDetectionConfig,
) -> IncidentEvidence {
    IncidentEvidence::LowDiskSpace {
        observed: DiskObserved {
            mount_point: disk.mount_point.clone(),
            file_system: disk.file_system.clone(),
            total_bytes: disk.total_bytes,
            available_bytes: disk.available_bytes,
            used_bytes: disk.used_bytes,
            available_percent: available,
        },
        derived: DiskDerived {
            available_threshold_percent: config.available_threshold_percent,
            recovery_available_percent: config.recovery_available_percent,
            critical_available_percent: config.critical_available_percent,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::Monitor;
    use crate::config::MonitorConfig;
    use crate::events::MonitorEvent;
    use crate::incident::{IncidentKind, IncidentState, Severity};
    use crate::metrics::disk::DiskMetrics;
    use crate::metrics::memory::MemoryMetrics;
    use crate::metrics::processes::TopProcesses;
    use crate::snapshot::{CollectionIssue, SCHEMA_VERSION, Snapshot};
    use chrono::{Duration, TimeZone, Utc};

    fn at(step: i64) -> chrono::DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap() + Duration::seconds(step * 5)
    }

    fn monitor() -> Monitor {
        Monitor::new(MonitorConfig::default()).unwrap()
    }

    fn snapshot(
        cpu: Option<f64>,
        memory: Option<MemoryMetrics>,
        disks: Vec<DiskMetrics>,
        issues: Vec<CollectionIssue>,
    ) -> Snapshot {
        Snapshot {
            schema_version: SCHEMA_VERSION,
            collected_at: "2026-01-01T00:00:00.000Z".into(),
            cpu: cpu
                .map(|percent| crate::metrics::cpu::CpuMetrics::new(percent, vec![], 200).unwrap()),
            memory,
            disks,
            processes: TopProcesses::from_processes(vec![], crate::metrics::ProcessSort::Cpu, 0),
            issues,
        }
    }

    fn cpu_snapshot(percent: Option<f64>) -> Snapshot {
        snapshot(percent, None, vec![], vec![])
    }

    fn memory_snapshot(available_percent: f64) -> Snapshot {
        let total = 1_000;
        let available = (total as f64 * available_percent / 100.0) as u64;
        let used = 700;
        let memory = MemoryMetrics::from_raw(total, used, available, 10).unwrap();
        snapshot(None, Some(memory), vec![], vec![])
    }

    fn disk_snapshot(mount: &str, available_percent: f64) -> Snapshot {
        let total = 1_000;
        let available = (total as f64 * available_percent / 100.0) as u64;
        let disk =
            DiskMetrics::from_raw(mount.into(), Some("ext4".into()), total, available).unwrap();
        snapshot(None, None, vec![disk], vec![])
    }

    fn start_cpu_incident(monitor: &mut Monitor, percent: f64) -> Vec<MonitorEvent> {
        let mut events = Vec::new();
        for step in 0..6 {
            events = monitor.observe(&cpu_snapshot(Some(percent)), at(step));
        }
        events
    }

    #[test]
    fn sustained_cpu_starts_after_required_samples() {
        let mut monitor = monitor();
        for step in 0..5 {
            let events = monitor.observe(&cpu_snapshot(Some(92.0)), at(step));
            assert!(events.is_empty());
            assert!(monitor.incidents().is_empty());
        }
        let events = monitor.observe(&cpu_snapshot(Some(92.0)), at(5));
        assert_eq!(events.len(), 1);
        let MonitorEvent::IncidentStarted { incident, .. } = &events[0] else {
            panic!("expected incident_started");
        };
        assert_eq!(incident.kind, IncidentKind::HighCpu);
        assert_eq!(incident.state, IncidentState::Active);
        assert_eq!(incident.severity, Severity::Warning);
        assert_eq!(incident.started_at, at(0));
        assert_eq!(incident.last_observed_at, at(5));
        assert_eq!(monitor.incidents().len(), 1);
        let crate::incident::IncidentEvidence::HighCpu { observed, derived } = &incident.evidence
        else {
            panic!("expected CPU evidence");
        };
        assert_eq!(observed.latest_percent, 92.0);
        assert_eq!(derived.consecutive_samples, 6);
        assert_eq!(derived.condition_duration_seconds, 25);
        assert_eq!(observed.recent_samples.len(), 6);
    }

    #[test]
    fn transient_spike_does_not_start_an_incident() {
        let mut monitor = monitor();
        for step in 0..5 {
            monitor.observe(&cpu_snapshot(Some(99.0)), at(step));
        }
        monitor.observe(&cpu_snapshot(Some(10.0)), at(5));
        for step in 6..11 {
            let events = monitor.observe(&cpu_snapshot(Some(99.0)), at(step));
            assert!(events.is_empty());
        }
        assert!(monitor.incidents().is_empty());
        let events = monitor.observe(&cpu_snapshot(Some(99.0)), at(11));
        assert!(matches!(events[0], MonitorEvent::IncidentStarted { .. }));
    }

    #[test]
    fn missing_cpu_sample_resets_the_streak_without_becoming_zero() {
        let mut monitor = monitor();
        for step in 0..5 {
            monitor.observe(&cpu_snapshot(Some(99.0)), at(step));
        }
        monitor.observe(&cpu_snapshot(None), at(5));
        for step in 6..11 {
            let events = monitor.observe(&cpu_snapshot(Some(99.0)), at(step));
            assert!(events.is_empty());
        }
        assert!(monitor.incidents().is_empty());
        assert_eq!(
            monitor.history().samples().back().unwrap().cpu_percent,
            Some(99.0)
        );
    }

    #[test]
    fn invalid_cpu_reading_is_treated_as_missing() {
        let mut monitor = monitor();
        let mut invalid = cpu_snapshot(None);
        invalid.cpu = Some(crate::metrics::cpu::CpuMetrics {
            overall_percent: 150.0,
            per_core_percent: vec![],
            logical_core_count: 1,
            sampling_interval_ms: 200,
        });
        for step in 0..6 {
            let events = monitor.observe(&invalid, at(step));
            assert!(events.is_empty());
        }
        assert!(monitor.incidents().is_empty());
        assert_eq!(
            monitor.history().samples().back().unwrap().cpu_percent,
            None
        );
    }

    #[test]
    fn repeated_observations_update_one_incident() {
        let mut monitor = monitor();
        start_cpu_incident(&mut monitor, 92.0);
        let first_id = monitor.incidents()[0].id.clone();
        let events = monitor.observe(&cpu_snapshot(Some(93.0)), at(6));
        assert_eq!(events.len(), 1);
        let MonitorEvent::IncidentUpdated { incident, .. } = &events[0] else {
            panic!("expected incident_updated");
        };
        assert_eq!(incident.id, first_id);
        assert_eq!(monitor.incidents().len(), 1);
        assert_eq!(incident.last_observed_at, at(6));
    }

    #[test]
    fn hysteresis_keeps_incident_open_until_recovery_threshold() {
        let mut monitor = monitor();
        start_cpu_incident(&mut monitor, 92.0);
        let id = monitor.incidents()[0].id.clone();

        let events = monitor.observe(&cpu_snapshot(Some(87.0)), at(6));
        let MonitorEvent::IncidentUpdated { incident, .. } = &events[0] else {
            panic!("expected incident_updated");
        };
        assert_eq!(incident.id, id);
        assert_eq!(incident.state, IncidentState::Active);

        let events = monitor.observe(&cpu_snapshot(Some(85.0)), at(7));
        let MonitorEvent::IncidentRecovered { incident, .. } = &events[0] else {
            panic!("expected incident_recovered");
        };
        assert_eq!(incident.id, id);
        assert_eq!(incident.state, IncidentState::Recovered);
        assert_eq!(incident.recovered_at, Some(at(7)));
    }

    #[test]
    fn recovery_then_new_condition_creates_a_new_identifier() {
        let mut monitor = monitor();
        start_cpu_incident(&mut monitor, 95.0);
        let first_id = monitor.incidents()[0].id.clone();
        monitor.observe(&cpu_snapshot(Some(10.0)), at(6));

        for step in 7..12 {
            monitor.observe(&cpu_snapshot(Some(95.0)), at(step));
        }
        let events = monitor.observe(&cpu_snapshot(Some(95.0)), at(12));
        let MonitorEvent::IncidentStarted { incident, .. } = &events[0] else {
            panic!("expected incident_started");
        };
        assert_ne!(incident.id, first_id);
        assert_eq!(monitor.incidents().len(), 2);
        assert_eq!(monitor.incidents()[0].state, IncidentState::Recovered);
    }

    #[test]
    fn severity_escalates_on_critical_threshold() {
        let mut monitor = monitor();
        start_cpu_incident(&mut monitor, 92.0);
        assert_eq!(monitor.incidents()[0].severity, Severity::Warning);
        let events = monitor.observe(&cpu_snapshot(Some(96.0)), at(6));
        let MonitorEvent::IncidentUpdated { incident, .. } = &events[0] else {
            panic!("expected incident_updated");
        };
        assert_eq!(incident.severity, Severity::Critical);
    }

    #[test]
    fn memory_pressure_uses_available_not_used_percentage() {
        let mut monitor = monitor();
        // 70% used but only 8% available: must trigger on available memory.
        for step in 0..5 {
            monitor.observe(&memory_snapshot(8.0), at(step));
        }
        let events = monitor.observe(&memory_snapshot(8.0), at(5));
        let MonitorEvent::IncidentStarted { incident, .. } = &events[0] else {
            panic!("expected incident_started");
        };
        assert_eq!(incident.kind, IncidentKind::MemoryPressure);
        let crate::incident::IncidentEvidence::MemoryPressure { observed, .. } = &incident.evidence
        else {
            panic!("expected memory evidence");
        };
        assert_eq!(observed.latest_used_percent, 70.0);
        assert_eq!(observed.latest_available_percent, 8.0);
    }

    #[test]
    fn memory_recovers_above_the_recovery_threshold() {
        let mut monitor = monitor();
        for step in 0..6 {
            monitor.observe(&memory_snapshot(8.0), at(step));
        }
        assert_eq!(monitor.incidents().len(), 1);
        let events = monitor.observe(&memory_snapshot(20.0), at(6));
        let MonitorEvent::IncidentRecovered { incident, .. } = &events[0] else {
            panic!("expected incident_recovered");
        };
        assert_eq!(incident.state, IncidentState::Recovered);
    }

    #[test]
    fn missing_memory_does_not_recover_or_trigger() {
        let mut monitor = monitor();
        for step in 0..6 {
            monitor.observe(&memory_snapshot(8.0), at(step));
        }
        monitor.observe(&snapshot(None, None, vec![], vec![]), at(6));
        assert_eq!(monitor.incidents()[0].state, IncidentState::Active);
        assert_eq!(monitor.incidents()[0].last_observed_at, at(5));
    }

    #[test]
    fn low_disk_space_is_tracked_per_mount_and_not_duplicated() {
        let mut monitor = monitor();
        let events = monitor.observe(&disk_snapshot("/", 5.0), at(0));
        let MonitorEvent::IncidentStarted { incident, .. } = &events[0] else {
            panic!("expected incident_started");
        };
        assert_eq!(incident.kind, IncidentKind::LowDiskSpace);
        assert_eq!(incident.subject.as_deref(), Some("/"));
        let id = incident.id.clone();

        let events = monitor.observe(&disk_snapshot("/", 4.0), at(1));
        let MonitorEvent::IncidentUpdated { incident, .. } = &events[0] else {
            panic!("expected incident_updated");
        };
        assert_eq!(incident.id, id);
        assert_eq!(monitor.incidents().len(), 1);

        let events = monitor.observe(&disk_snapshot("/var", 2.0), at(2));
        assert!(matches!(
            events.as_slice(),
            [MonitorEvent::IncidentStarted { .. }]
        ));
        assert_eq!(monitor.incidents().len(), 2);
    }

    #[test]
    fn low_disk_recovers_and_can_start_a_new_incident() {
        let mut monitor = monitor();
        let events = monitor.observe(&disk_snapshot("/", 5.0), at(0));
        let first_id = match &events[0] {
            MonitorEvent::IncidentStarted { incident, .. } => incident.id.clone(),
            other => panic!("expected incident_started, got {other:?}"),
        };
        let events = monitor.observe(&disk_snapshot("/", 30.0), at(1));
        assert!(matches!(events[0], MonitorEvent::IncidentRecovered { .. }));
        let events = monitor.observe(&disk_snapshot("/", 5.0), at(2));
        let second_id = match &events[0] {
            MonitorEvent::IncidentStarted { incident, .. } => incident.id.clone(),
            other => panic!("expected incident_started, got {other:?}"),
        };
        assert_ne!(first_id, second_id);
    }

    #[test]
    fn zero_capacity_disk_is_ignored() {
        let mut monitor = monitor();
        let disk = DiskMetrics {
            mount_point: "/broken".into(),
            file_system: None,
            total_bytes: 0,
            available_bytes: 0,
            used_bytes: 0,
            used_percent: 0.0,
        };
        let events = monitor.observe(&snapshot(None, None, vec![disk], vec![]), at(0));
        assert!(events.is_empty());
        assert!(monitor.incidents().is_empty());
    }

    #[test]
    fn missing_disk_leaves_incident_open() {
        let mut monitor = monitor();
        monitor.observe(&disk_snapshot("/", 5.0), at(0));
        let events = monitor.observe(&snapshot(None, None, vec![], vec![]), at(1));
        assert!(events.is_empty());
        assert_eq!(monitor.incidents()[0].state, IncidentState::Active);
        assert_eq!(monitor.incidents()[0].last_observed_at, at(0));
    }

    #[test]
    fn acknowledgement_does_not_stop_updates_or_recovery() {
        let mut monitor = monitor();
        start_cpu_incident(&mut monitor, 92.0);
        let id = monitor.incidents()[0].id.clone();
        assert!(monitor.acknowledge_at(&id, at(6)));
        assert_eq!(monitor.incidents()[0].state, IncidentState::Acknowledged);
        assert!(!monitor.acknowledge_at(&id, at(6)));

        let events = monitor.observe(&cpu_snapshot(Some(93.0)), at(7));
        let MonitorEvent::IncidentUpdated { incident, .. } = &events[0] else {
            panic!("expected incident_updated");
        };
        assert_eq!(incident.state, IncidentState::Acknowledged);

        let events = monitor.observe(&cpu_snapshot(Some(10.0)), at(8));
        let MonitorEvent::IncidentRecovered { incident, .. } = &events[0] else {
            panic!("expected incident_recovered");
        };
        assert_eq!(incident.state, IncidentState::Recovered);
        assert!(!monitor.acknowledge_at(&id, at(9)));
    }

    #[test]
    fn history_is_bounded() {
        let config = MonitorConfig {
            history_limit: 3,
            ..MonitorConfig::default()
        };
        let mut monitor = Monitor::new(config).unwrap();
        for step in 0..5 {
            monitor.observe(&cpu_snapshot(Some(10.0)), at(step));
        }
        assert_eq!(monitor.history().len(), 3);
        assert_eq!(
            monitor.history().samples().front().unwrap().observed_at,
            at(2)
        );
        assert_eq!(
            monitor.history().samples().back().unwrap().observed_at,
            at(4)
        );
    }

    #[test]
    fn collection_errors_are_emitted_only_when_the_issue_set_changes() {
        let issue = CollectionIssue {
            component: "memory".into(),
            message: "unavailable".into(),
        };
        let mut monitor = monitor();
        let with_issue = snapshot(None, None, vec![], vec![issue.clone()]);
        let first = monitor.observe(&with_issue, at(0));
        assert!(matches!(
            first.as_slice(),
            [MonitorEvent::CollectionError { .. }]
        ));
        let second = monitor.observe(&with_issue, at(1));
        assert!(second.is_empty());

        monitor.observe(&snapshot(None, None, vec![], vec![]), at(2));
        let third = monitor.observe(&with_issue, at(3));
        assert!(matches!(
            third.as_slice(),
            [MonitorEvent::CollectionError { .. }]
        ));
    }

    #[test]
    fn retained_recovered_incidents_are_bounded() {
        let config = MonitorConfig {
            max_retained_incidents: 2,
            ..MonitorConfig::default()
        };
        let mut monitor = Monitor::new(config).unwrap();

        let mut step = 0;
        for _ in 0..4 {
            for _ in 0..6 {
                monitor.observe(&cpu_snapshot(Some(95.0)), at(step));
                step += 1;
            }
            monitor.observe(&cpu_snapshot(Some(10.0)), at(step));
            step += 1;
        }
        assert_eq!(monitor.incidents().len(), 2);
        assert!(
            monitor
                .incidents()
                .iter()
                .all(|incident| incident.state == IncidentState::Recovered)
        );
    }

    #[test]
    fn engine_collects_and_emits_events_then_stops_promptly() {
        let defaults = MonitorConfig::default();
        let config = MonitorConfig {
            sampling_interval: std::time::Duration::from_millis(250),
            history_limit: 10,
            cpu: crate::config::CpuDetectionConfig {
                sustained_for: std::time::Duration::from_millis(500),
                ..defaults.cpu
            },
            ..defaults
        };
        let monitor = Monitor::new(config).unwrap();

        let handle = monitor
            .start(|| cpu_snapshot(Some(95.0)))
            .expect("monitor thread should start");
        let event = handle
            .recv_timeout(std::time::Duration::from_secs(5))
            .expect("expected an incident event");
        assert!(matches!(event, MonitorEvent::IncidentStarted { .. }));

        let started = std::time::Instant::now();
        handle.stop().expect("monitor thread should stop cleanly");
        assert!(started.elapsed() < std::time::Duration::from_secs(2));
    }

    #[test]
    fn engine_respects_the_interval_and_never_overlaps_collections() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::{Arc, Mutex};

        let config = MonitorConfig {
            sampling_interval: std::time::Duration::from_millis(250),
            history_limit: 10,
            ..MonitorConfig::default()
        };
        let monitor = Monitor::new(config).unwrap();

        let active = Arc::new(AtomicUsize::new(0));
        let max_active = Arc::new(AtomicUsize::new(0));
        let starts = Arc::new(Mutex::new(Vec::new()));
        let source_active = Arc::clone(&active);
        let source_max = Arc::clone(&max_active);
        let source_starts = Arc::clone(&starts);
        let handle = monitor
            .start(move || {
                let concurrent = source_active.fetch_add(1, Ordering::SeqCst) + 1;
                source_max.fetch_max(concurrent, Ordering::SeqCst);
                source_starts
                    .lock()
                    .unwrap()
                    .push(std::time::Instant::now());
                std::thread::sleep(std::time::Duration::from_millis(20));
                source_active.fetch_sub(1, Ordering::SeqCst);
                cpu_snapshot(Some(10.0))
            })
            .expect("monitor thread should start");

        std::thread::sleep(std::time::Duration::from_millis(900));
        handle.stop().expect("monitor thread should stop cleanly");

        let starts = starts.lock().unwrap();
        assert!(
            starts.len() >= 3,
            "expected at least three collections, got {}",
            starts.len()
        );
        for window in starts.windows(2) {
            assert!(
                window[1].duration_since(window[0]) >= std::time::Duration::from_millis(200),
                "collection started too early: {:?}",
                window[1].duration_since(window[0])
            );
        }
        assert_eq!(max_active.load(Ordering::SeqCst), 1);
    }
}
