//! Local HTTP bridge for read-only diagnostic tools.
//!
//! The service binds only to loopback, accepts a single versioned JSON call
//! endpoint, and exposes discovery through a second endpoint. It deliberately
//! has no endpoint for commands, scripts, file mutation, or arbitrary paths.

pub mod protocol;
pub mod tools;

use std::io::{self, Read};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;
use tiny_http::{Header, Method, Request, Response, Server};

use crate::config::{ConfigError, MonitorConfig};
use crate::monitor::{Monitor, MonitorHandle};
use crate::snapshot::Collector;
use protocol::{
    ErrorCode, PROTOCOL_VERSION, ToolCallRequest, ToolCallResponse, ToolDiscoveryResponse,
    ToolError, validate_request_id,
};
use tools::{IncidentStore, ToolContext, ToolRegistry};

/// Default loopback bind address.
pub const DEFAULT_BIND_ADDRESS: &str = "127.0.0.1:8765";
/// Default maximum JSON request body size (64 KiB).
pub const DEFAULT_MAX_REQUEST_BYTES: usize = 64 * 1024;
/// Smallest accepted request body limit. Enough for every documented request.
pub const MIN_REQUEST_BYTES: usize = 1024;
/// Largest accepted request body limit.
pub const MAX_REQUEST_BYTES: usize = 1024 * 1024;

const TOOL_PATH: &str = "/v1/tools";
const CALL_PATH: &str = "/v1/tools/call";
const SERVER_POLL_INTERVAL: Duration = Duration::from_millis(100);

/// Service configuration.
#[derive(Debug, Clone)]
pub struct ServerConfig {
    /// Loopback socket address to bind.
    pub bind: SocketAddr,
    /// Maximum accepted JSON request body, in bytes.
    pub max_request_bytes: usize,
    /// Monitoring configuration used to maintain incident state.
    pub monitor: MonitorConfig,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind: DEFAULT_BIND_ADDRESS
                .parse()
                .expect("valid default bind address"),
            max_request_bytes: DEFAULT_MAX_REQUEST_BYTES,
            monitor: MonitorConfig::default(),
        }
    }
}

impl ServerConfig {
    /// Validate configuration before binding.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if !self.bind.ip().is_loopback() {
            return Err(ConfigError::new(format!(
                "bind address must be loopback, got {}",
                self.bind
            )));
        }
        if !(MIN_REQUEST_BYTES..=MAX_REQUEST_BYTES).contains(&self.max_request_bytes) {
            return Err(ConfigError::new(format!(
                "max_request_bytes must be between {MIN_REQUEST_BYTES} and {MAX_REQUEST_BYTES}"
            )));
        }
        self.monitor.validate()
    }
}

/// Running local diagnostic service.
///
/// `bind` starts the monitoring thread immediately. Call [`AgentService::serve`]
/// to process HTTP requests; dropping the service stops and joins the monitor.
pub struct AgentService {
    server: Server,
    registry: ToolRegistry,
    incidents: Arc<RwLock<IncidentStore>>,
    monitor: Option<MonitorHandle>,
    max_request_bytes: usize,
}

impl AgentService {
    /// Bind the loopback HTTP endpoint and start background monitoring.
    pub fn bind(config: ServerConfig) -> io::Result<Self> {
        config
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;

        let server = Server::http(config.bind)
            .map_err(|error| io::Error::other(format!("failed to bind HTTP server: {error}")))?;

        let incidents = Arc::new(RwLock::new(IncidentStore::new(
            config.monitor.max_retained_incidents,
        )));
        let context = ToolContext::new(Arc::clone(&incidents));
        let registry = ToolRegistry::standard(context);

        let monitor = Monitor::new(config.monitor.clone())
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;
        let mut collector = Collector::without_processes();
        let handle = monitor
            .start(move || collector.collect())
            .map_err(|error| io::Error::other(format!("failed to start monitor: {error}")))?;

        Ok(Self {
            server,
            registry,
            incidents,
            monitor: Some(handle),
            max_request_bytes: config.max_request_bytes,
        })
    }

