//! Keep the standalone shim independent of Host and speech-engine dependencies.
pub mod opencode;

/// A native session identifier that can be used as a single path component.
pub fn native_session_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    bytes.len() <= 128
        && bytes.first().is_some_and(u8::is_ascii_alphanumeric)
        && bytes.iter().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}
