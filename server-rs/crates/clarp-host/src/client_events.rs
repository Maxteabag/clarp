//! Admission and redaction for the always-kept client log.
use std::{
    borrow::Cow,
    path::PathBuf,
    sync::{Arc, LazyLock},
    time::Duration,
};

use axum::{
    Extension, Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
};
use clarp_ports::clock::Clock;
use clarp_store_sqlite::client_events::{CrashCandidate, Event, Flash, Query as Filters, Store};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App, Principal};

const DETAIL_BYTES: usize = 4096;
/// The event the apps' scroll dashcam sends when a chat view jumps and comes back without a
/// gesture: kept like an important one, with room for its frames and causes.
pub(crate) const SCROLL_FLASH: &str = "scroll_flash";
const FLASH_DETAIL_BYTES: usize = 16 * 1024;
const REDACTED: &str = "[redacted]";
const MARKER: &str = "…[truncated]";

fn pattern(text: &str) -> Regex {
    Regex::new(text).unwrap_or_else(|why| panic!("client event redaction pattern: {why}"))
}

static IMPORTANT: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"(?i)error|crash|exception|fatal|hang|fault|metrickit"));
static SECRET_KEY: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r"(?i)token|secret|password|passwd|authorization|cookie|api[ _-]?key|credential")
});
static SECRETS: LazyLock<[(Regex, &str); 4]> = LazyLock::new(|| {
    [
        (
            pattern(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]+"),
            "$1 [redacted]",
        ),
        (
            pattern(r"(?i)\b(cld|clp)_[A-Za-z0-9_\-]+"),
            "${1}_[redacted]",
        ),
        (
            pattern(r#"(?i)\b((?:access_|auth_|api_)?token|secret|password|key)=[^&\s"']+"#),
            "$1=[redacted]",
        ),
        (
            pattern(r#"(?i)(authorization"?\s*[:=]\s*"?)[^",}\s]+"#),
            "$1[redacted]",
        ),
    ]
});

fn redact_text(text: &str) -> String {
    let mut text = std::borrow::Cow::Borrowed(text);
    for (re, replacement) in SECRETS.iter() {
        if let std::borrow::Cow::Owned(replaced) = re.replace_all(&text, *replacement) {
            text = std::borrow::Cow::Owned(replaced);
        }
    }
    text.into_owned()
}

fn redact(value: &Value, depth: usize, bounded: bool) -> Value {
    if bounded && depth > 12 {
        return json!("[deep]");
    }
    let most = if bounded { 200 } else { usize::MAX };
    match value {
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .take(most)
                .map(|(key, value)| {
                    let value = if SECRET_KEY.is_match(key) {
                        json!(REDACTED)
                    } else {
                        redact(value, depth + 1, bounded)
                    };
                    let key = redact_text(key);
                    let key = if bounded {
                        clarp_kernel::python::first(&key, 80).to_owned()
                    } else {
                        key
                    };
                    (key, value)
                })
                .collect::<Map<_, _>>(),
        ),
        Value::Array(rows) => Value::Array(
            rows.iter()
                .take(most)
                .map(|row| redact(row, depth + 1, bounded))
                .collect(),
        ),
        Value::String(text) => json!(redact_text(text)),
        value => value.clone(),
    }
}

/// Preserve a MetricKit report's shape while removing secrets before writing it.
pub(crate) fn redact_report(report: &Value) -> Value {
    redact(report, 0, false)
}

fn detail(value: &Value, most: usize) -> Option<String> {
    if value.is_null() || value == "" {
        return None;
    }
    let mut text = redact(value, 0, true).to_string();
    if text.len() > most {
        text.truncate(text.floor_char_boundary(most - MARKER.len()));
        text.push_str(MARKER);
    }
    Some(text)
}

fn text(value: &Value, field: &str, fallback: &str) -> String {
    let value = &value[field];
    if clarp_kernel::python::truthy(value) {
        value
            .as_str()
            .map_or_else(|| clarp_kernel::python::repr(value), str::to_owned)
    } else {
        fallback.to_owned()
    }
}

/// Telemetry also accepts the legacy semicolon separator. Capability
/// negotiation still uses its own strict header validation.
fn telemetry_header(header: &str) -> Cow<'_, str> {
    if let Some((client, contract)) = header.split_once(';') {
        Cow::Owned(format!("{} {}", client.trim_end(), contract.trim_start()))
    } else {
        Cow::Borrowed(header)
    }
}

