//! Human-readable presentation of snapshots and process lists.
//!
//! Rendering is pure: it never touches the operating system and is therefore
//! easy to test deterministically.

use std::fmt::Write as _;

use crate::metrics::processes::TopProcesses;
use crate::snapshot::Snapshot;

const WRITE_ERROR: &str = "writing to a String cannot fail";
const MAX_DISPLAYED_NAME_CHARS: usize = 64;

/// Format a byte count using binary units (`KiB`, `MiB`, `GiB`, ...).
pub fn format_bytes(bytes: u64) -> String {
    const UNITS: [&str; 6] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    if bytes < 1024 {
        return format!("{bytes} B");
    }
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    format!("{value:.1} {}", UNITS[unit])
}

/// Render a full health report.
pub fn render_status(snapshot: &Snapshot) -> String {
    let mut out = String::new();
    out.push_str("Hades system health\n");
    writeln!(out, "Collected: {}", snapshot.collected_at).expect(WRITE_ERROR);

    match &snapshot.cpu {
        Some(cpu) => {
            writeln!(
                out,
                "CPU: {:.1}% across {} logical cores ({} ms sample)",
                cpu.overall_percent, cpu.logical_core_count, cpu.sampling_interval_ms
            )
            .expect(WRITE_ERROR);
            if !cpu.per_core_percent.is_empty() {
                if cpu.per_core_percent.len() <= 16 {
                    let cores = cpu
                        .per_core_percent
                        .iter()
                        .enumerate()
                        .map(|(index, percent)| format!("core {index}: {percent:.1}%"))
                        .collect::<Vec<_>>()
                        .join(", ");
                    writeln!(out, "  {cores}").expect(WRITE_ERROR);
                } else {
                    writeln!(
                        out,
                        "  Per-core utilization for {} cores is available in JSON output",
                        cpu.per_core_percent.len()
                    )
                    .expect(WRITE_ERROR);
                }
            }
        }
        None => out.push_str("CPU: unavailable (see collection issues)\n"),
    }

    match &snapshot.memory {
        Some(memory) => {
            writeln!(
                out,
                "Memory: {} used / {} total ({:.1}%), {} available, {} free",
                format_bytes(memory.used_bytes),
                format_bytes(memory.total_bytes),
                memory.used_percent,
                format_bytes(memory.available_bytes),
                format_bytes(memory.free_bytes)
            )
            .expect(WRITE_ERROR);
        }
        None => out.push_str("Memory: unavailable (see collection issues)\n"),
    }

    if snapshot.disks.is_empty() {
        out.push_str("Disks: unavailable (see collection issues)\n");
    } else {
        out.push_str("Disks:\n");
        for disk in &snapshot.disks {
            let file_system = disk.file_system.as_deref().unwrap_or("unknown");
            writeln!(
                out,
                "  {} [{}]: {} used / {} ({:.1}%), {} available",
                disk.mount_point,
                file_system,
                format_bytes(disk.used_bytes),
                format_bytes(disk.total_bytes),
                disk.used_percent,
                format_bytes(disk.available_bytes)
            )
            .expect(WRITE_ERROR);
        }
    }

    out.push_str(&render_processes(&snapshot.processes));

    if !snapshot.issues.is_empty() {
        out.push_str("Collection issues:\n");
        for issue in &snapshot.issues {
            writeln!(out, "  - {}: {}", issue.component, issue.message).expect(WRITE_ERROR);
        }
    }

    out
}

/// Render a process table with its sort order and displayed count.
pub fn render_processes(processes: &TopProcesses) -> String {
    let mut out = String::new();
    if processes.items.is_empty() {
        writeln!(
            out,
            "Top processes (0 shown, sorted by {}): none reported",
            processes.sorted_by.as_str()
        )
        .expect(WRITE_ERROR);
        return out;
    }
    writeln!(
        out,
        "Top processes ({} shown, sorted by {}):",
        processes.items.len(),
        processes.sorted_by.as_str()
    )
    .expect(WRITE_ERROR);
    writeln!(out, "  {:<8}{:<8}{:<12}NAME", "PID", "CPU%", "MEMORY").expect(WRITE_ERROR);
    for process in &processes.items {
        writeln!(
            out,
            "  {:<8}{:<8}{:<12}{}",
            process.pid,
            format!("{:.1}", process.cpu_percent),
            format_bytes(process.memory_bytes),
            display_name(&process.name)
        )
        .expect(WRITE_ERROR);
    }
    out
}

