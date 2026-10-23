//! Versioned JSON protocol shared by the Rust service and Python client.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// Current bridge protocol version.
pub const PROTOCOL_VERSION: u32 = 1;
/// Maximum accepted request identifier length.
pub const MAX_REQUEST_ID_LENGTH: usize = 128;
/// Maximum number of incidents returned by one tool call.
pub const MAX_INCIDENT_LIMIT: usize = 100;

/// Stable machine-readable error codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    /// Envelope or JSON was malformed.
    InvalidRequest,
    /// Protocol version is not supported.
    UnsupportedProtocolVersion,
    /// The tool name is not registered.
    UnknownTool,
    /// Arguments failed validation.
    InvalidArguments,
    /// The requested process is gone or not visible to this user.
    ProcessUnavailable,
    /// Request body exceeded the configured limit.
    RequestTooLarge,
    /// Content type was not `application/json`.
    UnsupportedMediaType,
    /// HTTP method is not allowed for the endpoint.
    MethodNotAllowed,
    /// Endpoint was not found.
    NotFound,
    /// Unexpected server-side failure.
    InternalError,
}

impl ErrorCode {
    /// Stable string representation used in JSON.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::UnsupportedProtocolVersion => "unsupported_protocol_version",
            Self::UnknownTool => "unknown_tool",
            Self::InvalidArguments => "invalid_arguments",
            Self::ProcessUnavailable => "process_unavailable",
            Self::RequestTooLarge => "request_too_large",
            Self::UnsupportedMediaType => "unsupported_media_type",
            Self::MethodNotAllowed => "method_not_allowed",
            Self::NotFound => "not_found",
            Self::InternalError => "internal_error",
        }
    }
}

/// Structured tool or protocol error.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolError {
    /// Stable error code.
    pub code: ErrorCode,
    /// Human-readable, non-sensitive explanation.
    pub message: String,
    /// Optional machine-readable context.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}

impl ToolError {
    /// Create an error without details.
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            details: None,
        }
    }

    /// Attach structured details.
    pub fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }
}

/// Validated request envelope.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCallRequest {
    /// Must equal [`PROTOCOL_VERSION`].
    pub protocol_version: u32,
    /// Client-generated correlation identifier.
    pub request_id: String,
    /// Registered tool name.
    pub tool: String,
    /// Tool-specific arguments. Missing arguments are treated as `{}`.
    #[serde(default = "empty_object")]
    pub arguments: Value,
}

/// Success status for a processed call.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResponseStatus {
    /// Tool executed successfully.
    Ok,
    /// Tool or protocol error.
    Error,
}

/// Versioned result payload. `data` is defined by each tool's output schema.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolResult {
    /// Output schema version for this tool.
    pub schema_version: u32,
    /// Tool-specific data.
    pub data: Value,
}

impl ToolResult {
    /// Wrap tool data with its output schema version.
    pub fn new(schema_version: u32, data: Value) -> Self {
        Self {
            schema_version,
            data,
        }
    }
}

/// Response envelope. Exactly one of `result` or `error` is set.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCallResponse {
    /// Protocol version used by the service.
    pub protocol_version: u32,
    /// Echo of the request ID, or `null` if the request could not be parsed.
    pub request_id: Option<String>,
    /// Whether the call succeeded.
    pub status: ResponseStatus,
    /// Successful, versioned result.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<ToolResult>,
    /// Structured error for failed calls.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ToolError>,
}

impl ToolCallResponse {
    /// Successful response.
    pub fn success(request_id: String, result: ToolResult) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id: Some(request_id),
            status: ResponseStatus::Ok,
            result: Some(result),
            error: None,
        }
    }

    /// Failed response, preserving the request ID when it was valid.
    pub fn failure(request_id: Option<String>, error: ToolError) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id,
            status: ResponseStatus::Error,
            result: None,
            error: Some(error),
        }
    }
}

/// Discovery metadata for one registered tool.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolDescriptor {
    /// Stable tool name.
    pub name: String,
    /// Purpose of the tool.
    pub description: String,
    /// JSON Schema for the tool arguments.
    pub input_schema: Value,
    /// Version of the input schema.
    pub input_schema_version: u32,
    /// Version of the result `data` payload.
    pub output_schema_version: u32,
}

