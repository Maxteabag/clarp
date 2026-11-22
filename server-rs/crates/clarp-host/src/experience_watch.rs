//! The Host's experience watcher and its HTTP-only signals.

mod book;
mod checks;
mod release;
mod settings;

pub(crate) use checks::AgentFacts;

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Mutex;

use crate::client_events::{self, TelemetryClient};
use crate::fleet::Directory;
use crate::kept::{Found, Key, Kind};
use crate::tools::ToolAnswer;
use book::{Book, Row};
use jiff::{Zoned, tz::TimeZone};
use settings::Settings;

use axum::http::{HeaderMap, HeaderValue};
use clarp_protocol::requests::SendRequest;
use clarp_protocol::responses::SendResponse;
use serde_json::json;

use crate::coordination::Made;
use crate::edge::{self, ApiError, App};

const SENDER: &str = "experience-watch";
const MAX_EVENTS: usize = 32_768;
const KEEP_MS: i64 = 24 * 60 * 60 * 1_000;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Relaunches {
    pub foreground_sessions: usize,
    pub legacy_requests: usize,
    pub platform: String,
    pub build: String,
    pub contract: Option<i64>,
}

struct Event<T> {
    at: i64,
    subject: String,
    detail: T,
}

#[derive(Default)]
struct Launches {
    foreground: BTreeSet<String>,
    events: VecDeque<Event<Relaunches>>,
}

#[derive(Default)]
pub struct Signals {
    launches: Mutex<Launches>,
    refusals: Mutex<VecDeque<Event<()>>>,
    malformed_clients: Mutex<BTreeSet<String>>,
}

fn append<T>(ring: &mut VecDeque<Event<T>>, at: i64, subject: &str, detail: T) {
    while ring
        .front()
        .is_some_and(|first| first.at < at.saturating_sub(KEEP_MS) || ring.len() >= MAX_EVENTS)
    {
        ring.pop_front();
    }
    ring.push_back(Event {
        at,
        subject: subject.to_owned(),
        detail,
    });
}

impl Signals {
    /// A marked foreground observation starts a session once per device until a
    /// request reports background. Unmarked startup requests are weaker evidence.
    pub(crate) fn device_request(
        &self,
        device_id: &str,
        headers: &HeaderMap,
        startup_endpoint: bool,
        now: i64,
    ) {
        let state = headers.get("x-clarp-app-state").map(HeaderValue::as_bytes);
        if state.is_none() && !startup_endpoint {
            return;
        }
        let client = self.client_metadata(device_id, headers);
        let Ok(mut launches) = self.launches.lock() else {
            return;
        };
        let foreground_session = match state {
            Some(b"foreground") => {
                if !launches.foreground.insert(device_id.to_owned()) {
                    return;
                }
                true
            }
            Some(b"background") => {
                launches.foreground.remove(device_id);
                return;
            }
            None => false,
            Some(_) => return,
        };
        let mut detail = match client {
            Ok(Some(client)) => Relaunches {
                platform: if checks::platform(&client.platform) {
                    client.platform
                } else {
                    "unknown".to_owned()
                },
                build: client.build,
                contract: Some(client.contract),
                ..Relaunches::default()
            },
            Ok(None) => Relaunches::default(),
            Err(()) => Relaunches {
                platform: "unknown".to_owned(),
                ..Relaunches::default()
            },
        };
        detail.foreground_sessions = usize::from(foreground_session);
        detail.legacy_requests = usize::from(!foreground_session);
        append(&mut launches.events, now, device_id, detail);
    }

    pub(crate) fn client_metadata(
        &self,
        device_id: &str,
        headers: &HeaderMap,
    ) -> Result<Option<TelemetryClient>, ()> {
        let client = client_events::telemetry_client(headers);
        if client.is_err()
            && let Ok(mut devices) = self.malformed_clients.lock()
            && devices.insert(device_id.to_owned())
        {
            eprintln!("clarp-host: experience-watch: device {device_id:?} sent a malformed X-Clarp-Client header; platform unknown");
        }
        client
    }

    pub(crate) fn refused(&self, agent_id: &str, now: i64) {
        if !agent_id.is_empty()
            && let Ok(mut ring) = self.refusals.lock()
        {
            append(&mut ring, now, agent_id, ());
        }
    }