pub(crate) struct TelemetryClient {
    pub platform: String,
    pub build: String,
    pub contract: i64,
}

pub(crate) fn telemetry_client(headers: &HeaderMap) -> Result<Option<TelemetryClient>, ()> {
    let Some(value) = headers.get("x-clarp-client") else {
        return Ok(None);
    };
    let header = telemetry_header(value.to_str().map_err(|_| ())?);
    let (platform, contract) = clarp_kernel::client::named(&header).ok_or(())?;
    let (_, tail) = header.split_once('/').ok_or(())?;
    let (build, _) = tail.split_once(' ').ok_or(())?;
    Ok(Some(TelemetryClient {
        platform: platform.to_ascii_lowercase(),
        build: build.to_owned(),
        contract,
    }))
}

struct Source {
    device: String,
    client: String,
    build: String,
    contract: Option<i64>,
    server_id: String,
}

impl Source {
    fn from(app: &App, principal: &Principal, headers: &HeaderMap) -> Self {
        let label = match principal {
            Principal::Administrator if app.config.auth_token.is_empty() => "local",
            Principal::Administrator => "administrator",
            Principal::Device { device_id, .. } => device_id,
            Principal::Tools => "unknown",
        };
        let client = match principal {
            Principal::Device { device_id, .. } => {
                app.experience.client_metadata(device_id, headers)
            }
            _ => telemetry_client(headers),
        };
        let (platform, build, contract) = match client {
            Ok(Some(client)) => (client.platform, client.build, Some(client.contract)),
            Ok(None) => (String::new(), String::new(), None),
            Err(()) => ("unknown".to_owned(), String::new(), None),
        };
        let cap =
            |text: &str, most| clarp_kernel::python::first(&redact_text(text), most).to_owned();
        Self {
            device: cap(label, 120),
            client: cap(&platform, 32),
            build: cap(&build, 80),
            contract,
            server_id: cap(&app.server_id, 64),
        }
    }

    fn event(&self, item: &Value, now: i64) -> Event {
        let cap =
            |text: &str, most| clarp_kernel::python::first(&redact_text(text), most).to_owned();
        let name = text(item, "event", "log");
        let name = clarp_kernel::python::first(&name, 64);
        let level = text(item, "level", "");
        let level = level.trim().to_lowercase();
        let level = clarp_kernel::python::first(&level, 16);
        let flash = name == SCROLL_FLASH;
        let important = flash || IMPORTANT.is_match(name) || IMPORTANT.is_match(level);
        let level = if !level.is_empty() {
            level
        } else if important {
            "error"
        } else {
            "info"
        };
        let trace = cap(&text(item, "trace_id", ""), 80);
        Event {
            received_at: now,
            device: self.device.clone(),
            client: self.client.clone(),
            client_build: self.build.clone(),
            client_contract: self.contract,
            server_id: self.server_id.clone(),
            event: cap(name, 64),
            level: cap(level, 16),
            detail: detail(
                &item["detail"],
                if flash {
                    FLASH_DETAIL_BYTES
                } else {
                    DETAIL_BYTES
                },
            ),
            trace_id: (!trace.is_empty()).then_some(trace),
            kept: important,
        }
    }
}

pub struct Desk {
    path: PathBuf,
    connection: Arc<tokio::sync::Mutex<Option<Store>>>,
}

impl Desk {
    pub fn new(path: PathBuf) -> Arc<Self> {
        Arc::new(Self {
            path,
            connection: Arc::default(),
        })
    }

    async fn call<T: Send + 'static>(
        self: &Arc<Self>,
        operation: impl FnOnce(&mut Store) -> Result<T, String> + Send + 'static,
    ) -> Result<T, String> {
        let mut connection = self.connection.clone().lock_owned().await;
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            if connection.is_none() {
                *connection = Some(Store::open(&path)?);
            }
            let store = connection
                .as_mut()
                .ok_or("client event store unavailable")?;
            operation(store)
        })
        .await
        .map_err(|error| error.to_string())?
    }

    pub fn age_out(self: &Arc<Self>, clock: Arc<dyn Clock + Send + Sync>) {
        let desk = Arc::downgrade(self);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(30)).await;
                let Some(desk) = desk.upgrade() else { return };
                if desk.path.is_file() {
                    let now = clock.now_ms();
                    if let Err(why) = desk
                        .call(move |store| store.prune(now).map_err(|e| e.to_string()))
                        .await
                    {
                        eprintln!("clientEventsPruneFail {why}");
                    }
                }
            }
        });
    }
}