/// Response for `GET /v1/tools`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolDiscoveryResponse {
    /// Protocol version used by the service.
    pub protocol_version: u32,
    /// Registered tools in stable order.
    pub tools: Vec<ToolDescriptor>,
    /// Optional discovery-level error.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ToolError>,
}

impl ToolDiscoveryResponse {
    /// Successful discovery response.
    pub fn success(tools: Vec<ToolDescriptor>) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            tools,
            error: None,
        }
    }
}

/// Validate a request identifier.
pub fn validate_request_id(request_id: &str) -> Result<(), ToolError> {
    if request_id.is_empty() {
        return Err(ToolError::new(
            ErrorCode::InvalidRequest,
            "request_id must not be empty",
        ));
    }
    if request_id.len() > MAX_REQUEST_ID_LENGTH {
        return Err(ToolError::new(
            ErrorCode::InvalidRequest,
            format!("request_id must be at most {MAX_REQUEST_ID_LENGTH} bytes"),
        ));
    }
    if !request_id.bytes().all(|byte| (0x21..=0x7e).contains(&byte)) {
        return Err(ToolError::new(
            ErrorCode::InvalidRequest,
            "request_id must contain only printable ASCII without spaces",
        ));
    }
    Ok(())
}

fn empty_object() -> Value {
    Value::Object(serde_json::Map::new())
}

/// Shared JSON schema fragment for an optional bounded limit.
pub fn limit_schema(default: u64, maximum: u64) -> Value {
    json!({
        "type": "integer",
        "minimum": 1,
        "maximum": maximum,
        "default": default
    })
}

#[cfg(test)]
mod tests {
    use super::{
        ErrorCode, PROTOCOL_VERSION, ResponseStatus, ToolCallRequest, ToolCallResponse, ToolError,
        ToolResult, validate_request_id,
    };
    use serde_json::json;

    #[test]
    fn request_deserializes_arguments_and_rejects_unknown_fields() {
        let request: ToolCallRequest = serde_json::from_value(json!({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "req-1",
            "tool": "get_system_metrics",
            "arguments": {}
        }))
        .unwrap();
        assert_eq!(request.tool, "get_system_metrics");

        let missing_arguments: ToolCallRequest = serde_json::from_value(json!({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "req-1",
            "tool": "get_system_metrics"
        }))
        .unwrap();
        assert_eq!(missing_arguments.arguments, json!({}));

        let unknown = serde_json::from_value::<ToolCallRequest>(json!({
            "protocol_version": PROTOCOL_VERSION,
            "request_id": "req-1",
            "tool": "get_system_metrics",
            "arguments": {},
            "extra": true
        }));
        assert!(unknown.is_err());
    }

    #[test]
    fn success_and_failure_envelopes_serialize_without_the_other_field() {
        let success =
            ToolCallResponse::success("req-1".into(), ToolResult::new(1, json!({"value": 42})));
        let value = serde_json::to_value(&success).unwrap();
        assert_eq!(value["status"], "ok");
        assert_eq!(value["result"]["schema_version"], 1);
        assert!(value.get("error").is_none());

        let failure = ToolCallResponse::failure(
            Some("req-1".into()),
            ToolError::new(ErrorCode::InvalidArguments, "bad limit"),
        );
        let value = serde_json::to_value(&failure).unwrap();
        assert_eq!(value["status"], "error");
        assert_eq!(value["error"]["code"], "invalid_arguments");
        assert!(value.get("result").is_none());
    }

    #[test]
    fn response_status_is_stable() {
        assert_eq!(
            serde_json::to_value(ResponseStatus::Ok).unwrap(),
            json!("ok")
        );
    }

    #[test]
    fn request_id_validation_is_bounded() {
        assert!(validate_request_id("req-123").is_ok());
        assert!(validate_request_id("").is_err());
        assert!(validate_request_id("has space").is_err());
        assert!(validate_request_id(&"x".repeat(129)).is_err());
    }

    #[test]
    fn error_codes_are_stable_strings() {
        assert_eq!(ErrorCode::UnknownTool.as_str(), "unknown_tool");
        assert_eq!(
            ErrorCode::ProcessUnavailable.as_str(),
            "process_unavailable"
        );
    }
}
