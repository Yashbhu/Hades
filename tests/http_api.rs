//! End-to-end HTTP tests against a real loopback service.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use os_agent::bridge::ServerConfig;
use os_agent::bridge::protocol::PROTOCOL_VERSION;
use os_agent::config::MonitorConfig;
use serde_json::{Value, json};

struct RunningService {
    address: SocketAddr,
    shutdown: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl RunningService {
    fn start(max_request_bytes: usize) -> Self {
        let config = ServerConfig {
            bind: "127.0.0.1:0".parse().unwrap(),
            max_request_bytes,
            monitor: MonitorConfig {
                sampling_interval: Duration::from_millis(250),
                ..MonitorConfig::default()
            },
        };
        let service = os_agent::AgentService::bind(config).unwrap();
        let address = service.local_addr().unwrap();
        let shutdown = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&shutdown);
        let thread = std::thread::spawn(move || service.serve(&flag).unwrap());
        Self {
            address,
            shutdown,
            thread: Some(thread),
        }
    }

    fn stop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for RunningService {
    fn drop(&mut self) {
        self.stop();
    }
}

fn http_request(
    address: SocketAddr,
    method: &str,
    path: &str,
    content_type: Option<&str>,
    body: Option<&str>,
) -> (u16, Value) {
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let body = body.unwrap_or("");
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    if let Some(content_type) = content_type {
        request.push_str(&format!("Content-Type: {content_type}\r\n"));
    }
    request.push_str("\r\n");
    request.push_str(body);
    stream.write_all(request.as_bytes()).unwrap();

    let mut response = Vec::new();
    stream.read_to_end(&mut response).unwrap();
    let response = String::from_utf8(response).unwrap();
    let (head, body) = response.split_once("\r\n\r\n").expect("HTTP headers");
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|status| status.parse().ok())
        .expect("HTTP status");
    assert!(
        head.to_ascii_lowercase()
            .contains("content-type: application/json"),
        "expected JSON content type, got {head}"
    );
    let payload: Value = serde_json::from_str(body).expect("JSON response body");
    (status, payload)
}

fn call(address: SocketAddr, request_id: &str, tool: &str, arguments: Value) -> (u16, Value) {
    let body = json!({
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "tool": tool,
        "arguments": arguments,
    })
    .to_string();
    http_request(
        address,
        "POST",
        "/v1/tools/call",
        Some("application/json"),
        Some(&body),
    )
}

#[test]
fn discovery_lists_the_registered_tools_with_schemas() {
    let service = RunningService::start(64 * 1024);
    let (status, body) = http_request(service.address, "GET", "/v1/tools", None, None);
    assert_eq!(status, 200);
    assert_eq!(body["protocol_version"], PROTOCOL_VERSION);
    let tools = body["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 5);
    let names: Vec<&str> = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
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
    for tool in tools {
        assert!(
            tool["description"]
                .as_str()
                .is_some_and(|value| !value.is_empty())
        );
        assert_eq!(tool["input_schema"]["type"], "object");
        assert_eq!(tool["input_schema_version"], 1);
        assert_eq!(tool["output_schema_version"], 1);
    }
}

#[test]
fn system_metrics_call_returns_a_versioned_success_envelope() {
    let service = RunningService::start(64 * 1024);
    let (status, body) = call(
        service.address,
        "metrics-1",
        "get_system_metrics",
        json!({}),
    );
    assert_eq!(status, 200);
    assert_eq!(body["protocol_version"], PROTOCOL_VERSION);
    assert_eq!(body["request_id"], "metrics-1");
    assert_eq!(body["status"], "ok");
    assert_eq!(body["result"]["schema_version"], 1);
    assert!(body["result"]["data"]["collected_at"].is_string());
    assert!(body["result"]["data"]["disks"].is_array());
    assert!(body.get("error").is_none());
}

#[test]
fn list_top_processes_honors_the_limit() {
    let service = RunningService::start(64 * 1024);
    let (status, body) = call(
        service.address,
        "processes-1",
        "list_top_processes",
        json!({ "sort": "memory", "limit": 2 }),
    );
    assert_eq!(status, 200);
    assert_eq!(body["result"]["data"]["sorted_by"], "memory");
    assert_eq!(body["result"]["data"]["limit"], 2);
    let items = body["result"]["data"]["items"].as_array().unwrap();
    assert!(items.len() <= 2);
}

#[test]
fn inspect_process_finds_a_live_process_and_rejects_missing_ones() {
    let service = RunningService::start(64 * 1024);
    let pid = std::process::id();
    let (status, body) = call(
        service.address,
        "inspect-1",
        "inspect_process",
        json!({ "pid": pid }),
    );
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["status"], "ok");
    assert_eq!(body["result"]["data"]["pid"], pid);
    assert!(body["result"]["data"]["name"].is_string());

    let (status, body) = call(
        service.address,
        "inspect-2",
        "inspect_process",
        json!({ "pid": u32::MAX }),
    );
    assert_eq!(status, 404);
    assert_eq!(body["error"]["code"], "process_unavailable");
}