    /// Actual bound address, including the assigned port when binding port 0.
    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.server
            .server_addr()
            .to_ip()
            .ok_or_else(|| io::Error::other("service is not bound to an IP socket"))
    }

    /// Serve until `shutdown` is set.
    ///
    /// The loop checks for shutdown at least every 100 ms. An in-flight
    /// request is allowed to complete; the monitor is stopped when the service
    /// is dropped.
    pub fn serve(mut self, shutdown: &AtomicBool) -> io::Result<()> {
        while !shutdown.load(Ordering::Relaxed) {
            self.drain_events();
            match self.server.recv_timeout(SERVER_POLL_INTERVAL) {
                Ok(Some(request)) => self.handle_request(request),
                Ok(None) => {}
                Err(error) => {
                    self.drain_events();
                    return Err(error);
                }
            }
        }
        self.drain_events();
        Ok(())
    }

    fn drain_events(&mut self) {
        let Some(monitor) = self.monitor.as_ref() else {
            return;
        };
        while let Ok(event) = monitor.try_recv() {
            let mut store = self
                .incidents
                .write()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            store.apply(&event);
        }
    }

    fn handle_request(&self, request: Request) {
        let path = request
            .url()
            .split('?')
            .next()
            .unwrap_or_default()
            .to_owned();

        if path == TOOL_PATH {
            if request.method() != &Method::Get {
                self.send_envelope(
                    request,
                    405,
                    None,
                    ErrorCode::MethodNotAllowed,
                    format!("{TOOL_PATH} only supports GET"),
                );
                return;
            }
            let discovery = ToolDiscoveryResponse::success(self.registry.descriptors());
            self.send_json(request, 200, &discovery);
            return;
        }

        if path == CALL_PATH {
            if request.method() != &Method::Post {
                self.send_envelope(
                    request,
                    405,
                    None,
                    ErrorCode::MethodNotAllowed,
                    format!("{CALL_PATH} only supports POST"),
                );
                return;
            }
            self.handle_call(request);
            return;
        }

        self.send_envelope(
            request,
            404,
            None,
            ErrorCode::NotFound,
            format!("no endpoint at '{path}'"),
        );
    }

    fn handle_call(&self, mut request: Request) {
        let content_type = request
            .headers()
            .iter()
            .find(|header| header.field.equiv("Content-Type"))
            .map(|header| header.value.as_str().to_owned());
        if !content_type
            .as_deref()
            .is_some_and(|value| value.to_ascii_lowercase().starts_with("application/json"))
        {
            self.send_envelope(
                request,
                415,
                None,
                ErrorCode::UnsupportedMediaType,
                "Content-Type must be application/json",
            );
            return;
        }

        if let Some(length) = content_length(&request) {
            if length > self.max_request_bytes {
                self.send_envelope(
                    request,
                    413,
                    None,
                    ErrorCode::RequestTooLarge,
                    format!(
                        "request body exceeds the {} byte limit",
                        self.max_request_bytes
                    ),
                );
                return;
            }
        }

        let mut body = Vec::new();
        let read_result = request
            .as_reader()
            .take((self.max_request_bytes + 1) as u64)
            .read_to_end(&mut body);
        if read_result.is_err() {
            self.send_envelope(
                request,
                400,
                None,
                ErrorCode::InvalidRequest,
                "could not read request body",
            );
            return;
        }
        if body.len() > self.max_request_bytes {
            self.send_envelope(
                request,
                413,
                None,
                ErrorCode::RequestTooLarge,
                format!(
                    "request body exceeds the {} byte limit",
                    self.max_request_bytes
                ),
            );
            return;
        }

        let value: Value = match serde_json::from_slice(&body) {
            Ok(value) => value,
            Err(error) => {
                self.send_envelope(
                    request,
                    400,
                    None,
                    ErrorCode::InvalidRequest,
                    format!("invalid JSON request: {error}"),
                );
                return;
            }
        };
        let request_id = value
            .get("request_id")
            .and_then(Value::as_str)
            .filter(|request_id| validate_request_id(request_id).is_ok())
            .map(str::to_owned);

        let call: ToolCallRequest = match serde_json::from_value(value) {
            Ok(call) => call,
            Err(error) => {
                self.send_envelope(
                    request,
                    400,
                    request_id,
                    ErrorCode::InvalidRequest,
                    format!("invalid request envelope: {error}"),
                );
                return;
            }
        };

        if let Err(error) = validate_request_id(&call.request_id) {
            self.send_envelope(request, 400, None, error.code, error.message);
            return;
        }
        if call.protocol_version != PROTOCOL_VERSION {
            self.send_envelope(
                request,
                400,
                Some(call.request_id),
                ErrorCode::UnsupportedProtocolVersion,
                format!(
                    "protocol version {} is not supported; expected {}",
                    call.protocol_version, PROTOCOL_VERSION
                ),
            );
            return;
        }

        match self.registry.call(&call.tool, call.arguments) {
            Ok(result) => {
                let response = ToolCallResponse::success(call.request_id, result);
                self.send_json(request, 200, &response);
            }
            Err(error) => {
                let status = http_status_for(error.code);
                let response = ToolCallResponse::failure(Some(call.request_id), error);
                self.send_json(request, status, &response);
            }
        }
    }

    fn send_envelope(
        &self,
        request: Request,
        status: u16,
        request_id: Option<String>,
        code: ErrorCode,
        message: impl Into<String>,
    ) {
        let response = ToolCallResponse::failure(request_id, ToolError::new(code, message));
        self.send_json(request, status, &response);
    }

    fn send_json<T: Serialize>(&self, request: Request, status: u16, body: &T) {
        let payload = match serde_json::to_vec(body) {
            Ok(payload) => payload,
            Err(error) => {
                let fallback = ToolCallResponse::failure(
                    None,
                    ToolError::new(
                        ErrorCode::InternalError,
                        format!("failed to serialize response: {error}"),
                    ),
                );
                let payload = serde_json::to_vec(&fallback)
                    .unwrap_or_else(|_| b"{\"status\":\"error\"}".to_vec());
                let response = Response::from_data(payload)
                    .with_status_code(500)
                    .with_header(json_header());
                let _ = request.respond(response);
                return;
            }
        };
        let response = Response::from_data(payload)
            .with_status_code(status)
            .with_header(json_header())
            .with_header(no_store_header());
        // Client disconnects are not server failures.
        let _ = request.respond(response);
    }
}

