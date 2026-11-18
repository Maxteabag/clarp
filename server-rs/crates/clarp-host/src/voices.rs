//! What an app reads and sets of voices: the catalogue its picker shows, the voice of one
//! agent, a preview of a voice, and the record an app keeps of what was said.
//!
//! The catalogue is the Python Host's, field for field ([`clarp_kernel::voices`], held by
//! that Host's own answers): the engines that speak, whether each can be used, each voice
//! with who has it. One thing is this Host's own: an engine it has not built (ElevenLabs,
//! Deepgram, Google's Chirp voices) is told as one that cannot be used here, whatever keys
//! the owner has, so that nobody gives an agent a voice nothing on this Host can speak in.
//!
//! An agent's voice is kept with the agent, as the Python Host keeps it: one voice for each
//! engine it has one for. A voice another agent has is refused.

use std::collections::HashMap;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::said::{merge_voice, resolve_voice};
use clarp_kernel::voices::{self, Agent, Speech};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use toml::Table;

use crate::edge::{ApiError, App};
use crate::fleet::{Amend, Directory};
use crate::media::header_text;
use crate::owner;


/// The engines that speak which are built into this Host.
use crate::voice::BUILT;

/// How many events of speech an app may report at once (the Python Host's `MAX_BATCH`).
const MOST_EVENTS: usize = 500;

/// A phone whose clock is further than this from the Host's is taken as not set.
const CLOCKS_APART_MS: i64 = 6 * 60 * 60 * 1000;

fn bad(why: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, why)
}

/// The agents of the fleet as the catalogue needs them.
fn agents_of(directory: &Directory) -> Vec<Agent<'_>> {
    let members = directory.members.iter();
    members
        .filter(|member| member.record.deleted_at_ms.is_none())
        // On this Host an agent's persona is its name.
        .map(|member| Agent {
            session: &member.record.session,
            name: &member.record.persona,
            persona: &member.record.persona,
            voice_id: &member.record.voice_id,
        })
        .collect()
}