pub(crate) async fn crash_candidates(
    app: &App,
    since: i64,
    now: i64,
) -> Result<Vec<CrashCandidate>, String> {
    app.client_events
        .call(move |store| {
            store
                .crash_candidates(since, now)
                .map_err(|why| why.to_string())
        })
        .await
}

/// What one device reported of chat-view scroll flashes in a window.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Flashes {
    pub count: u64,
    pub first_at: i64,
    pub newest_at: i64,
    /// The newest flash's rule (`jump_back`, `top_flip`).
    pub rule: String,
    /// The kinds of what happened around the newest flash (`reload`, `insert`, ...), each once.
    pub causes: Vec<String>,
}

pub(crate) type ByDevice = std::collections::BTreeMap<(String, String), Flashes>;

/// The scroll flashes of a window, by device and the platform (client) that sent them.
pub(crate) fn flashes_by_device(rows: &[Flash]) -> ByDevice {
    let mut by = ByDevice::new();
    for row in rows {
        let flashes = by
            .entry((row.device.clone(), row.client.clone()))
            .or_default();
        if flashes.count == 0 || row.received_at < flashes.first_at {
            flashes.first_at = row.received_at;
        }
        flashes.count += 1;
        if row.received_at >= flashes.newest_at {
            flashes.newest_at = row.received_at;
            let detail = row
                .detail
                .as_deref()
                .and_then(|detail| serde_json::from_str::<Value>(detail).ok())
                .unwrap_or(Value::Null);
            flashes.rule = detail["rule"].as_str().unwrap_or("unknown").to_owned();
            let mut causes: Vec<String> = Vec::new();
            for event in detail["events"].as_array().into_iter().flatten() {
                if let Some(kind) = event["kind"].as_str()
                    && !causes.iter().any(|seen| seen == kind)
                {
                    causes.push(kind.to_owned());
                }
            }
            flashes.causes = causes;
        }
    }
    by
}

/// The scroll flashes apps reported in the window, by device and platform.
pub(crate) async fn flashes(app: &App, since: i64, now: i64) -> Result<ByDevice, String> {
    let rows = app
        .client_events
        .call(move |store| store.flashes(since, now).map_err(|why| why.to_string()))
        .await?;
    Ok(flashes_by_device(&rows))
}

pub(crate) async fn record(
    app: &App,
    principal: &Principal,
    headers: &HeaderMap,
    items: &[Value],
) -> Result<usize, String> {
    let source = Source::from(app, principal, headers);
    let now = app.clock.now_ms();
    let rows: Vec<_> = items
        .iter()
        .filter(|item| item.is_object())
        .map(|item| source.event(item, now))
        .collect();
    if rows.is_empty() {
        return Ok(0);
    }
    app.client_events
        .call(move |store| {
            store
                .record(&rows, &source.device, now)
                .map_err(|e| e.to_string())
        })
        .await
}

fn parse_since(since: &str, now: i64) -> Result<Option<i64>, ApiError> {
    if since.is_empty() {
        return Ok(None);
    }
    if since.bytes().all(|byte| byte.is_ascii_digit()) {
        return Ok(Some(since.parse().unwrap_or(i64::MAX)));
    }
    for (unit, ms) in [
        ('s', 1000),
        ('m', 60_000),
        ('h', 3_600_000),
        ('d', 86_400_000),
    ] {
        if let Some(amount) = since.strip_suffix(unit) {
            let amount = amount.trim_end();
            if !amount.is_empty() && amount.bytes().all(|byte| byte.is_ascii_digit()) {
                let amount = amount.parse::<i64>().unwrap_or(i64::MAX);
                return Ok(Some(now.saturating_sub(amount.saturating_mul(ms))));
            }
        }
    }
    Err(ApiError::new(
        StatusCode::BAD_REQUEST,
        "since is 30m, 2h, 7d or milliseconds since the epoch",
    ))
}

fn shown(row: Event) -> Value {
    let detail = row.detail.map(|detail| {
        if detail.ends_with(MARKER) {
            Value::String(detail)
        } else {
            serde_json::from_str(&detail).unwrap_or(Value::String(detail))
        }
    });
    json!({
        "received_at": row.received_at, "device": row.device, "client": row.client,
        "client_build": row.client_build, "client_contract": row.client_contract,
        "server_id": row.server_id, "event": row.event, "level": row.level,
        "detail": detail, "trace_id": row.trace_id, "kept": row.kept,
    })
}

