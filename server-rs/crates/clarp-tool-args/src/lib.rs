//! Keep the standalone shim independent of Host and speech-engine dependencies.
pub mod opencode;

/// A native session identifier that can be used as a single path component.
pub fn native_session_name(name: &str) -> bool {
    !name.is_empty() && !name.contains(['/', '\\']) && name != "." && name != ".."
}