/// The catalogue as the Python Host builds it from the owner's file, the agents and
/// Cartesia's own list.
pub fn told(
    owners: &Table,
    agents: &[Agent<'_>],
    current: &str,
    has_key: &dyn Fn(&str) -> bool,
    cartesia: &[Value],
) -> Value {
    let chosen = owner::voices(owners);
    let fallback = match chosen.fallback.as_str() {
        "" => "none",
        fallback => fallback,
    };
    let speech = Speech {
        provider: &chosen.provider,
        fallback,
        has_key,
        google_error: owner::google_voice_error(owners),
        voices: &chosen,
        cartesia,
    };
    voices::catalog(agents, current, &speech)
}

/// The engines that speak, as the old settings screen reads them, by the owner's file.
pub fn engines(owners: &Table, has_key: &dyn Fn(&str) -> bool) -> Value {
    let chosen = owner::voices(owners);
    let fallback = match chosen.fallback.as_str() {
        "" => "none",
        fallback => fallback,
    };
    let speech = Speech {
        provider: &chosen.provider,
        fallback,
        has_key,
        google_error: owner::google_voice_error(owners),
        voices: &chosen,
        cartesia: &[],
    };
    let source = match owners.get("google_tts").and_then(|own| own.as_table()) {
        Some(_) => "google_tts",
        None => "google_stt",
    };
    let mut result=voices::status(&speech, source);
    result["voice"]=json!(owner::text_at(owners,&["local_tts","voice"]).unwrap_or_default());
    result
}

/// `GET /tts/providers`: the engines that speak, for the old settings screen. An engine
/// this Host has not built cannot be used here, as in the catalogue.
async fn engines_told(State(app): State<App>) -> Json<Value> {
    let owners = app.mouth.owners();
    let has_key = |provider: &str| app.mouth.has_key(provider, &owners);
    let mut told = engines(&owners, &has_key);
    for row in told["providers"].as_array_mut().into_iter().flatten() {
        let id = row["id"].as_str().unwrap_or("");
        if id != "none" && !BUILT.contains(&id) {
            row["available"] = json!(false);
        }
    }
    Json(told)
}

/// `POST /tts/providers`: choosing the engine from an app. Who speaks is read from the
/// owner's file here.
async fn choose(State(app):State<App>,body:Bytes)->Result<Json<Value>,ApiError>{
    let data:Value=serde_json::from_slice(&body).map_err(|_|bad("bad json"))?;
    let word=|key:&str,default:&str| data[key].as_str().filter(|s|!s.is_empty()).unwrap_or(default).trim().to_string();
    let provider=word("provider","").to_lowercase();
    let fallback=word("fallback","none").to_lowercase();
    if [&provider,&fallback].iter().any(|p|p.as_str()!="none" && !BUILT.contains(&p.as_str())){return Err(bad("unsupported TTS provider or fallback"));}
    let mut changes=vec![("speech.provider".into(),provider),("speech.fallback".into(),fallback)];
    let voice=word("voice","");
    if !voice.is_empty(){changes.push(("speech.local_voice".into(),voice));}
    app.fleet.set(changes).await.map_err(|_|ApiError::new(StatusCode::SERVICE_UNAVAILABLE,"speech settings could not be saved"))?;
    let Json(mut result)=engines_told(State(app)).await;
    result["restart_scheduled"]=json!(false);
    Ok(Json(result))
}

/// The same catalogue as this Host tells it: an engine it has not built cannot be used
/// here, and whether there is a voice at all is counted again.
fn here(mut catalogue: Value) -> Value {
    let mut speaks = false;
    for row in catalogue["providers"].as_array_mut().into_iter().flatten() {
        let built = row["id"].as_str().is_some_and(|id| BUILT.contains(&id));
        if !built {
            row["available"] = json!(false);
        }
        speaks |= row["available"] == true && (row["selected"] == true || row["fallback"] == true);
    }
    catalogue["voice_available"] = json!(speaks);
    catalogue
}

/// The catalogue for one agent (`for`), or for nobody in particular.
async fn catalogue_for(app: &App, current: &str) -> Value {
    let owners = app.mouth.owners();
    let cartesia = app.mouth.cartesia_list().await;
    let directory = app.fleet.directory();
    let agents = agents_of(&directory);
    let has_key = |provider: &str| app.mouth.has_key(provider, &owners);
    here(told(&owners, &agents, current, &has_key, &cartesia))
}

/// `GET /voice-catalog?for=<session>`.
async fn catalog(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
) -> Json<Value> {
    let current = asked.get("for").map_or("", |session| session.trim());
    Json(catalogue_for(&app, current).await)
}

/// `GET /voices?for=<session>`: the ElevenLabs voices of the old settings screen, each with
/// who has it, and what the agent is called.
async fn settings_voices(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
) -> Json<Value> {
    let current = asked.get("for").map_or("", |session| session.trim());
    let directory = app.fleet.directory();
    let agents = agents_of(&directory);
    let persona = agents.iter().find(|agent| agent.session == current);
    let persona = persona.map_or("", |agent| agent.name);
    // What the persona of that name is like: its way of speaking, without the word that
    // opens the clause an agent's tool is told.
    let way = clarp_kernel::personas::personality(persona);
    Json(json!({
        "voices": voices::settings_voices(&agents, current),
        "bio": way.strip_prefix("Personality: ").unwrap_or(way),
        "persona": persona,
    }))
}

/// `POST /agent-voice` with `{session, voice_id, provider}`: an agent is given a voice, for
/// one engine when the engine is named. A voice another agent has is refused.
async fn agent_voice(State(app): State<App>, body: Bytes) -> Result<Response, ApiError> {
    let said: Map<String, Value> = if body.is_empty() {
        Map::new()
    } else {
        match serde_json::from_slice(&body) {
            Ok(Value::Object(said)) => said,
            _ => return Err(bad("bad json")),
        }
    };
    let text = |name: &str| said.get(name).and_then(Value::as_str).map_or("", str::trim);
    let (session, voice) = (text("session"), text("voice_id"));
    let provider = text("provider").to_lowercase();
    if session.is_empty() || voice.is_empty() {
        return Err(bad("session and voice_id required"));
    }
    // Google's voices are a closed list.
    if provider == "google" && !voices::google_has(voice) {
        return Err(bad("unknown voice"));
    }
    let directory = app.fleet.directory();
    let Some(member) = directory.by_session(session) else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "no such agent"));
    };
    for other in directory
        .members
        .iter()
        .filter(|other| other.record.session != session)
    {
        let stored = Some(other.record.voice_id.as_str());
        let has = match provider.as_str() {
            "" => stored.map(str::to_string),
            provider => resolve_voice(stored, provider),
        };
        if has.as_deref() == Some(voice) {
            let by = match other.record.persona.as_str() {
                "" => other.record.session.as_str(),
                name => name,
            };
            let taken = json!({ "error": "voice taken", "by": by });
            return Ok((StatusCode::CONFLICT, Json(taken)).into_response());
        }
    }
    let stored = Some(member.record.voice_id.as_str());
    let given = match provider.as_str() {
        "" => voice.to_string(),
        provider => merge_voice(stored, provider, voice),
    };
    let amended = app
        .fleet
        .amend(session.to_string(), Amend::Voice(given))
        .await?;
    if amended.is_err() {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "no such agent"));
    }
    Ok(Json(json!({ "ok": true })).into_response())
}