    pub fn relaunches(&self, since: i64, now: i64) -> BTreeMap<String, Relaunches> {
        let mut result: BTreeMap<String, Relaunches> = BTreeMap::new();
        let Ok(launches) = self.launches.lock() else {
            return result;
        };
        for event in launches
            .events
            .iter()
            .filter(|event| in_window(event.at, since, now))
        {
            let row = result.entry(event.subject.clone()).or_insert_with(|| Relaunches {
                platform: "unknown".to_owned(),
                ..Relaunches::default()
            });
            row.foreground_sessions += event.detail.foreground_sessions;
            row.legacy_requests += event.detail.legacy_requests;
            if !event.detail.platform.is_empty() {
                row.platform.clone_from(&event.detail.platform);
                row.build.clone_from(&event.detail.build);
                row.contract = event.detail.contract;
            }
        }
        result
    }

    pub fn refusals(&self, since: i64, now: i64) -> BTreeMap<String, usize> {
        let mut result = BTreeMap::new();
        let Ok(events) = self.refusals.lock() else {
            return result;
        };
        for event in events
            .iter()
            .filter(|event| in_window(event.at, since, now))
        {
            *result.entry(event.subject.clone()).or_default() += 1;
        }
        result
    }
}

fn in_window(at: i64, since: i64, now: i64) -> bool {
    (since.max(now.saturating_sub(KEEP_MS))..=now).contains(&at)
}

/// The watcher is a Host-owned sender, with no provider turn or roster identity.
pub async fn tell(
    app: &App,
    session: &str,
    message_id: String,
    text: String,
) -> Result<SendResponse, ApiError> {
    let request = SendRequest {
        session: session.to_owned(),
        text,
        client_msg_id: Some(message_id),
        force_session: Some(true),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        origin: Some("agent".into()),
        sender: Some(json!(SENDER)),
        ..SendRequest::default()
    };
    let made = Made {
        // The Host itself authorizes this fixed sender.
        internal_sender: Some(SENDER),
        ..Made::default()
    };
    edge::send_from(app, request, &made).await
}

const KIND: &str = "experience-watch";
const BOOK: &str = "findings";

pub(crate) const KEPT: &[Kind] = &[Kind::book(KIND, Key::Plain, fits).when_read()];

fn fits(data: &[u8]) -> bool {
    serde_json::from_slice::<Book>(data).is_ok()
}

async fn load(app: &App) -> Result<Book, String> {
    match app
        .fleet
        .judged(KIND, BOOK.into())
        .await
        .map_err(|why| why.0)?
    {
        Found::Absent => Ok(Book::default()),
        Found::Read(data) => serde_json::from_slice(&data).map_err(|why| why.to_string()),
        Found::Unfit => Err("the findings book cannot be read".into()),
    }
}

async fn save(app: &App, book: &Book) -> Result<(), String> {
    let bytes = serde_json::to_vec(book).map_err(|why| why.to_string())?;
    app.fleet
        .keep(KIND, BOOK.into(), Some(bytes))
        .await
        .map_err(|why| why.0)
}

async fn pass(app: &App, settings: &Settings, previous: i64, now: i64) -> Result<(), String> {
    let mut book = load(app).await?;
    let seen = checks::collect(app, settings, settings.window_start(now, previous), now).await;
    let directory = app.fleet.directory();
    let zone = TimeZone::system();
    book.reconcile(seen, settings, &directory, &zone, now);
    // Record pending notices before sending, with stable IDs across a restart.
    save(app, &book).await?;
    if book.owed.is_empty() {
        return Ok(());
    }
    for owed in std::mem::take(&mut book.owed) {
        if let book::Notice::Summary(day) = &owed.notice
            && book::summary_day(settings, &zone, app.clock.now_ms()).as_ref() != Some(day)
        {
            continue;
        }
        let id = owed.message_id();
        match tell(app, &owed.to, id, owed.text.clone()).await {
            Ok(_) => book.told(owed),
            Err(why) => {
                eprintln!(
                    "clarp-host: experience-watch could not tell {}: {}",
                    owed.to,
                    why.message(),
                );
                // A summary can be retried in its hour using its original text and id.
                if matches!(owed.notice, book::Notice::Summary(_)) {
                    book.owed.push(owed);
                }
            }
        }
    }
    save(app, &book).await
}

/// The first pass waits one interval. The next pass uses newly read owner settings.
pub fn run(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        let mut settings = Settings::read();
        let mut previous = app.clock.now_ms();
        loop {
            tokio::time::sleep(settings.interval).await;
            settings = Settings::read();
            let now = app.clock.now_ms();
            if settings.enabled
                && let Err(why) = pass(&app, &settings, previous, now).await
            {
                eprintln!("clarp-host: experience-watch pass: {why}");
                continue;
            }
            previous = now;
        }
    });
}

