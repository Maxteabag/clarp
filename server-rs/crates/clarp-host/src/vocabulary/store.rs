//! The book and run records use the same kept-store port as other fleet data.
//! Audits have no expiry. Their keyed lookup records have bounded size.
use crate::fleet::FleetHandle;
use axum::http::StatusCode;
use clarp_kernel::vocabulary::book::{self, Book};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::sync::Arc;

/// What this desk keeps, declared once (`kept.rs`). Each kind is read by key alone (its runs and
/// their traces are audits with no expiry, and a start does not read a history): judged when
/// read, by the type this desk reads.
pub(crate) const KEPT: &[crate::kept::Kind] = &[
    crate::kept::Kind::book(BOOK, crate::kept::Key::Plain, crate::kept::fits::<Book>).when_read(),
    crate::kept::Kind::book(RUNS, crate::kept::Key::Plain, crate::kept::fits::<Run>).when_read(),
    crate::kept::Kind::book(COUNTER, crate::kept::Key::Plain, crate::kept::fits::<i64>).when_read(),
    crate::kept::Kind::book(TRACES, crate::kept::Key::Plain, crate::kept::fits::<i64>).when_read(),
    crate::kept::Kind::book(
        SESSIONS,
        crate::kept::Key::Plain,
        crate::kept::fits::<Session>,
    )
    .when_read(),
    crate::kept::Kind::book(
        GLOBAL,
        crate::kept::Key::Plain,
        crate::kept::fits::<Vec<i64>>,
    )
    .when_read(),
];

const BOOK: &str = "vocabulary-book";
const RUNS: &str = "vocabulary-runs";
const COUNTER: &str = "vocabulary-counter";
const TRACES: &str = "vocabulary-traces";
const SESSIONS: &str = "vocabulary-sessions";
const GLOBAL: &str = "vocabulary-global";
const PAGE: usize = 200;
const RECENT: usize = 3;

pub(super) type Failure = (StatusCode, String);
pub(super) type Result<T> = std::result::Result<T, Failure>;
fn unavailable() -> Failure {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        "vocabulary store unavailable".into(),
    )
}
fn rejected((code, why): book::Failure) -> Failure {
    (
        StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_REQUEST),
        why,
    )
}
fn decode<T: DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    serde_json::from_slice(bytes).map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "invalid vocabulary record".into(),
        )
    })
}

/// A record, judged by its type as it is read (`kept.rs`). One that is not this desk's is set
/// aside, counted and said, and the desk goes on without it: a book it starts without, a run
/// that is not there, a session or a page that begins anew. Only the counter is not begun anew,
/// which would write new runs over the ones kept: no run is recorded until it is mended.
async fn load<T: DeserializeOwned>(
    fleet: &FleetHandle,
    kind: &'static str,
    key: &str,
) -> Result<Option<T>> {
    let found = fleet
        .judged(kind, key.to_owned())
        .await
        .map_err(|_| unavailable())?;
    match found {
        crate::kept::Found::Read(bytes) => decode(&bytes).map(Some),
        crate::kept::Found::Unfit if kind == COUNTER => Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "vocabulary counter could not be read: no run is recorded over the ones kept".into(),
        )),
        crate::kept::Found::Absent | crate::kept::Found::Unfit => Ok(None),
    }
}

fn encode(value: &impl Serialize) -> Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| unavailable())
}

