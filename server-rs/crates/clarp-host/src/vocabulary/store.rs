//! The book and run records use the same kept-store port as other fleet data.
//! Indexes are disposable: after restart they are reconstructed from the runs.
use crate::fleet::FleetHandle;
use axum::http::StatusCode;
use clarp_kernel::vocabulary::book::{self, Book};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    collections::{BTreeSet, HashMap},
    sync::Arc,
};

const BOOK: &str = "vocabulary-book";
const RUNS: &str = "vocabulary-runs";
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

async fn load_book(fleet: &FleetHandle) -> Result<Book> {
    let data = fleet
        .kept(BOOK, "book".into())
        .await
        .map_err(|_| unavailable())?;
    data.map_or_else(|| Ok(Book::default()), |bytes| decode(&bytes))
}
async fn save(
    fleet: &FleetHandle,
    kind: &'static str,
    key: String,
    value: &impl Serialize,
) -> Result<()> {
    let bytes = serde_json::to_vec(value).map_err(|_| unavailable())?;
    fleet
        .keep(kind, key, Some(bytes))
        .await
        .map_err(|_| unavailable())
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
        row
    }
}

#[derive(Default)]
struct Index {
    all: BTreeSet<i64>,
    sessions: HashMap<String, BTreeSet<i64>>,
    spoken: HashMap<String, BTreeSet<i64>>,
    traces: HashMap<String, i64>,
}
impl Index {
    fn observe(&mut self, id: i64, r: &Run) {
        self.all.insert(id);
        self.sessions
            .entry(r.session.clone())
            .or_default()
            .insert(id);
        let spoken = self.spoken.entry(r.session.clone()).or_default();
        if r.transcript.is_empty() {
            spoken.remove(&id);
        } else {
            spoken.insert(id);
        }
        self.traces
            .entry(r.trace.clone())
            .and_modify(|v| *v = (*v).max(id))
            .or_insert(id);
    }
}

#[derive(Default, Clone)]
pub(crate) struct Desk {
    guard: Arc<tokio::sync::Mutex<Option<Index>>>,
}
async fn index<'a>(held: &'a mut Option<Index>, fleet: &FleetHandle) -> Result<&'a Index> {
    if held.is_none() {
        let mut built = Index::default();
        for (key, bytes) in fleet.kept_all(RUNS).await.map_err(|_| unavailable())? {
            built.observe(key.parse().map_err(|_| unavailable())?, &decode(&bytes)?);
        }
        *held = Some(built);
    }
    held.as_ref().ok_or_else(unavailable)
}
async fn run(fleet: &FleetHandle, id: i64) -> Result<Run> {
    let bytes = fleet
        .kept(RUNS, id.to_string())
        .await
        .map_err(|_| unavailable())?
        .ok_or_else(unavailable)?;
    decode(&bytes)
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
        let book = load_book(fleet).await?;
        if !write {
            return book.read(action, data).map_err(rejected);
        }
        let (next, response) = book
            .change(action, data, now, &crate::ids::hex(6), known)
            .map_err(rejected)?;
        save(fleet, BOOK, "book".into(), &next).await?;
        Ok(response)
    }

    pub(super) async fn inputs(
        &self,
        fleet: &FleetHandle,
        session: &str,
    ) -> Result<(Book, Vec<String>)> {
        let mut held = self.guard.lock().await;
        let book = load_book(fleet).await?;
        let mut recent = Vec::new();
        if !session.is_empty()
            && let Some(ids) = index(&mut held, fleet).await?.spoken.get(session)
        {
            for id in ids.iter().rev().take(3) {
                recent.push(run(fleet, *id).await?.transcript);
            }
        }
        Ok((book, recent))
    }

    async fn write_run(
        held: &mut Option<Index>,
        fleet: &FleetHandle,
        id: i64,
        r: &Run,
    ) -> Result<()> {
        // If this task is cancelled while the fleet is writing, a subsequent
        // reader rebuilds its index from the authoritative records.
        let mut cached = held.take();
        save(fleet, RUNS, id.to_string(), r).await?;
        if let Some(ix) = &mut cached {
            ix.observe(id, r);
        }
        *held = cached;
        Ok(())
    }

    pub(super) async fn record(&self, fleet: &FleetHandle, record: Run) -> Result<i64> {
        let mut held = self.guard.lock().await;
        let mut book = load_book(fleet).await?;
        let id = book.last_run.checked_add(1).ok_or_else(unavailable)?;
        book.last_run = id;
        // Reserve the id first. A failed record may leave a gap, never a
        // reused id or a later run overwriting one already returned to a phone.
        save(fleet, BOOK, "book".into(), &book).await?;
        Self::write_run(&mut held, fleet, id, &record).await?;
        Ok(id)
    }

    pub(crate) async fn finish(
        &self,
        fleet: &FleetHandle,
        id: i64,
        transcript: String,
        latency: i64,
    ) -> Result<()> {
        let mut held = self.guard.lock().await;
        let mut r = run(fleet, id).await?;
        r.transcript = transcript;
        r.latency = latency;
        Self::write_run(&mut held, fleet, id, &r).await
    }

    pub(super) async fn report(
        &self,
        fleet: &FleetHandle,
        action: &str,
        v: &Value,
    ) -> Result<Value> {
        let mut held = self.guard.lock().await;
        let ix = index(&mut held, fleet).await?;
        if action == "run" {
            let id = ix
                .traces
                .get(
                    v["trace_id"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or(""),
                )
                .ok_or_else(|| (StatusCode::NOT_FOUND, "no run for that trace".into()))?;
            let mut row = run(fleet, *id).await?.row(*id);
            row["audio_url"] = Value::Null;
            return Ok(row);
        }
        let limit = v["limit"]
            .as_str()
            .and_then(clarp_kernel::python::int)
            .unwrap_or(20)
            .clamp(1, 200) as usize;
        let ids = match v["session"].as_str().filter(|s| !s.is_empty()) {
            Some(s) => ix.sessions.get(s),
            None => Some(&ix.all),
        };
        let mut rows = Vec::new();
        if let Some(ids) = ids {
            for id in ids.iter().rev().take(limit) {
                rows.push(run(fleet, *id).await?.row(*id));
            }
        }
        Ok(json!({"runs":rows}))
    }
}
