//! Structured failures for the commands the webview calls.
//!
//! A command used to fail with a bare string, so the page could only guess at
//! what happened by searching that string for a keyword. The tag is a stable
//! code the page branches on; the detail is what a person reads. Both travel,
//! so an old page that only shows `message` behaves exactly as before.

use serde::Serialize;

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IpcError {
    /// Stable, machine-readable, and safe for the page to match on.
    pub code: String,
    /// Human-readable detail. Never a code, never a provider message.
    pub message: String,
}

impl IpcError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    /// A failure whose own text is already the right thing to show.
    pub fn with_message(code: impl Into<String>, message: impl std::fmt::Display) -> Self {
        Self::new(code, message.to_string())
    }
}

impl std::fmt::Display for IpcError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for IpcError {}

impl From<String> for IpcError {
    fn from(message: String) -> Self {
        Self::new("ipc_failure", message)
    }
}

impl From<&str> for IpcError {
    fn from(message: &str) -> Self {
        Self::new("ipc_failure", message)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serializes_both_fields_for_the_page() {
        let error = IpcError::new("update_busy", "Wenyi is busy.");
        let value = serde_json::to_value(&error).unwrap();
        assert_eq!(value["code"], "update_busy");
        assert_eq!(value["message"], "Wenyi is busy.");
    }

    #[test]
    fn display_is_the_message_not_the_code() {
        // Anything that prints the error keeps showing the sentence a person
        // can act on; the code only matters to the page.
        let error = IpcError::new("update_busy", "Wenyi is busy.");
        assert_eq!(error.to_string(), "Wenyi is busy.");
    }
}