/// `GET /voice-preview?provider=&id=`: a voice saying who it is. Made once and kept: the
/// same voice is not paid for twice.
async fn preview(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> Response {
    let provider = asked
        .get("provider")
        .map_or("", |text| text.trim())
        .to_lowercase();
    let voice = asked.get("id").map_or("", |text| text.trim());
    let catalogue = catalogue_for(&app, "").await;
    let rows = catalogue["providers"].as_array().into_iter().flatten();
    let row = rows.clone().find(|row| row["id"] == provider.as_str());
    let listed = row.filter(|row| row["available"] == true).and_then(|row| {
        let voices = row["voices"].as_array().into_iter().flatten();
        voices.clone().find(|listed| listed["id"] == voice)
    });
    let Some(listed) = listed else {
        return ApiError::plain(StatusCode::NOT_FOUND, "voice unavailable").into_response();
    };
    let name = listed["name"].as_str().unwrap_or("");
    let said = format!("Hello, I'm {name}. This is how I'll sound in Clarp.");
    // What can change the sound is in the name the preview is kept under.
    let kept_as: String = Sha256::digest(format!("v3\0{provider}\0{voice}\0\0{said}").as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    match app.mouth.preview(&provider, voice, &said, &kept_as).await {
        Ok(preview) => preview.send(&header_text(&headers, "range")),
        Err(why) => {
            eprintln!("clarp-host: a preview of a voice could not be made: {why}");
            ApiError::plain(StatusCode::BAD_GATEWAY, "preview unavailable").into_response()
        }
    }
}

/// A number as the Python Host reads one from an app: a number, never a truth.
async fn cartesia_voices(State(app):State<App>,Query(asked):Query<HashMap<String,String>>)->Result<Json<Value>,ApiError>{
    let force=asked.get("force").is_some_and(|s|matches!(s.as_str(),"1"|"true"));
    let rows=app.mouth.cartesia_rows(force,false).await.map_err(|_|ApiError::new(StatusCode::BAD_GATEWAY,"Cartesia voice library unavailable"))?;
    let owners=app.mouth.owners();
    let settings=owner::voices(&owners);
    let available=settings.provider=="cartesia" && app.mouth.has_key("cartesia",&owners);
    let directory=app.fleet.directory();
    let mut occupied=HashMap::new();
    for agent in agents_of(&directory) {
        let voice=resolve_voice(Some(agent.voice_id),"cartesia").or_else(||settings.cartesia_voice_for(agent.name).map(str::to_string));
        if let Some(id)=voice {occupied.insert(id,agent.name);}
    }
    let voices=rows.iter().filter(|_|available).filter_map(|r|{
        let id=r["id"].as_str().filter(|s|!s.is_empty())?;
        let mut row=json!({"id":id,"name":r["name"].as_str().filter(|s|!s.is_empty()).unwrap_or(id),
            "taken_by":occupied.get(id),"selection_value":json!({"cartesia":id}).to_string()});
        let mut quoted=String::new();
        use std::fmt::Write as _;
        for byte in id.bytes(){if byte.is_ascii_alphanumeric() || b"-_.~/".contains(&byte){quoted.push(byte.into());}else{let _=write!(quoted,"%{byte:02X}");}}
        row["preview_url"]=json!(format!("/cartesia-voice-preview?id={quoted}"));
        for key in ["tagline","description","gender","language","country"]{row[key]=json!(r[key].as_str().unwrap_or(""));}
        Some(row)
    }).collect::<Vec<_>>();
    Ok(Json(json!({"available":available,"voices":voices})))
}

async fn cartesia_preview(State(app):State<App>,Query(asked):Query<HashMap<String,String>>, headers:HeaderMap)->Response{
    let id=asked.get("id").map_or("",|id|id.trim());
    let rows=app.mouth.cartesia_rows(false,true).await.unwrap_or_default();
    let Some(row)=rows.iter().find(|r|r["id"]==id) else {return ApiError::plain(StatusCode::NOT_FOUND,"not found").into_response()};
    let name:String=row["name"].as_str().filter(|s|!s.is_empty()).unwrap_or("this voice").chars().take(80).collect();
    let prompt=format!("Hello, I'm {name}. This is how I'll sound in Clarp.");
    let model=owner::cartesia_model(&app.mouth.owners());
    let key=format!("{:x}",Sha256::digest(format!("v1\0{id}\0{name}\0{model}\0{prompt}").as_bytes()));
    match app.mouth.preview("cartesia",id,&prompt,&key).await {
        Ok(p)=>p.send(&header_text(&headers,"range")),
        Err(_)=>ApiError::plain(StatusCode::BAD_GATEWAY,"preview unavailable").into_response()
    }
}

async fn legacy_preview(State(app):State<App>,body:Bytes)->Result<Json<Value>,ApiError>{
    let data:Value=serde_json::from_slice(&body).map_err(|_|bad("bad json"))?;
    let voice=data["voice_id"].as_str().unwrap_or("").trim();
    if voice.is_empty(){return Err(bad("voice_id required"));}
    let text=data["text"].as_str().filter(|s|!s.is_empty()).unwrap_or("Hi there, this is what I sound like.").trim();
    let owners=app.mouth.owners();
    let default=owner::text_at(&owners,&["server","default_session"]).unwrap_or_else(||"claude".into());
    let session=data["session"].as_str().map(str::trim).filter(|s|!s.is_empty()).unwrap_or(&default);
    let name=app.mouth.legacy_preview(session,voice,clarp_kernel::python::first(text,200)).await.map_err(|e|ApiError::new(StatusCode::INTERNAL_SERVER_ERROR,&e))?;
    Ok(Json(json!({"ok":true,"url":format!("/audio/{name}")})))
}

fn whole(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Number(number) => number.as_i64().or_else(|| {
            let float = number.as_f64().filter(|float| float.is_finite())?;
            // Towards zero, as Python's `int()` cuts.
            Some(float.trunc() as i64)
        }),
        _ => None,
    }
}

/// `POST /voice-events`: an app reports what its microphone and its player did, for the
/// record of each thing said. This Host keeps no such record yet: the report is read as
/// the Python Host reads it, refused where that Host refuses it, and answered with how
/// many events were kept, which is none.
async fn voice_events(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let said: Map<String, Value> = if body.is_empty() {
        Map::new()
    } else {
        match serde_json::from_slice(&body) {
            Ok(Value::Object(said)) => said,
            _ => return Err(bad("bad json")),
        }
    };
    let Some(Value::Array(events)) = said.get("events") else {
        return Err(bad("events must be a list"));
    };
    if events.len() > MOST_EVENTS {
        return Err(ApiError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "batch too large",
        ));
    }
    let now = app.clock.now_ms();
    // How far the app's clock is from the Host's, by when it says it sent the report.
    let apart = whole(said.get("sent_at")).map(|sent| now - sent);
    let apart = apart.filter(|apart| apart.abs() <= CLOCKS_APART_MS);
    Ok(Json(json!({
        "ok": true,
        "stored": 0,
        "clock_offset_ms": apart,
        "server_now": now,
    })))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/voice-catalog", get(catalog))
        .route("/tts/providers", get(engines_told).post(choose))
        .route("/voices", get(settings_voices))
        .route("/agent-voice", post(agent_voice))
        .route("/voice-preview", get(preview))
        .route("/cartesia-voices",get(cartesia_voices))
        .route("/cartesia-voice-preview",get(cartesia_preview))
        .route("/preview",post(legacy_preview))
        .route("/voice-events", post(voice_events))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// The Python Host's own answers (`server-rs/tools/catalog-vectors.py`).
    fn vectors() -> Value {
        let written = include_str!("../../clarp-kernel/tests/fixtures/catalog-vectors.json");
        serde_json::from_str(written).unwrap()
    }

    #[test]
    fn the_catalogue_of_voices_is_the_python_hosts() {
        let vectors = vectors();
        let cases = vectors["cases"].as_array().unwrap();
        assert!(cases.len() >= 7);
        for case in cases {
            let owners: Table = case["config"].as_str().unwrap().parse().unwrap();
            // An agent has a name, a persona, both or neither; one that is nothing has none.
            let fleet = case["agents"].as_object().unwrap();
            let text = |agent: &Value, name: &str| agent[name].as_str().unwrap_or("").to_string();
            let held: Vec<(String, String, String, String)> = fleet
                .iter()
                .map(|(session, agent)| {
                    let (name, persona) = (text(agent, "name"), text(agent, "persona"));
                    (session.clone(), name, persona, text(agent, "voice_id"))
                })
                .collect();
            let agents: Vec<Agent<'_>> = held
                .iter()
                .map(|(session, name, persona, voice_id)| Agent {
                    session,
                    name,
                    persona,
                    voice_id,
                })
                .collect();
            // The keys a config has, as the Python Host finds them.
            let has_key = |provider: &str| {
                let section = match provider {
                    "gemini" => "gemini_tts",
                    other => other,
                };
                owner::text_at(&owners, &[section, "api_key"]).is_some()
            };
            // Cartesia's list is fetched with its key: without one there is none.
            let live = case["cartesia_live"].as_array().unwrap();
            let live: &[Value] = if has_key("cartesia") { live } else { &[] };
            let current = case["session"].as_str().unwrap();
            let ours = told(&owners, &agents, current, &has_key, live);
            assert_eq!(ours, case["answer"], "{}", case["name"]);
            // And the engines as the old settings screen reads them.
            assert_eq!(
                engines(&owners, &has_key),
                case["engines"],
                "the engines of {}",
                case["name"]
            );
        }
    }

    #[test]
    fn configured_builtin_engines_are_available_in_the_catalogue() {
        let vectors = vectors();
        let chosen = vectors["cases"].as_array().unwrap().iter();
        let case = chosen
            .clone()
            .find(|case| case["name"] == "an engine nobody has is chosen")
            .unwrap();
        // The Python Host with keys for everything: every engine could be used.
        let mut all = case["answer"].clone();
        for row in all["providers"].as_array_mut().unwrap() {
            row["available"] = json!(true);
        }
        let ours = here(all);
        let usable: Vec<&str> = ours["providers"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["available"] == true)
            .map(|row| row["id"].as_str().unwrap())
            .collect();
        assert_eq!(usable, ["cartesia","elevenlabs","gemini","deepgram","google"]);
        // ElevenLabs was chosen, with Deepgram as its fallback.
        assert_eq!(ours["voice_available"], true);
        assert_eq!(whole(Some(&json!(12.9))), Some(12));
        assert_eq!(whole(Some(&json!(true))), None);
        assert_eq!(whole(Some(&json!("12"))), None);
    }
}
