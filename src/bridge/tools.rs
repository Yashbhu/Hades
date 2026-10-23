//! Explicitly registered, read-only diagnostic tools.
//!
//! Only operations listed in [`ToolRegistry::standard`] can be called. Inputs
//! are deserialized into closed structs (unknown fields are rejected), and
//! results are plain serializable data with a versioned output schema.

use std::sync::{Arc, RwLock};

use serde::Deserialize;
use serde_json::{Value, json};
use sysinfo::{MINIMUM_CPU_UPDATE_INTERVAL, Pid, ProcessesToUpdate, System};

use crate::bridge::protocol::{
    ErrorCode, MAX_INCIDENT_LIMIT, ToolDescriptor, ToolError, ToolResult, limit_schema,
};
use crate::events::MonitorEvent;
use crate::incident::Incident;
use crate::metrics::{DEFAULT_PROCESS_LIMIT, MAX_PROCESS_LIMIT, ProcessSort};
use crate::snapshot::Collector;

/// Stable tool names.
pub const GET_SYSTEM_METRICS: &str = "get_system_metrics";
/// Stable tool name.
pub const LIST_TOP_PROCESSES: &str = "list_top_processes";
/// Stable tool name.
pub const INSPECT_PROCESS: &str = "inspect_process";
/// Stable tool name.
pub const GET_DISK_USAGE: &str = "get_disk_usage";
/// Stable tool name.
pub const GET_RECENT_INCIDENTS: &str = "get_recent_incidents";

/// Default and maximum number of incidents returned by one call.
pub const DEFAULT_INCIDENT_LIMIT: usize = 20;

/// Bounded, thread-safe mirror of incidents emitted by the monitor.
///
/// The monitor owns detection state; this store only mirrors the latest full
/// incident carried by each event. It intentionally retains a bounded number
/// of recovered incidents, matching [`crate::MonitorConfig`].
#[derive(Debug)]
pub struct IncidentStore {
    max_retained: usize,
    incidents: Vec<Incident>,
}

impl IncidentStore {
    /// Create a store that retains at most `max_retained` recovered incidents.
    pub fn new(max_retained: usize) -> Self {
        Self {
            max_retained,
            incidents: Vec::new(),
        }
    }

    /// Apply one monitor event. Collection errors do not change incidents.
    pub fn apply(&mut self, event: &MonitorEvent) {
        let incident = match event {
            MonitorEvent::IncidentStarted { incident, .. }
            | MonitorEvent::IncidentUpdated { incident, .. }
            | MonitorEvent::IncidentRecovered { incident, .. } => incident,
            MonitorEvent::CollectionError { .. } => return,
        };
        self.upsert(incident.clone());
        self.prune();
    }

    /// Most recently updated incidents, newest first.
    pub fn recent(&self, limit: usize) -> Vec<Incident> {
        let mut incidents = self.incidents.clone();
        incidents.sort_by(|left, right| {
            right
                .updated_at
                .cmp(&left.updated_at)
                .then_with(|| right.id.cmp(&left.id))
        });
        incidents.truncate(limit);
        incidents
    }

    /// Number of incidents currently retained.
    pub fn len(&self) -> usize {
        self.incidents.len()
    }

    /// Whether no incidents are retained.
    pub fn is_empty(&self) -> bool {
        self.incidents.is_empty()
    }

    fn upsert(&mut self, incident: Incident) {
        if let Some(existing) = self
            .incidents
            .iter_mut()
            .find(|existing| existing.id == incident.id)
        {
            *existing = incident;
        } else {
            self.incidents.push(incident);
        }
    }

    fn prune(&mut self) {
        let mut recovered = self
            .incidents
            .iter()
            .filter(|incident| !incident.is_open())
            .count();
        while recovered > self.max_retained {
            let oldest = self
                .incidents
                .iter()
                .enumerate()
                .filter(|(_, incident)| !incident.is_open())
                .min_by_key(|(_, incident)| incident.updated_at)
                .map(|(index, _)| index);
            match oldest {
                Some(index) => {
                    self.incidents.remove(index);
                    recovered -= 1;
                }
                None => break,
            }
        }
    }
}

