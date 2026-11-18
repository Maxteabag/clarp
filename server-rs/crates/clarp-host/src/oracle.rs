//! The Oracle on this Host: its contact, the voice it speaks with, what it hands to agents,
//! and its calls.
//!
//! The Oracle is the voice a user speaks with hands-free. What it is asked to look into it
//! hands to one agent, its contact: the Host keeps who that is (`GET` and `POST
//! /oracle/contact`), and the owner's choice between the two voice models a call of it can
//! run on (`GET` and `POST /oracle/voice-provider`). Both are kept among the Host's settings
//! under the names the Python Host keeps them under. The rules are in
//! [`clarp_kernel::oracle`]; the routes answer as the Python Host's
//! (`server/lib/routes/oracle.py`).
//!
//! What calls it can hold it says itself (`GET /oracle/status`): an app offers a call where
//! that says one can be held, and no more than is said there.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::oracle::{self, Agent, CONTACT_KEY, VOICE_KEY, Voices};
use serde_json::{Value, json};
use tokio::sync::Mutex;

use crate::edge::{ApiError, App};
use crate::fleet::Directory;
use crate::owner::{self, OwnerFile};
use crate::personas::object;

mod call;
mod delegations;
mod handoffs;
mod memory;
mod router;
mod tools;

type Answered = Result<Json<Value>, ApiError>;

/// The Oracle's desk: one request at a time reads what is kept of the Oracle and writes it,
/// so that a contact chosen while a stale one is being let go is not let go with it.
#[derive(Default)]
pub struct Desk {
    turn: Mutex<()>,
    /// What the Oracle handed to agents: read from what was kept when it is first asked
    /// for, and read and written by one request at a time.
    delegations: Mutex<Option<clarp_kernel::oracle::delegations::Book>>,
    /// The memory of the Oracle's threads: read from what was kept when it is first asked
    /// for, and asked by one question at a time.
    threads: Mutex<Option<clarp_kernel::oracle::threads::Memory>>,
    /// The calls that are open, by the phone each is of.
    calls: call::Calls,
    /// The handoffs: calls put through to agents, and back.
    handoffs: handoffs::Handoffs,
}

/// What of the Oracle runs by itself from the start of the Host: the deadlines of handoffs.
pub fn run(app: &App) {
    handoffs::run(app);
}

pub(crate) use call::zone_of_this_machine;
pub(crate) use handoffs::put_through;
pub(crate) use router::asked as route_voice;

/// The agents of this Host, as a contact is held against them. Its janitors are apart from
/// its roster and are not among them: a janitor is no agent a contact names.
fn agents(directory: &Directory) -> Vec<Agent<'_>> {
    let roster = directory.members.iter();
    roster
        .map(|member| {
            let record = &member.record;
            let parent = record.parent_agent_id.as_deref();
            Agent {
                session: &record.session,
                persona: &record.persona,
                archived: record.archived_at_ms.is_some(),
                janitor: false,
                helper: record.role == "helper",
                child: parent.is_some_and(|parent| !parent.is_empty()),
            }
        })
        .collect()
}

/// Keeps one thing of the Oracle among the Host's settings. Nothing is kept as an empty
/// text: what reads it takes that for nothing.
async fn keep(app: &App, key: &str, text: String) -> Result<(), ApiError> {
    Ok(app.fleet.set(vec![(key.to_string(), text)]).await?)
}

/// `GET /oracle/contact`: the contact, held against the agents there are now. One that
/// names no agent any more is let go and said to be stale, once.
async fn contact(State(app): State<App>) -> Answered {
    let _turn = app.oracle.turn.lock().await;
    let directory = app.fleet.directory();
    let kept = directory.setting(CONTACT_KEY).unwrap_or("");
    let (told, let_go) = oracle::contact(kept, &agents(&directory));
    if let_go {
        keep(&app, CONTACT_KEY, String::new()).await?;
        let gone = &told.stale;
        eprintln!(
            "clarp-host: the Oracle's contact is no agent any more oracleContactStale cleared={gone:?}"
        );
    }
    Ok(Json(told.told()))
}

/// `POST /oracle/contact` with `{"session": "<a session, a name, or nothing>"}`: the contact
/// is chosen, or cleared. A name that is no one agent's is refused, and what was kept stays.
async fn choose_contact(State(app): State<App>, body: Bytes) -> Answered {
    let said = object(&body)?;
    let _turn = app.oracle.turn.lock().await;
    let directory = app.fleet.directory();
    let named = said.get("session").unwrap_or(&Value::Null);
    let chosen = oracle::choose_contact(named, &agents(&directory));
    let (told, kept) = chosen.map_err(|why| ApiError::new(StatusCode::CONFLICT, why))?;
    keep(&app, CONTACT_KEY, kept.unwrap_or_default()).await?;
    let session = Some(told.session.as_str()).filter(|session| !session.is_empty());
    let session = session.unwrap_or("-");
    eprintln!("clarp-host: the Oracle's contact is chosen oracleContactSet session={session}");
    Ok(Json(told.told()))
}

