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
fn object(body: &Bytes) -> Result<Map<String, Value>, ApiError> {
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

pub fn routes() -> Router<App> {
    Router::new()
        .route("/personas", post(create))
        .route("/personas/update", post(update))
        .route("/personas/{name}", delete(remove))
        .route("/persona-avatars/{id}", get(avatar))
}