fn duration_ms(raw: &str) -> Option<i64> {
    let raw = raw.trim();
    let (value, scale) = match raw.as_bytes().last()?.to_ascii_lowercase() {
        b's' => (&raw[..raw.len() - 1], 1_000),
        b'm' => (&raw[..raw.len() - 1], 60_000),
        b'h' => (&raw[..raw.len() - 1], 3_600_000),
        b'd' => (&raw[..raw.len() - 1], 86_400_000),
        _ => (raw, 1_000),
    };
    let value: i64 = value.parse().ok()?;
    (value >= 0).then_some(value.saturating_mul(scale))
}

fn local(ms: i64, zone: &TimeZone) -> Option<Zoned> {
    let ts = jiff::Timestamp::from_millisecond(ms).ok()?;
    Some(ts.to_zoned(zone.clone()))
}

fn clock(ms: i64, zone: &TimeZone) -> String {
    local(ms, zone)
        .map(|time| time.strftime("%Y-%m-%d %H:%M %:z").to_string())
        .unwrap_or_default()
}

/// `clarp-admin experience [--since 1h] [--open] [--json]`.
pub(crate) async fn command(app: &App, args: &[String]) -> ToolAnswer {
    let parsed = match crate::artifact_tool::parse(args, &["--since"], &["--open", "--json"], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return ToolAnswer::said(2, why),
    };
    let since = if let Some(raw) = parsed.option("--since") {
        let Some(ms) = duration_ms(raw) else {
            return ToolAnswer::said(2, "--since: give a duration such as 30m, 1h or 2d");
        };
        app.clock.now_ms().saturating_sub(ms)
    } else {
        i64::MIN
    };
    let book = match load(app).await {
        Ok(book) => book,
        Err(why) => return ToolAnswer::said(1, format!("clarp-admin experience: {why}")),
    };
    let only_open = parsed.flags.contains("--open");
    let mut rows = book
        .rows
        .into_iter()
        .filter(|row| row.last_seen >= since && (!only_open || row.cleared_at.is_none()))
        .collect::<Vec<_>>();
    rows.sort_unstable_by_key(|row| std::cmp::Reverse((row.last_seen, row.id)));
    rows.truncate(500);
    render(
        rows,
        &Settings::read(),
        &app.fleet.directory(),
        &TimeZone::system(),
        parsed.flags.contains("--json"),
    )
}

fn render(
    mut rows: Vec<Row>,
    settings: &Settings,
    directory: &Directory,
    zone: &TimeZone,
    json_output: bool,
) -> ToolAnswer {
    for row in &mut rows {
        row.refresh_device_name(directory);
    }
    if json_output {
        #[derive(serde::Serialize)]
        struct LocalRow<'a> {
            #[serde(flatten)]
            row: &'a Row,
            first_seen_local: String,
            last_seen_local: String,
            cleared_at_local: Option<String>,
        }
        let shown: Vec<_> = rows
            .iter()
            .map(|row| LocalRow {
                row,
                first_seen_local: clock(row.first_seen, zone),
                last_seen_local: clock(row.last_seen, zone),
                cleared_at_local: row.cleared_at.map(|at| clock(at, zone)),
            })
            .collect();
        return match serde_json::to_string_pretty(&shown) {
            Ok(text) => ToolAnswer::printed(0, text),
            Err(why) => ToolAnswer::said(1, format!("clarp-admin experience: {why}")),
        };
    }
    let enabled = if settings.enabled { "on" } else { "off" };
    let mut lines = vec![format!(
        "experience watch: {enabled}, every {}s; {} finding(s)",
        settings.interval.as_secs(),
        rows.len(),
    )];
    for row in rows {
        let state = if row.cleared_at.is_none() {
            "OPEN"
        } else {
            "cleared"
        };
        let told = |to: &[String]| {
            if to.is_empty() {
                "-".to_owned()
            } else {
                to.join(", ")
            }
        };
        let cleared = row
            .cleared_at
            .map(|at| format!(", cleared {}", clock(at, zone)))
            .unwrap_or_default();
        lines.push(format!(
            "  {state:<7} {:<18} {:<28} since {}, last {}{cleared}, seen {}x, told on open {}, on clear {}\n          {}",
            row.kind, row.subject_label(), clock(row.first_seen, zone), clock(row.last_seen, zone),
            row.count, told(&row.notified_open), told(&row.notified_clear), row.detail,
        ));
    }
    ToolAnswer::printed(0, lines.join("\n"))
}