#[derive(Serialize, Deserialize)]
pub(super) struct Run {
    pub session: String,
    pub trace: String,
    pub agent: String,
    pub profile: Option<String>,
    pub provider: String,
    pub model: String,
    pub audit: Value,
    pub transcript: String,
    pub latency: i64,
    pub now: i64,
}
impl Run {
    fn row(&self, id: i64) -> Value {
        let mut row = json!({"run_id":id,"session":self.session,"provider":self.provider,"model":self.model,
            "transcript":self.transcript,"created_at":self.now});
        for key in [
            "unit", "capacity", "used", "form", "payload", "included", "dropped",
        ] {
            row[key] = self.audit[key].clone();
        }
        if let Some(stt) = self.audit.get("stt") {
            row["stt"] = stt.clone();
        }
        row
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Session {
    newest: Vec<i64>,
    // Speech remains useful even after a full page of silent turns.
    spoken: Vec<i64>,
}

fn remember(ids: &mut Vec<i64>, id: i64, limit: usize) {
    let at = ids.partition_point(|previous| *previous > id);
    if at < limit && ids.get(at) != Some(&id) {
        ids.insert(at, id);
        ids.truncate(limit);
    }
}

#[derive(Default, Clone)]
pub(crate) struct Desk {
    guard: Arc<tokio::sync::Mutex<()>>,
}

async fn run(fleet: &FleetHandle, id: i64) -> Result<Run> {
    load(fleet, RUNS, &id.to_string())
        .await?
        .ok_or_else(unavailable)
}

impl Desk {
    pub(super) async fn administration(
        &self,
        fleet: &FleetHandle,
        action: &str,
        data: &Value,
        now: i64,
        known: bool,
        write: bool,
    ) -> Result<Value> {
        let _lock = self.guard.lock().await;
        let book: Book = load(fleet, BOOK, "book").await?.unwrap_or_default();
        if !write {
            return book.read(action, data).map_err(rejected);
        }
        let (next, response) = book
            .change(action, data, now, &crate::ids::hex(6), known)
            .map_err(rejected)?;
        fleet
            .keep(BOOK, "book".into(), Some(encode(&next)?))
            .await
            .map_err(|_| unavailable())?;
        Ok(response)
    }

    pub(super) async fn inputs(
        &self,
        fleet: &FleetHandle,
        session: &str,
    ) -> Result<(Book, Vec<String>)> {
        let _lock = self.guard.lock().await;
        let book = load(fleet, BOOK, "book").await?.unwrap_or_default();
        let mut recent = Vec::new();
        if !session.is_empty()
            && let Some(index) = load::<Session>(fleet, SESSIONS, session).await?
        {
            for id in index.spoken {
                let r = run(fleet, id).await?;
                if !r.transcript.is_empty() {
                    recent.push(r.transcript);
                }
            }
        }
        Ok((book, recent))
    }

    pub(super) async fn record(&self, fleet: &FleetHandle, record: Run) -> Result<i64> {
        let _lock = self.guard.lock().await;
        let last = load::<i64>(fleet, COUNTER, "last")
            .await?
            .unwrap_or_default();
        let id = last.checked_add(1).ok_or_else(unavailable)?;
        let mut session: Session = load(fleet, SESSIONS, &record.session)
            .await?
            .unwrap_or_default();
        remember(&mut session.newest, id, PAGE);
        if !record.transcript.is_empty() {
            remember(&mut session.spoken, id, RECENT);
        }
        let mut all: Vec<i64> = load(fleet, GLOBAL, "all").await?.unwrap_or_default();
        remember(&mut all, id, PAGE);

        // A rolled-back INSERT in the reference also rolls back its sequence.
        let mut writes = vec![
            (COUNTER, "last".into(), encode(&id)?),
            (SESSIONS, record.session.clone(), encode(&session)?),
            (GLOBAL, "all".into(), encode(&all)?),
            (RUNS, id.to_string(), encode(&record)?),
        ];
        if !record.trace.is_empty() {
            writes.push((TRACES, record.trace.clone(), encode(&id)?));
        }
        fleet
            .change_together(writes, Vec::new())
            .await
            .map_err(|_| unavailable())?;
        Ok(id)
    }

    pub(crate) async fn finish(
        &self,
        fleet: &FleetHandle,
        id: i64,
        transcript: String,
        latency: i64,
        stt: Option<&Value>,
    ) -> Result<()> {
        let _lock = self.guard.lock().await;
        let mut r = run(fleet, id).await?;
        let mut writes = Vec::new();
        if !transcript.is_empty() && r.transcript.is_empty() {
            let mut session: Session = load(fleet, SESSIONS, &r.session).await?.unwrap_or_default();
            remember(&mut session.spoken, id, RECENT);
            writes.push((SESSIONS, r.session.clone(), encode(&session)?));
        }
        if let Some(report) = stt {
            r.audit["stt"] = report.clone();
        }
        r.transcript = transcript;
        r.latency = latency;
        writes.push((RUNS, id.to_string(), encode(&r)?));
        fleet
            .change_together(writes, Vec::new())
            .await
            .map_err(|_| unavailable())
    }

    pub(super) async fn report(
        &self,
        fleet: &FleetHandle,
        action: &str,
        v: &Value,
    ) -> Result<Value> {
        let _lock = self.guard.lock().await;
        if action == "run" {
            let missing = || (StatusCode::NOT_FOUND, "no run for that trace".into());
            let trace = v["trace_id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(missing)?;
            let id = load(fleet, TRACES, trace).await?.ok_or_else(missing)?;
            let mut row = run(fleet, id).await?.row(id);
            row["audio_url"] = Value::Null;
            return Ok(row);
        }
        let limit = v["limit"]
            .as_str()
            .and_then(clarp_kernel::python::int)
            .unwrap_or(20)
            .clamp(1, PAGE as i128) as usize;
        let ids = match v["session"].as_str().filter(|s| !s.is_empty()) {
            Some(s) => {
                load::<Session>(fleet, SESSIONS, s)
                    .await?
                    .unwrap_or_default()
                    .newest
            }
            None => load(fleet, GLOBAL, "all").await?.unwrap_or_default(),
        };
        let mut rows = Vec::new();
        for id in ids.into_iter().take(limit) {
            rows.push(run(fleet, id).await?.row(id));
        }
        Ok(json!({"runs":rows}))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests;
