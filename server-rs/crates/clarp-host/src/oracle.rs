//! The Oracle's contact and the voice it speaks with, as this Host keeps and tells them.
//!
//! The Oracle is the voice a user speaks with hands-free. What it is asked to look into it
//! hands to one agent, its contact: the Host keeps who that is (`GET` and `POST
//! /oracle/contact`), and the owner's choice between the two voice models a call of it can
//! run on (`GET` and `POST /oracle/voice-provider`). Both are kept among the Host's settings
//! under the names the Python Host keeps them under. The rules are in
//! [`clarp_kernel::oracle`]; the routes answer as the Python Host's
//! (`server/lib/routes/oracle.py`).
//!
//! The Oracle's conversation is not built on this Host yet: `/oracle/status` says so, and an
//! app that reads it offers no call. What is chosen here is kept for the day it is.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::oracle::{self, Agent, CONTACT_KEY, VOICE_KEY, Voices};
use serde_json::Value;
use tokio::sync::Mutex;

use crate::edge::{ApiError, App};
use crate::fleet::Directory;
use crate::owner::{self, OwnerFile};
use crate::personas::object;

type Answered = Result<Json<Value>, ApiError>;

/// The Oracle's desk: one request at a time reads what is kept of the Oracle and writes it,
/// so that a contact chosen while a stale one is being let go is not let go with it.
#[derive(Default)]
pub struct Desk {
    turn: Mutex<()>,
}

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

pub fn routes() -> Router<App> {
    Router::new()
        .route("/oracle/contact", get(contact).post(choose_contact))
        .route("/oracle/voice-provider", get(voice).post(choose_voice))
}
