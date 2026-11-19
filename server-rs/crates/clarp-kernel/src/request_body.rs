//! Request body admission from the Python Host's request_body.refused.
//! The tools media alias and conversation import are native Host routes.
pub const JSON_LIMIT: usize = 16 * 1024 * 1024;
const PAIRING_LIMIT: usize = 64 * 1024;
const IMPORT_LIMIT: usize = 512 * 1024 * 1024;
const LARGE: u64 = 2 * 1024 * 1024;
const DRAINED_MOST: u64 = 1024 * 1024;
pub const TOO_LARGE: &str = "request body too large";

pub fn limit(method: &str, path: &str) -> Option<usize> {
    match (method, path) {
        ("POST", "/media" | "/tools/media" | "/upload" | "/transcribe") => None,
        ("POST", "/pairing/exchange") => Some(PAIRING_LIMIT),
        ("POST", "/agents/import") => Some(IMPORT_LIMIT),
        ("POST" | "PUT" | "PATCH" | "DELETE", _) => Some(JSON_LIMIT),
        _ => None,
    }
}

pub fn family(path: &str) -> String {
    format!(
        "/{}",
        path.trim_start_matches('/').split('/').next().unwrap_or("")
    )
}

#[derive(Debug, PartialEq)]
pub enum Admission {
    Accept,
    LogLarge,
    Refuse { close: bool },
}

pub fn admission(length: u64, limit: usize) -> Admission {
    if length > limit as u64 {
        Admission::Refuse {
            close: length > DRAINED_MOST,
        }
    } else if length > LARGE {
        Admission::LogLarge
    } else {
        Admission::Accept
    }
}