/// What is told of the voice a call of the Oracle runs on: the choice a phone made over the
/// owner's file (`[oracle] voice_provider`, `gemini_model`, `gemini_voice`), and whether
/// this Host has a key for the second voice. The owner's file is read now.
fn voice_told(app: &App, directory: &Directory) -> Value {
    let owners = OwnerFile::new(app.config.speech_keys.clone());
    let file = owners.read().unwrap_or_default();
    let text = |name: &str| owner::text_at(&file, &["oracle", name]);
    let (configured, model, voice) = (
        text("voice_provider"),
        text("gemini_model"),
        text("gemini_voice"),
    );
    let said = owner::voices(&file);
    let voices = Voices {
        kept: directory.setting(VOICE_KEY),
        configured: configured.as_deref(),
        has_gemini_key: owners.key("gemini").is_some(),
        model: model.as_deref().unwrap_or(""),
        voice: voice.as_deref().unwrap_or(""),
        of_oracle: said.gemini_voice_for("Oracle"),
        usual: &said.gemini_voice,
    };
    voices.status()
}

/// `GET /oracle/voice-provider`: which voice model a call of the Oracle runs on.
async fn voice(State(app): State<App>) -> Json<Value> {
    Json(voice_told(&app, &app.fleet.directory()))
}

/// `POST /oracle/voice-provider` with `{"provider": "openai" | "gemini"}`: a phone chooses
/// the voice of the calls to come. Anything else is refused, and what was kept stays.
async fn choose_voice(State(app): State<App>, body: Bytes) -> Answered {
    let said = object(&body)?;
    let named = said.get("provider").unwrap_or(&Value::Null);
    let chosen = oracle::choose_voice(named);
    let chosen = chosen.map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let _turn = app.oracle.turn.lock().await;
    keep(&app, VOICE_KEY, chosen.to_string()).await?;
    let told = voice_told(&app, &app.fleet.directory());
    let (selected, effective) = (&told["selected"], &told["provider"]);
    eprintln!(
        "clarp-host: the Oracle's voice is chosen oracleVoiceProvider selected={selected} \
         effective={effective}"
    );
    Ok(Json(told))
}

/// The model and the voice of the first kind of call, as the Python Host names them where
/// its owner wrote nothing else. This Host holds no call of that kind.
const FIRST_KIND: (&str, &str) = ("gpt-realtime-2.1", "cedar");

/// `GET /oracle/status`: what calls of the Oracle this Host can hold.
///
/// The calls of the second kind (`GET /oracle/v2`) are built: they go on in the thread of
/// their owner and contact, begin anew where that is asked, and go to the contact alone
/// where that is asked; and one can be held where the owner's key for their voice is there.
/// Their voice is the one model: the other, and detours into a podcast, are not built. The
/// first kind of call, WebRTC and the other engine of the second kind are not built either,
/// and nothing of them is claimed.
async fn status(State(app): State<App>) -> Json<Value> {
    let key = app.ears.key("openai").is_some();
    let (model, voice) = (oracle::voice::model(), oracle::voice::voice());
    Json(json!({
        "available": false,
        "model": FIRST_KIND.0,
        "voice": FIRST_KIND.1,
        "transport": "clarp-websocket-proxy",
        "webrtc": false,
        "v2": {
            "context_memory": true,
            "context_reset": true,
            "direct_contact": true,
            "direct_contact_available": key,
            "available": key,
            "model": model,
            "voice": voice,
            "podcast": false,
            "podcast_history": false,
            "voice_provider": "openai",
            "voice_providers": ["openai"],
        },
        "v2_tinkered": {
            "context_memory": false,
            "context_reset": false,
            "direct_contact": false,
            "direct_contact_available": false,
            "available": false,
            "model": model,
            "voice": voice,
            "mode": "api",
            "webrtc": false,
            "router": "api",
            "podcast": false,
            "podcast_history": false,
        },
    }))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/oracle/status", get(status))
        .route("/oracle/contact", get(contact).post(choose_contact))
        .route("/oracle/voice-provider", get(voice).post(choose_voice))
        .merge(delegations::routes())
        .merge(handoffs::routes())
        .merge(call::routes())
}

/// Read the same fleet status or transcript the Oracle would show. The live
/// call has already selected this session from its own frozen team.
pub(crate) async fn live_read(
    app: &App,
    principal: &str,
    tool: &str,
    session: &str,
    limit: usize,
) -> Value {
    tools::execute(
        app,
        (principal, ""),
        (tool, &json!({"agent":session,"limit":limit}), ""),
    )
    .await
    .receipt
    .unwrap_or_else(|| json!({"error":"agent not running"}))
}