#[test]
fn recent_incidents_returns_an_array_and_respects_the_limit() {
    let service = RunningService::start(64 * 1024);
    let (status, body) = call(
        service.address,
        "incidents-1",
        "get_recent_incidents",
        json!({ "limit": 1 }),
    );
    assert_eq!(status, 200);
    assert!(body["result"]["data"]["incidents"].is_array());
    assert!(
        body["result"]["data"]["incidents"]
            .as_array()
            .unwrap()
            .len()
            <= 1
    );
    assert!(body["result"]["data"]["total_known"].as_u64().is_some());
}

#[test]
fn unknown_tools_and_invalid_arguments_are_rejected_without_dispatch() {
    let service = RunningService::start(64 * 1024);
    let (status, body) = call(service.address, "unknown-1", "execute_shell", json!({}));
    assert_eq!(status, 404);
    assert_eq!(body["status"], "error");
    assert_eq!(body["request_id"], "unknown-1");
    assert_eq!(body["error"]["code"], "unknown_tool");

    let (status, body) = call(
        service.address,
        "invalid-1",
        "list_top_processes",
        json!({ "limit": 1000 }),
    );
    assert_eq!(status, 422);
    assert_eq!(body["error"]["code"], "invalid_arguments");
}

#[test]
fn malformed_envelopes_and_protocol_mismatches_are_rejected() {
    let service = RunningService::start(64 * 1024);
    let (status, body) = http_request(
        service.address,
        "POST",
        "/v1/tools/call",
        Some("application/json"),
        Some("{not-json"),
    );
    assert_eq!(status, 400);
    assert_eq!(body["error"]["code"], "invalid_request");
    assert!(body["request_id"].is_null());

    let body = json!({
        "protocol_version": 99,
        "request_id": "version-1",
        "tool": "get_system_metrics",
        "arguments": {}
    })
    .to_string();
    let (status, body) = http_request(
        service.address,
        "POST",
        "/v1/tools/call",
        Some("application/json"),
        Some(&body),
    );
    assert_eq!(status, 400);
    assert_eq!(body["error"]["code"], "unsupported_protocol_version");
    assert_eq!(body["request_id"], "version-1");
}

#[test]
fn transport_limits_and_routes_are_enforced() {
    let service = RunningService::start(1024);
    let (status, body) = http_request(
        service.address,
        "POST",
        "/v1/tools/call",
        Some("text/plain"),
        Some("{}"),
    );
    assert_eq!(status, 415);
    assert_eq!(body["error"]["code"], "unsupported_media_type");

    let oversized = "x".repeat(2048);
    let (status, body) = http_request(
        service.address,
        "POST",
        "/v1/tools/call",
        Some("application/json"),
        Some(&oversized),
    );
    assert_eq!(status, 413);
    assert_eq!(body["error"]["code"], "request_too_large");

    let (status, body) = http_request(service.address, "GET", "/v1/tools/call", None, None);
    assert_eq!(status, 405);
    assert_eq!(body["error"]["code"], "method_not_allowed");

    let (status, body) = http_request(service.address, "GET", "/nope", None, None);
    assert_eq!(status, 404);
    assert_eq!(body["error"]["code"], "not_found");
}

#[test]
fn service_shuts_down_promptly_even_with_a_long_monitor_interval() {
    let config = ServerConfig {
        bind: "127.0.0.1:0".parse().unwrap(),
        monitor: MonitorConfig::default(),
        ..ServerConfig::default()
    };
    let service = os_agent::AgentService::bind(config).unwrap();
    let shutdown = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&shutdown);
    let thread = std::thread::spawn(move || service.serve(&flag).unwrap());
    std::thread::sleep(Duration::from_millis(50));
    let started = Instant::now();
    shutdown.store(true, Ordering::SeqCst);
    thread.join().unwrap();
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "shutdown took {:?}",
        started.elapsed()
    );
}
