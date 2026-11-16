//! The personas the owner makes himself, as the apps ask for them.
//!
//! A persona is who an agent can be: a name with its voices, how it speaks, a symbol or a
//! picture. A Host has many built in; the owner makes more (`POST /personas`), changes them
//! (`POST /personas/update`) and removes them (`DELETE /personas/<name>`), and an app shows
//! a persona's own picture (`GET /persona-avatars/<id>`). Of a persona that is built in
//! only the look can be changed.
//!
//! The rules are in [`clarp_kernel::personas`]. The change itself is the fleet's
//! ([`crate::fleet::FleetHandle::persona_write`]): one writer for who an agent can be. The
//! routes answer as the Python Host's do (`server/lib/routes/personas.py`), in its words
//! where they refuse.

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_kernel::personas::{self as rules, Owner};
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::{PersonaSaid, picture_name};
use crate::media;

/// A request's body as the object it has to be. No body is an empty object.
pub(crate) fn object(body: &Bytes) -> Result<Map<String, Value>, ApiError> {
    let given = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(body).unwrap_or(Value::Null)
    };
    match given {
        Value::Object(fields) => Ok(fields),
        _ => Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json")),
    }
}

/// A field as the text the Python Host makes of it: a text as it is, a number in its
/// digits, and nothing for what says nothing.
fn text(fields: &Map<String, Value>, key: &str) -> String {
    match fields.get(key) {
        Some(Value::String(said)) => said.clone(),
        Some(Value::Number(said)) => said.to_string(),
        _ => String::new(),
    }
}

fn said(fields: &Map<String, Value>, voice_id: String) -> PersonaSaid {
    PersonaSaid {
        name: text(fields, "name"),
        voice_id,
        avatar_symbol: text(fields, "avatar_symbol"),
        personality: text(fields, "personality"),
        picture: text(fields, "avatar_base64"),
    }
}

/// A change that the rules refuse is answered 409 with the rule's words.
fn refused(why: String) -> ApiError {
    ApiError::new(StatusCode::CONFLICT, why)
}

/// `POST /personas` with `{name, voice_id?, avatar_symbol?, personality?, avatar_base64?}`:
/// the owner makes a persona. Without a voice, or with the word for none (`{}`), it is given
/// one that no agent and no persona has.
async fn create(State(app): State<App>, body: Bytes) -> Result<Response, ApiError> {
    let asked = object(&body)?;
    let voice = text(&asked, "voice_id").trim().to_string();
    let voice = if voice == "{}" { String::new() } else { voice };
    let fetched = app.mouth.cartesia_known();
    let made = app.fleet.persona_write(None, said(&asked, voice), fetched);
    let persona = made.await?.map_err(refused)?;
    Ok((StatusCode::CREATED, Json(json!({ "persona": persona }))).into_response())
}

/// `POST /personas/update` with `{original_name, name, voice_id, ...}`: the owner changes
/// the persona that was called `original_name`.
async fn update(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let asked = object(&body)?;
    let original = text(&asked, "original_name");
    let fetched = app.mouth.cartesia_known();
    let voice = text(&asked, "voice_id");
    let changed = app
        .fleet
        .persona_write(Some(original), said(&asked, voice), fetched);
    let persona = changed.await?.map_err(refused)?;
    Ok(Json(json!({ "persona": persona })))
}

/// `DELETE /personas/<name>`: the owner removes a persona of his own. One that is built in,
/// or that is not there, is not removed.
async fn remove(State(app): State<App>, Path(name): Path<String>) -> Result<Json<Value>, ApiError> {
    let name = name.trim_matches('/').to_string();
    if app.fleet.persona_remove(name).await? {
        Ok(Json(json!({ "ok": true })))
    } else {
        Err(ApiError::new(StatusCode::NOT_FOUND, rules::NOT_REMOVABLE))
    }
}

/// `GET /persona-avatars/<id>`: the picture a persona has of its own, or the part of it a
/// `Range` header asks for.
async fn avatar(State(app): State<App>, Path(id): Path<String>, headers: HeaderMap) -> Response {
    let [roster, cartesia, gemini] = crate::owner::personas(&app.mouth.owners());
    let owner = Owner {
        roster: &roster,
        cartesia: &cartesia,
        gemini: &gemini,
    };
    let directory = app.fleet.directory();
    let Some(digest) = directory.personas.picture_of(&owner, id.trim_matches('/')) else {
        return ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response();
    };
    let range = media::header_text(&headers, "range");
    media::send_kept(&app.blobs, picture_name(&digest), "image/jpeg", &range)
}

/// Where the picture an agent has of its own is fetched from.
pub(crate) const OF_AGENTS: &str = "/avatars";

/// `GET /avatars/<agent>`: the picture an agent has of its own, by the agent's id or its
/// session name, or the part of it a `Range` header asks for.
async fn agent_avatar(
    State(app): State<App>,
    Path(agent): Path<String>,
    headers: HeaderMap,
) -> Response {
    let directory = app.fleet.directory();
    let member = directory.named(agent.trim_matches('/'));
    let Some(digest) = member.and_then(|member| member.record.avatar.clone()) else {
        return ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response();
    };
    let range = media::header_text(&headers, "range");
    media::send_kept(&app.blobs, picture_name(&digest), "image/jpeg", &range)
}