pub async fn read(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(query): Query<Vec<(String, String)>>,
) -> Result<Json<Value>, ApiError> {
    if matches!(
        principal,
        Principal::Device {
            scope: crate::pairing::Scope::Limited,
            ..
        }
    ) {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            clarp_kernel::devices::LIMITED_REFUSAL,
        ));
    }
    let events = events(app, |name| {
        query
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    })
    .await?;
    Ok(Json(json!({"count":events.len(),"events":events})))
}

pub(crate) async fn events<'a>(
    app: App,
    value: impl Fn(&str) -> Option<&'a str>,
) -> Result<Vec<Value>, ApiError> {
    let value = |name| value(name).map(str::trim).filter(|value| !value.is_empty());
    let limit = value("limit").unwrap_or("200");
    let limit = limit
        .parse::<i64>()
        .map_err(|_| {
            ApiError::new(
                StatusCode::BAD_REQUEST,
                format!(
                    "invalid literal for int() with base 10: {}",
                    clarp_kernel::python::repr(&json!(limit))
                ),
            )
        })?
        .clamp(1, 5000);
    let now = app.clock.now_ms();
    let filters = Filters {
        device: value("device").map(str::to_owned),
        since_ms: parse_since(value("since").unwrap_or_default(), now)?,
        level: value("level").map(str::to_lowercase),
        event: value("event").map(str::to_owned),
        limit,
    };
    let rows = app
        .client_events
        .call(move |store| store.read(&filters, now).map_err(|e| e.to_string()))
        .await
        .map_err(|why| {
            eprintln!("clientEventsReadFail {why}");
            ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "store failed")
        })?;
    Ok(rows.into_iter().map(shown).collect())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn source() -> Source {
        Source {
            device: "phone".into(),
            client: "ios".into(),
            build: "500".into(),
            contract: Some(80),
            server_id: "host".into(),
        }
    }

    fn flash(frames: usize) -> Value {
        json!({
            "event": SCROLL_FLASH, "level": "warning",
            "detail": {
                "chat": "transit-probe", "window": "main", "screen": 812, "rule": "jump_back",
                "frames": (0..frames).map(|t| json!({"t": t, "y": t * 7, "h": 9000, "top": format!("message-{t}-{}", "0".repeat(48)), "g": false})).collect::<Vec<_>>(),
                "events": [{"t": 3, "kind": "reload", "note": "snapshot"}, {"t": 5, "kind": "height", "note": "image"}],
            },
        })
    }

    #[test]
    fn a_scroll_flash_is_kept_like_an_important_event_with_room_for_its_frames() {
        let event = source().event(&flash(60), 1);
        assert!(
            event.kept,
            "IMPORTANT does not match the name, the flash is kept all the same"
        );
        assert_eq!(event.level, "warning");
        let detail = event.detail.unwrap();
        assert!(
            detail.len() > DETAIL_BYTES && detail.len() <= FLASH_DETAIL_BYTES,
            "{}",
            detail.len()
        );
        assert!(!detail.ends_with(MARKER));
        // Another event of the same size is cut at the usual bound.
        let mut other = flash(60);
        other["event"] = json!("scroll_note");
        let other = source().event(&other, 1);
        assert!(!other.kept);
        assert!(other.detail.unwrap().ends_with(MARKER));
    }

    #[test]
    fn flashes_are_counted_by_device_with_the_newest_ones_rule_and_causes() {
        let row = |client: &str, at, rule: &str, kinds: &[&str]| {
            Flash {
            device: format!("{client}-phone"),
            client: client.into(),
            received_at: at,
            detail: Some(
                json!({"rule": rule, "events": kinds.iter().map(|k| json!({"kind": k})).collect::<Vec<_>>()})
                    .to_string(),
            ),
        }
        };
        let by = flashes_by_device(&[
            row("ios", 10, "top_flip", &["insert"]),
            row("desktop", 11, "jump_back", &["live"]),
            row("ios", 20, "jump_back", &["reload", "height", "reload"]),
        ]);
        let ios = &by[&("ios-phone".to_owned(), "ios".to_owned())];
        assert_eq!(
            (ios.count, ios.first_at, ios.newest_at, ios.rule.as_str()),
            (2, 10, 20, "jump_back")
        );
        assert_eq!(ios.causes, ["reload", "height"]);
        assert_eq!(
            by[&("desktop-phone".to_owned(), "desktop".to_owned())].count,
            1
        );
    }
}