impl Drop for AgentService {
    fn drop(&mut self) {
        if let Some(monitor) = self.monitor.take() {
            let _ = monitor.stop();
        }
    }
}

fn content_length(request: &Request) -> Option<usize> {
    request
        .headers()
        .iter()
        .find(|header| header.field.equiv("Content-Length"))
        .and_then(|header| header.value.as_str().parse().ok())
}

fn http_status_for(code: ErrorCode) -> u16 {
    match code {
        ErrorCode::InvalidRequest | ErrorCode::UnsupportedProtocolVersion => 400,
        ErrorCode::UnknownTool | ErrorCode::ProcessUnavailable | ErrorCode::NotFound => 404,
        ErrorCode::InvalidArguments => 422,
        ErrorCode::RequestTooLarge => 413,
        ErrorCode::UnsupportedMediaType => 415,
        ErrorCode::MethodNotAllowed => 405,
        ErrorCode::InternalError => 500,
    }
}

fn json_header() -> Header {
    Header::from_bytes(
        &b"Content-Type"[..],
        &b"application/json; charset=utf-8"[..],
    )
    .expect("static header is valid")
}

fn no_store_header() -> Header {
    Header::from_bytes(&b"Cache-Control"[..], &b"no-store"[..]).expect("static header is valid")
}

#[cfg(test)]
mod tests {
    use super::{AgentService, ServerConfig};
    use crate::config::{CpuDetectionConfig, MonitorConfig};
    use crate::monitor::Monitor;
    use std::net::{IpAddr, Ipv4Addr, SocketAddr};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::Duration;

    #[test]
    fn rejects_non_loopback_bind_addresses() {
        let config = ServerConfig {
            bind: SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 8765),
            ..ServerConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn rejects_invalid_request_body_limits() {
        let config = ServerConfig {
            max_request_bytes: 10,
            ..ServerConfig::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn binds_to_an_assigned_loopback_port_and_stops() {
        let config = ServerConfig {
            bind: "127.0.0.1:0".parse().unwrap(),
            monitor: MonitorConfig {
                sampling_interval: Duration::from_millis(250),
                cpu: CpuDetectionConfig {
                    sustained_for: Duration::from_millis(500),
                    ..MonitorConfig::default().cpu
                },
                ..MonitorConfig::default()
            },
            ..ServerConfig::default()
        };
        let service = AgentService::bind(config).unwrap();
        let address = service.local_addr().unwrap();
        assert!(address.ip().is_loopback());
        assert_ne!(address.port(), 0);

        let shutdown = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&shutdown);
        let thread = std::thread::spawn(move || service.serve(&flag).unwrap());
        shutdown.store(true, Ordering::Relaxed);
        thread.join().unwrap();
    }

    #[test]
    fn monitor_config_is_validated() {
        let mut monitor = MonitorConfig::default();
        monitor.cpu.threshold_percent = -1.0;
        let config = ServerConfig {
            monitor,
            ..ServerConfig::default()
        };
        assert!(config.validate().is_err());
        // Keep the import meaningful for the direct monitor path used above.
        assert!(Monitor::new(MonitorConfig::default()).is_ok());
    }
}