/// Shared state available to tools.
#[derive(Clone, Debug)]
pub struct ToolContext {
    incidents: Arc<RwLock<IncidentStore>>,
}

impl ToolContext {
    /// Create a context backed by a shared incident store.
    pub fn new(incidents: Arc<RwLock<IncidentStore>>) -> Self {
        Self { incidents }
    }

    /// Shared incident store, used by the service to apply monitor events.
    pub fn incidents(&self) -> &Arc<RwLock<IncidentStore>> {
        &self.incidents
    }
}

struct RegisteredTool {
    name: &'static str,
    description: &'static str,
    input_schema: Value,
    input_schema_version: u32,
    output_schema_version: u32,
    execute: fn(&ToolContext, Value) -> Result<Value, ToolError>,
}

/// Registry of the only callable diagnostics.
pub struct ToolRegistry {
    tools: Vec<RegisteredTool>,
    context: ToolContext,
}

impl ToolRegistry {
    /// Build the standard read-only registry.
    pub fn standard(context: ToolContext) -> Self {
        let empty_schema = json!({
            "type": "object",
            "properties": {},
            "additionalProperties": false
        });
        let tools = vec![
            RegisteredTool {
                name: GET_SYSTEM_METRICS,
                description: "Return a fresh CPU, memory, and filesystem health snapshot.",
                input_schema: empty_schema.clone(),
                input_schema_version: 1,
                output_schema_version: 1,
                execute: get_system_metrics,
            },
            RegisteredTool {
                name: LIST_TOP_PROCESSES,
                description: "Return a bounded list of processes sorted by CPU or memory use.",
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "sort": {
                            "type": "string",
                            "enum": ["cpu", "memory"],
                            "default": "cpu"
                        },
                        "limit": limit_schema(DEFAULT_PROCESS_LIMIT as u64, MAX_PROCESS_LIMIT as u64)
                    },
                    "additionalProperties": false
                }),
                input_schema_version: 1,
                output_schema_version: 1,
                execute: list_top_processes,
            },
            RegisteredTool {
                name: INSPECT_PROCESS,
                description: "Inspect one process by PID. Returns resource use and metadata availability.",
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": u32::MAX
                        }
                    },
                    "required": ["pid"],
                    "additionalProperties": false
                }),
                input_schema_version: 1,
                output_schema_version: 1,
                execute: inspect_process,
            },
            RegisteredTool {
                name: GET_DISK_USAGE,
                description: "Return filesystem capacity and availability without recursive scans.",
                input_schema: empty_schema,
                input_schema_version: 1,
                output_schema_version: 1,
                execute: get_disk_usage,
            },
            RegisteredTool {
                name: GET_RECENT_INCIDENTS,
                description: "Return recent incidents observed by the monitoring engine.",
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "limit": limit_schema(
                            DEFAULT_INCIDENT_LIMIT as u64,
                            MAX_INCIDENT_LIMIT as u64
                        )
                    },
                    "additionalProperties": false
                }),
                input_schema_version: 1,
                output_schema_version: 1,
                execute: get_recent_incidents,
            },
        ];
        Self { tools, context }
    }

    /// Stable discovery metadata, in tool-name order.
    pub fn descriptors(&self) -> Vec<ToolDescriptor> {
        let mut descriptors: Vec<ToolDescriptor> = self
            .tools
            .iter()
            .map(|tool| ToolDescriptor {
                name: tool.name.to_owned(),
                description: tool.description.to_owned(),
                input_schema: tool.input_schema.clone(),
                input_schema_version: tool.input_schema_version,
                output_schema_version: tool.output_schema_version,
            })
            .collect();
        descriptors.sort_by(|left, right| left.name.cmp(&right.name));
        descriptors
    }

    /// Execute a registered tool. Unknown names are never dispatched.
    pub fn call(&self, name: &str, arguments: Value) -> Result<ToolResult, ToolError> {
        let tool = self
            .tools
            .iter()
            .find(|tool| tool.name == name)
            .ok_or_else(|| {
                ToolError::new(ErrorCode::UnknownTool, format!("unknown tool '{name}'"))
                    .with_details(json!({ "tool": name }))
            })?;

        let arguments = if arguments.is_null() {
            json!({})
        } else {
            arguments
        };
        if !arguments.is_object() {
            return Err(ToolError::new(
                ErrorCode::InvalidArguments,
                "arguments must be a JSON object",
            ));
        }

        let data = (tool.execute)(&self.context, arguments)?;
        Ok(ToolResult::new(tool.output_schema_version, data))
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyArguments {}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TopProcessArguments {
    #[serde(default = "default_sort")]
    sort: ProcessSort,
    #[serde(default = "default_process_limit")]
    limit: usize,
}

fn default_sort() -> ProcessSort {
    ProcessSort::Cpu
}

fn default_process_limit() -> usize {
    DEFAULT_PROCESS_LIMIT
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InspectProcessArguments {
    pid: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecentIncidentArguments {
    #[serde(default = "default_incident_limit")]
    limit: usize,
}

fn default_incident_limit() -> usize {
    DEFAULT_INCIDENT_LIMIT
}

fn parse_arguments<T: for<'de> Deserialize<'de>>(arguments: Value) -> Result<T, ToolError> {
    serde_json::from_value(arguments).map_err(|error| {
        ToolError::new(
            ErrorCode::InvalidArguments,
            format!("invalid arguments: {error}"),
        )
    })
}

fn internal_error(message: impl Into<String>) -> ToolError {
    ToolError::new(ErrorCode::InternalError, message)
}

fn get_system_metrics(_context: &ToolContext, arguments: Value) -> Result<Value, ToolError> {
    let _: EmptyArguments = parse_arguments(arguments)?;
    // Process collection is intentionally disabled: this tool is documented as
    // CPU/memory/disk and must not pay for a full process scan.
    let snapshot = Collector::without_processes().collect();
    serde_json::to_value(snapshot)
        .map_err(|error| internal_error(format!("failed to serialize snapshot: {error}")))
}

fn list_top_processes(_context: &ToolContext, arguments: Value) -> Result<Value, ToolError> {
    let arguments: TopProcessArguments = parse_arguments(arguments)?;
    if !(1..=MAX_PROCESS_LIMIT).contains(&arguments.limit) {
        return Err(ToolError::new(
            ErrorCode::InvalidArguments,
            format!("limit must be between 1 and {MAX_PROCESS_LIMIT}"),
        )
        .with_details(json!({ "limit": arguments.limit })));
    }

    let snapshot = Collector::with_processes(arguments.sort, arguments.limit).collect();
    Ok(json!({
        "sorted_by": snapshot.processes.sorted_by,
        "limit": snapshot.processes.limit,
        "items": snapshot.processes.items,
        "issues": snapshot.issues,
    }))
}

fn inspect_process(_context: &ToolContext, arguments: Value) -> Result<Value, ToolError> {
    let arguments: InspectProcessArguments = parse_arguments(arguments)?;
    if arguments.pid == 0 {
        return Err(ToolError::new(
            ErrorCode::InvalidArguments,
            "pid must be a positive process identifier",
        ));
    }

    let pid = Pid::from_u32(arguments.pid);
    let mut system = System::new();
    system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);
    std::thread::sleep(MINIMUM_CPU_UPDATE_INTERVAL);
    system.refresh_processes(ProcessesToUpdate::Some(&[pid]), true);

    let process = system.process(pid).ok_or_else(|| {
        ToolError::new(
            ErrorCode::ProcessUnavailable,
            format!(
                "process {} is not present or is not accessible to this user",
                arguments.pid
            ),
        )
        .with_details(json!({ "pid": arguments.pid }))
    })?;

    let cpu_percent = f64::from(process.cpu_usage());
    if !cpu_percent.is_finite() || cpu_percent < 0.0 {
        return Err(ToolError::new(
            ErrorCode::ProcessUnavailable,
            format!(
                "process {} disappeared or reported an invalid CPU value",
                arguments.pid
            ),
        )
        .with_details(json!({ "pid": arguments.pid })));
    }

    let name = process.name().to_string_lossy();
    let name = if name.is_empty() {
        "unknown".to_owned()
    } else {
        name.into_owned()
    };

    // Deliberately omit command line, environment, user, and executable path.
    Ok(json!({
        "pid": process.pid().as_u32(),
        "name": name,
        "cpu_percent": cpu_percent,
        "memory_bytes": process.memory(),
        "status": format!("{:?}", process.status()).to_lowercase(),
        "run_time_seconds": process.run_time(),
        "executable_path_available": process.exe().is_some(),
    }))
}

fn get_disk_usage(_context: &ToolContext, arguments: Value) -> Result<Value, ToolError> {
    let _: EmptyArguments = parse_arguments(arguments)?;
    let snapshot = Collector::without_processes().collect();
    Ok(json!({
        "disks": snapshot.disks,
        "issues": snapshot.issues,
    }))
}

fn get_recent_incidents(context: &ToolContext, arguments: Value) -> Result<Value, ToolError> {
    let arguments: RecentIncidentArguments = parse_arguments(arguments)?;
    if !(1..=MAX_INCIDENT_LIMIT).contains(&arguments.limit) {
        return Err(ToolError::new(
            ErrorCode::InvalidArguments,
            format!("limit must be between 1 and {MAX_INCIDENT_LIMIT}"),
        )
        .with_details(json!({ "limit": arguments.limit })));
    }

    let store = context
        .incidents
        .read()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let incidents = store.recent(arguments.limit);
    Ok(json!({
        "incidents": incidents,
        "total_known": store.len(),
    }))
}

#[cfg(test)]
mod tests {
    use super::{
        GET_RECENT_INCIDENTS, INSPECT_PROCESS, IncidentStore, LIST_TOP_PROCESSES, ToolContext,
        ToolRegistry,
    };
    use crate::bridge::protocol::{ErrorCode, ToolError};
    use crate::events::MonitorEvent;
    use crate::history::Trend;
    use crate::incident::{
        CpuDerived, CpuObserved, Incident, IncidentEvidence, IncidentKind, IncidentState, Severity,
    };
    use chrono::{TimeZone, Utc};
    use serde_json::json;
    use std::sync::{Arc, RwLock};

    fn incident(id: &str, state: IncidentState, second: u32) -> Incident {
        let timestamp = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, second).unwrap();
        Incident {
            id: id.into(),
            kind: IncidentKind::HighCpu,
            subject: None,
            severity: Severity::Warning,
            state,
            started_at: timestamp,
            updated_at: timestamp,
            last_observed_at: timestamp,
            recovered_at: None,
            summary: "CPU utilization is 95.0%".into(),
            evidence: IncidentEvidence::HighCpu {
                observed: CpuObserved {
                    latest_percent: 95.0,
                    recent_samples: vec![],
                },
                derived: CpuDerived {
                    threshold_percent: 90.0,
                    recovery_threshold_percent: 85.0,
                    critical_threshold_percent: 95.0,
                    consecutive_samples: 6,
                    condition_duration_seconds: 25,
                    recent_average_percent: 94.0,
                    trend: Trend::Rising,
                },
            },
        }
    }

    fn registry(store: IncidentStore) -> ToolRegistry {
        ToolRegistry::standard(ToolContext::new(Arc::new(RwLock::new(store))))
    }

    fn code(error: ToolError) -> ErrorCode {
        error.code
    }

    #[test]
    fn discovers_every_registered_tool_in_stable_order() {
        let registry = registry(IncidentStore::new(100));
        let descriptors = registry.descriptors();
        let names: Vec<&str> = descriptors
            .iter()
            .map(|descriptor| descriptor.name.as_str())
            .collect();
        assert_eq!(
            names,
            vec![
                "get_disk_usage",
                "get_recent_incidents",
                "get_system_metrics",
                "inspect_process",
                "list_top_processes",
            ]
        );
        for descriptor in descriptors {
            assert_eq!(descriptor.input_schema_version, 1);
            assert_eq!(descriptor.output_schema_version, 1);
            assert_eq!(descriptor.input_schema["type"], "object");
            assert_eq!(descriptor.input_schema["additionalProperties"], false);
        }
    }

    #[test]
    fn rejects_unknown_tools_and_non_object_arguments() {
        let registry = registry(IncidentStore::new(100));
        let unknown = registry.call("run_shell", json!({})).unwrap_err();
        assert_eq!(unknown.code, ErrorCode::UnknownTool);

        let non_object = registry
            .call(GET_RECENT_INCIDENTS, json!(["limit", 5]))
            .unwrap_err();
        assert_eq!(non_object.code, ErrorCode::InvalidArguments);
    }

    #[test]
    fn rejects_unknown_and_invalid_tool_arguments() {
        let registry = registry(IncidentStore::new(100));
        assert_eq!(
            code(
                registry
                    .call(LIST_TOP_PROCESSES, json!({ "limit": 0 }))
                    .unwrap_err()
            ),
            ErrorCode::InvalidArguments
        );
        assert_eq!(
            code(
                registry
                    .call(LIST_TOP_PROCESSES, json!({ "limit": 101 }))
                    .unwrap_err()
            ),
            ErrorCode::InvalidArguments
        );
        assert_eq!(
            code(
                registry
                    .call(LIST_TOP_PROCESSES, json!({ "sort": "disk" }))
                    .unwrap_err()
            ),
            ErrorCode::InvalidArguments
        );
        assert_eq!(
            code(
                registry
                    .call(LIST_TOP_PROCESSES, json!({ "extra": true }))
                    .unwrap_err()
            ),
            ErrorCode::InvalidArguments
        );
        assert_eq!(
            code(registry.call(INSPECT_PROCESS, json!({})).unwrap_err()),
            ErrorCode::InvalidArguments
        );
        assert_eq!(
            code(
                registry
                    .call(INSPECT_PROCESS, json!({ "pid": 0 }))
                    .unwrap_err()
            ),
            ErrorCode::InvalidArguments
        );
        assert_eq!(
            code(
                registry
                    .call(GET_RECENT_INCIDENTS, json!({ "limit": 101 }))
                    .unwrap_err()
            ),
            ErrorCode::InvalidArguments
        );
    }

    #[test]
    fn recent_incidents_are_empty_not_an_error() {
        let registry = registry(IncidentStore::new(100));
        let result = registry
            .call(GET_RECENT_INCIDENTS, json!({ "limit": 5 }))
            .unwrap();
        assert_eq!(result.schema_version, 1);
        assert_eq!(result.data["incidents"], json!([]));
        assert_eq!(result.data["total_known"], 0);
    }

    #[test]
    fn incident_store_deduplicates_by_id_and_bounds_results() {
        let mut store = IncidentStore::new(100);
        let started = incident("inc-1", IncidentState::Active, 1);
        store.apply(&MonitorEvent::IncidentStarted {
            timestamp: started.updated_at,
            incident: started.clone(),
        });

        let updated = incident("inc-1", IncidentState::Recovered, 2);
        store.apply(&MonitorEvent::IncidentRecovered {
            timestamp: updated.updated_at,
            incident: updated,
        });
        let other = incident("inc-2", IncidentState::Active, 3);
        store.apply(&MonitorEvent::IncidentUpdated {
            timestamp: other.updated_at,
            incident: other,
        });

        assert_eq!(store.len(), 2);
        let recent = store.recent(1);
        assert_eq!(recent.len(), 1);
        assert_eq!(recent[0].id, "inc-2");
        assert_eq!(store.recent(10).len(), 2);
    }

    #[test]
    fn incident_store_prunes_oldest_recovered_incidents() {
        let mut store = IncidentStore::new(1);
        for (index, second) in [(1, 1), (2, 2)] {
            let recovered = incident(&format!("inc-{index}"), IncidentState::Recovered, second);
            store.apply(&MonitorEvent::IncidentRecovered {
                timestamp: recovered.updated_at,
                incident: recovered,
            });
        }
        let incidents = store.recent(10);
        assert_eq!(incidents.len(), 1);
        assert_eq!(incidents[0].id, "inc-2");
    }

    #[test]
    fn inspect_process_reports_unavailable_for_missing_pid() {
        let registry = registry(IncidentStore::new(100));
        let error = registry
            .call(INSPECT_PROCESS, json!({ "pid": u32::MAX }))
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::ProcessUnavailable);
        assert_eq!(error.details.unwrap()["pid"], u32::MAX);
    }
}