fn display_name(name: &str) -> String {
    let mut sanitized: String = name
        .chars()
        .map(|character| {
            if character.is_control() {
                '?'
            } else {
                character
            }
        })
        .take(MAX_DISPLAYED_NAME_CHARS + 1)
        .collect();
    if sanitized.chars().count() > MAX_DISPLAYED_NAME_CHARS {
        sanitized = sanitized
            .chars()
            .take(MAX_DISPLAYED_NAME_CHARS - 3)
            .collect();
        sanitized.push_str("...");
    }
    sanitized
}

#[cfg(test)]
mod tests {
    use super::{format_bytes, render_processes, render_status};
    use crate::metrics::ProcessSort;
    use crate::metrics::cpu::CpuMetrics;
    use crate::metrics::disk::DiskMetrics;
    use crate::metrics::memory::MemoryMetrics;
    use crate::metrics::processes::{ProcessMetrics, TopProcesses};
    use crate::snapshot::{CollectionIssue, SCHEMA_VERSION, Snapshot};

    #[test]
    fn formats_bytes_with_binary_units() {
        assert_eq!(format_bytes(0), "0 B");
        assert_eq!(format_bytes(1023), "1023 B");
        assert_eq!(format_bytes(1024), "1.0 KiB");
        assert_eq!(format_bytes(1536), "1.5 KiB");
        assert_eq!(format_bytes(1024 * 1024 * 1024), "1.0 GiB");
    }

    fn snapshot() -> Snapshot {
        Snapshot {
            schema_version: SCHEMA_VERSION,
            collected_at: "2026-01-02T03:04:05.678Z".to_owned(),
            cpu: Some(CpuMetrics::new(12.5, vec![10.0, 15.0], 200).unwrap()),
            memory: Some(MemoryMetrics::from_raw(1_000, 400, 500, 100).unwrap()),
            disks: vec![
                DiskMetrics::from_raw("/".into(), Some("apfs".into()), 1_000, 250).unwrap(),
            ],
            processes: TopProcesses::from_processes(
                vec![ProcessMetrics {
                    pid: 42,
                    name: "worker".into(),
                    cpu_percent: 20.0,
                    memory_bytes: 1_024,
                }],
                ProcessSort::Cpu,
                10,
            ),
            issues: vec![],
        }
    }

    #[test]
    fn renders_all_sections() {
        let report = render_status(&snapshot());
        assert!(report.contains("Hades system health"));
        assert!(report.contains("CPU: 12.5% across 2 logical cores"));
        assert!(report.contains("Memory: 400 B used / 1000 B total (40.0%)"));
        assert!(report.contains("/ [apfs]"));
        assert!(report.contains("Top processes (1 shown, sorted by cpu)"));
        assert!(report.contains("worker"));
        assert!(!report.contains("Collection issues:"));
    }

    #[test]
    fn renders_unavailable_metrics_without_zeroes() {
        let mut snapshot = snapshot();
        snapshot.cpu = None;
        snapshot.memory = None;
        snapshot.disks.clear();
        snapshot.issues = vec![CollectionIssue {
            component: "memory".into(),
            message: "total memory was reported as zero".into(),
        }];
        let report = render_status(&snapshot);
        assert!(report.contains("CPU: unavailable"));
        assert!(report.contains("Memory: unavailable"));
        assert!(report.contains("Disks: unavailable"));
        assert!(report.contains("Collection issues:"));
        assert!(report.contains("memory: total memory was reported as zero"));
    }

    #[test]
    fn renders_empty_process_list() {
        let processes = TopProcesses::from_processes(vec![], ProcessSort::Memory, 5);
        let report = render_processes(&processes);
        assert!(report.contains("0 shown, sorted by memory"));
        assert!(report.contains("none reported"));
    }

    #[test]
    fn sanitizes_control_characters_in_names() {
        let processes = TopProcesses::from_processes(
            vec![ProcessMetrics {
                pid: 7,
                name: "bad\nname".into(),
                cpu_percent: 1.0,
                memory_bytes: 2,
            }],
            ProcessSort::Cpu,
            1,
        );
        let report = render_processes(&processes);
        assert!(report.contains("bad?name"));
        assert!(!report.contains("bad\nname"));
    }
}
