use assert_cmd::Command;
use predicates::prelude::*;
use serde_json::Value;

fn os_agent() -> Command {
    Command::cargo_bin("os-agent").expect("the os-agent binary should build")
}

fn process_row_count(stdout: &str) -> usize {
    stdout
        .lines()
        .filter(|line| {
            line.trim_start()
                .chars()
                .next()
                .is_some_and(|character| character.is_ascii_digit())
        })
        .count()
}

#[test]
fn status_prints_a_human_readable_report() {
    os_agent()
        .arg("status")
        .assert()
        .success()
        .stdout(predicate::str::contains("Hades system health"))
        .stdout(predicate::str::contains("Collected:"))
        .stdout(predicate::str::contains("CPU:"))
        .stdout(predicate::str::contains("Memory:"))
        .stdout(predicate::str::contains("Top processes"));
}

#[test]
fn status_json_is_valid_and_uses_the_documented_schema() {
    let output = os_agent()
        .args(["status", "--json"])
        .output()
        .expect("failed to run os-agent");
    assert!(output.status.success(), "status --json failed: {output:?}");

    let snapshot: Value =
        serde_json::from_slice(&output.stdout).expect("status --json must emit valid JSON");

    assert_eq!(snapshot["schema_version"], 1);
    assert!(
        snapshot["collected_at"]
            .as_str()
            .is_some_and(|value| value.ends_with('Z')),
        "collected_at should be an RFC 3339 UTC timestamp"
    );

    let cpu = snapshot["cpu"].as_object().expect("cpu must be an object");
    let overall = cpu["overall_percent"]
        .as_f64()
        .expect("cpu.overall_percent must be a number");
    assert!((0.0..=100.0).contains(&overall));
    assert!(
        cpu["logical_core_count"]
            .as_u64()
            .is_some_and(|count| count >= 1)
    );
    assert!(cpu["sampling_interval_ms"].as_u64().unwrap_or(0) >= 200);
    assert!(cpu["per_core_percent"].is_array());

    let memory = snapshot["memory"]
        .as_object()
        .expect("memory must be an object");
    assert!(memory["total_bytes"].as_u64().unwrap_or(0) > 0);
    let memory_percent = memory["used_percent"].as_f64().expect("used_percent");
    assert!((0.0..=100.0).contains(&memory_percent));
    assert!(memory["available_bytes"].is_u64());
    assert!(memory["free_bytes"].is_u64());

    assert!(snapshot["disks"].is_array());
    assert_eq!(snapshot["processes"]["sorted_by"], "cpu");
    assert_eq!(snapshot["processes"]["limit"], 10);
    assert!(snapshot["processes"]["items"].is_array());
    assert!(snapshot["issues"].is_array());
}

#[test]
fn processes_sorted_by_cpu_respects_the_limit() {
    let output = os_agent()
        .args(["processes", "--sort", "cpu", "--limit", "3"])
        .output()
        .expect("failed to run os-agent");
    assert!(output.status.success(), "processes failed: {output:?}");
    let stdout = String::from_utf8(output.stdout).expect("stdout should be UTF-8");
    assert!(stdout.contains("Top processes"));
    assert!(stdout.contains("sorted by cpu"));
    assert!(stdout.contains("PID"));
    assert!(stdout.contains("NAME"));
    assert!(process_row_count(&stdout) <= 3);
}

#[test]
fn processes_sorted_by_memory_works() {
    os_agent()
        .args(["processes", "--sort", "memory", "--limit", "1"])
        .assert()
        .success()
        .stdout(predicate::str::contains("sorted by memory"));
}

#[test]
fn default_process_limit_is_ten() {
    let output = os_agent()
        .arg("processes")
        .output()
        .expect("failed to run os-agent");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("stdout should be UTF-8");
    assert!(process_row_count(&stdout) <= 10);
    assert!(stdout.contains("sorted by cpu"));
}

#[test]
fn rejects_unknown_sort_metric() {
    os_agent()
        .args(["processes", "--sort", "disk"])
        .assert()
        .failure()
        .code(2)
        .stderr(predicate::str::contains("invalid value"));
}

#[test]
fn rejects_limit_below_one() {
    os_agent()
        .args(["processes", "--limit", "0"])
        .assert()
        .failure()
        .code(2)
        .stderr(predicate::str::contains("limit must be between 1 and 100"));
}

#[test]
fn rejects_limit_above_the_upper_bound() {
    os_agent()
        .args(["processes", "--limit", "101"])
        .assert()
        .failure()
        .code(2)
        .stderr(predicate::str::contains("limit must be between 1 and 100"));
}

#[test]
fn rejects_non_numeric_limit() {
    os_agent()
        .args(["processes", "--limit", "many"])
        .assert()
        .failure()
        .code(2)
        .stderr(predicate::str::contains("not a valid integer"));
}

#[test]
fn requires_a_subcommand() {
    os_agent()
        .assert()
        .failure()
        .code(2)
        .stderr(predicate::str::contains("Usage"));
}

#[test]
fn prints_version() {
    os_agent()
        .arg("--version")
        .assert()
        .success()
        .stdout(predicate::str::contains(env!("CARGO_PKG_VERSION")));
}