/// The most portraits an agent keeps, as the Python Host has it.
const PORTRAITS_MOST: usize = 3;

/// The id of the portrait that is the picture an agent wears, as the Python Host names one
/// nobody chose from a gallery.
fn worn_id(agent_id: &str, digest: &str) -> String {
    format!(
        "portrait_legacy_{agent_id}_{}",
        digest.get(..24).unwrap_or(digest)
    )
}

/// Whether these bytes begin as a picture a portrait can hold: PNG, JPEG, GIF or WebP.
fn is_portrait(begins: &[u8]) -> bool {
    begins.starts_with(b"\x89PNG\r\n\x1a\n")
        || begins.starts_with(b"\xff\xd8\xff")
        || begins.starts_with(b"GIF87a")
        || begins.starts_with(b"GIF89a")
        || (begins.len() >= 12 && &begins[..4] == b"RIFF" && &begins[8..12] == b"WEBP")
}

/// `GET /agent-portraits?session=`: the portraits an agent keeps. Here that is the picture
/// it wears, when it has one of its own and that is a picture: choosing another from what
/// it published is not built.
async fn portraits(
    State(app): State<App>,
    axum::extract::Query(asked): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let session = asked.get("session").map_or("", |said| said.trim());
    let directory = app.fleet.directory();
    let member = directory.by_session(session);
    let member = member.ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "unknown session"))?;
    let record = &member.record;
    let worn = record.avatar.as_deref().filter(|digest| {
        let begins = app.blobs.read(&picture_name(digest), 0, 12);
        begins.is_ok_and(|begins| is_portrait(&begins))
    });
    let listed: Vec<Value> = worn
        .map(|digest| {
            let id = worn_id(&record.agent_id, digest);
            let version = rules::version(digest);
            json!({
                "portrait_id": id,
                "media_asset_id": null,
                "role": "primary",
                "content_version": version,
                "url": format!("/agent-portraits/{id}/content?v={version}"),
                "source": "legacy_avatar",
                "created_by": "legacy_avatar",
                "created_at": record.created_at_ms,
                "available": true,
            })
        })
        .into_iter()
        .collect();
    Ok(Json(json!({
        "contract": "agent-portraits.v1",
        "session": record.session,
        "agent_id": record.agent_id,
        "primary_portrait_id": listed.first().map(|portrait| portrait["portrait_id"].clone()),
        "portraits": listed,
        "max_portraits": PORTRAITS_MOST,
    })))
}

/// `GET /agent-portraits/<id>/content`: a portrait's picture.
async fn portrait(State(app): State<App>, Path(id): Path<String>, headers: HeaderMap) -> Response {
    let directory = app.fleet.directory();
    let worn = directory.members.iter().find_map(|member| {
        let digest = member.record.avatar.as_deref()?;
        (worn_id(&member.record.agent_id, digest) == id).then(|| digest.to_string())
    });
    let Some(digest) = worn else {
        return ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response();
    };
    let range = media::header_text(&headers, "range");
    media::send_kept(&app.blobs, picture_name(&digest), "image/jpeg", &range)
}

/// `GET /notification-avatars/<agent>?v=&exp=&sig=`: the picture an agent has of its own,
/// for a phone that shows a notification. It needs no token and opens only by an address
/// this Host signed: for this agent, for the picture it has now, and not yet run out.
async fn pushed_face(
    State(app): State<App>,
    Path(agent): Path<String>,
    axum::extract::Query(asked): axum::extract::Query<std::collections::HashMap<String, String>>,
    headers: HeaderMap,
) -> Response {
    let agent = agent.trim_matches('/');
    let directory = app.fleet.directory();
    let member = directory.named(agent);
    let Some(digest) = member.and_then(|member| member.record.avatar.clone()) else {
        return ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response();
    };
    let forbidden = || ApiError::plain(StatusCode::FORBIDDEN, "forbidden").into_response();
    let said = |key: &str| asked.get(key).map_or("", String::as_str);
    let Ok(expires_at) = said("exp").parse::<i64>() else {
        return forbidden();
    };
    let version = rules::version(&digest);
    let signed = (expires_at, said("sig"));
    let now = app.clock.now_ms() / 1_000;
    let good = said("v") == version
        && clarp_kernel::portraits::notification_authorized(
            &app.config.auth_token,
            (agent, version),
            signed,
            now,
        );
    if !good {
        return forbidden();
    }
    let range = media::header_text(&headers, "range");
    media::send_kept(&app.blobs, picture_name(&digest), "image/jpeg", &range)
}

async fn no_portraits() -> ApiError {
    let why = "Choosing a portrait from what an agent published is not built on this Host yet";
    ApiError::new(StatusCode::NOT_IMPLEMENTED, why)
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/agent-portraits", get(portraits).post(no_portraits))
        .route("/agent-portraits/{id}/content", get(portrait))
        .route("/notification-avatars/{agent}", get(pushed_face))
        .route("/personas", post(create))
        .route("/personas/update", post(update))
        .route("/personas/{name}", delete(remove))
        .route("/persona-avatars/{id}", get(avatar))
        .route("/avatars/{agent}", get(agent_avatar))
}
