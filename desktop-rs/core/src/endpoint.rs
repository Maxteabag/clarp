//! Host endpoint rules shared by the API and SSE clients: base URL
//! normalisation, path resolution, the same-origin test that guards
//! authenticated media, reconnect backoff, and error-body messages.
//! Ports the pure parts of the C++ client's `ApiClient` and `SseClient`.

use std::time::Duration;

use serde_json::Value;
use url::Url;

use crate::json::Object;
use crate::sse::SseMessage;

/// A base URL always ends in `/` and carries no query or fragment, so that
/// relative paths resolve beneath it.
pub fn normalize_base(mut base: Url) -> Url {
    if !base.path().ends_with('/') {
        let path = format!("{}/", base.path());
        base.set_path(&path);
    }
    base.set_query(None);
    base.set_fragment(None);
    base
}

/// Resolve an API path beneath the base; leading slashes do not escape it.
/// An absolute URL resolves to itself (callers then apply origin checks).
pub fn resolve(base: &Url, path: &str) -> Option<Url> {
    base.join(path.trim_start_matches('/')).ok()
}

/// Scheme, host and effective port all match, and no credentials are
/// embedded — the only case where a bearer token may ride along to media.
pub fn same_origin(url: &Url, base: &Url) -> bool {
    url.username().is_empty()
        && url.password().is_none()
        && url.scheme().eq_ignore_ascii_case(base.scheme())
        && url.host_str().map(str::to_ascii_lowercase) == base.host_str().map(str::to_ascii_lowercase)
        && url.port_or_known_default() == base.port_or_known_default()
}

/// Redirect safety for ordinary API calls (Qt's NoLessSafeRedirectPolicy):
/// never downgrade from https to http.
pub fn redirect_is_no_less_safe(from: &Url, to: &Url) -> bool {
    !(from.scheme() == "https" && to.scheme() != "https")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SseTiming {
    /// Abort a stream that has been silent this long; the Host pings sooner.
    pub watchdog: Duration,
    pub initial_reconnect: Duration,
    pub maximum_reconnect: Duration,
}

impl Default for SseTiming {
    fn default() -> Self {
        Self {
            watchdog: Duration::from_millis(25_000),
            initial_reconnect: Duration::from_millis(250),
            maximum_reconnect: Duration::from_millis(5_000),
        }
    }
}

impl SseTiming {
    /// Exponential backoff: initial × 2^min(attempt, 5), capped at maximum.
    pub fn reconnect_delay(&self, attempt: u32) -> Duration {
        (self.initial_reconnect * (1u32 << attempt.min(5))).min(self.maximum_reconnect)
    }
}

/// The resume cursor for one Host. Event IDs are scoped to the Host that
/// minted them: reusing another Host's cursor could skip its initial state.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SseCursor {
    base: Option<Url>,
    last_event_id: String,
}

impl SseCursor {
    pub fn set_endpoint(&mut self, base: &Url) {
        if self.base.as_ref().is_some_and(|current| current != base) {
            self.last_event_id.clear();
        }
        self.base = Some(base.clone());
    }

    pub fn last_event_id(&self) -> &str {
        &self.last_event_id
    }

    pub fn set_last_event_id(&mut self, id: &str) {
        self.last_event_id = id.to_owned();
    }

    /// Record a delivered message and return the event the app sees: the
    /// data object plus `event_id` (numeric when the id is an integer).
    pub fn accept(&mut self, message: SseMessage) -> Object {
        let mut event = message.data;
        if !message.id.is_empty() {
            let id = message
                .id
                .parse::<i64>()
                .map_or_else(|_| Value::from(message.id.clone()), Value::from);
            event.insert("event_id".into(), id);
            self.last_event_id = message.id;
        }
        event
    }
}

/// The user-facing message for a failed request: the Host's `message`,
/// `detail` or `error` field when the body is a JSON object, else fallback.
pub fn error_message(body: &[u8], fallback: &str) -> String {
    let Ok(Value::Object(object)) = serde_json::from_slice::<Value>(body) else {
        return fallback.to_owned();
    };
    ["message", "detail", "error"]
        .iter()
        .filter_map(|key| object.get(*key).and_then(Value::as_str))
        .find(|text| !text.is_empty())
        .map_or_else(|| fallback.to_owned(), str::to_owned)
}

/// QUrl::toPercentEncoding: everything but RFC 3986 unreserved characters.
pub fn percent_encode_segment(text: &str) -> String {
    text.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}
