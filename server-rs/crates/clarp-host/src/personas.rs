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
use clarp_kernel::kept_portraits::{self as kept, Portrait};
use clarp_kernel::personas::{self as rules, Owner};
use clarp_kernel::python;
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::{AgentRecord, Amend, PersonaSaid, picture_name};
use crate::{ids, media};

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
        base_voice_id: fields
            .get("base_voice_id")
            .and_then(Value::as_str)
            .map(str::to_string),
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
    // A persona made without a picture and without a symbol is asked one, unless the request
    // says not to (`generate_avatar: false`); nothing waits for it.
    let drawn = asked.get("generate_avatar") != Some(&Value::Bool(false));
    let bare = persona["avatar_url"].as_str().unwrap_or("").is_empty()
        && persona["avatar_symbol"].as_str().unwrap_or("").is_empty();
    if drawn && bare {
        let name = persona["name"].as_str().unwrap_or("").to_string();
        let tier = text(&asked, "tier");
        let backend = text(&asked, "backend");
        let app = app.clone();
        tokio::spawn(async move { crate::pictures::ask(&app, &name, &tier, &backend).await });
    }
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

/// The picture an agent wears, as a portrait can hold it: its digest, and what kind of
/// picture its bytes begin as.
fn worn(app: &App, record: &AgentRecord) -> Option<(String, &'static str)> {
    let digest = record.avatar.as_deref()?;
    let begins = app.blobs.read(&picture_name(digest), 0, 12).ok()?;
    kept::picture_kind(&begins).map(|kind| (digest.to_string(), kind))
}

/// Whether a portrait's bytes are kept. Every portrait's are kept under the name a picture
/// of an agent is kept by, whatever file it was chosen from.
fn there(app: &App) -> impl Fn(&Portrait) -> bool + '_ {
    |portrait| matches!(app.blobs.len(&picture_name(&portrait.sha256)), Ok(Some(_)))
}

/// If storage failed between the durable pair decision and filling the blob store,
/// a reader must not mistake the first available alternative for the whole gallery.
/// Missing legacy files keep their usual projection once there is no pending publication.
pub(crate) async fn gallery_ready(
    fleet: &crate::fleet::FleetHandle,
    blobs: &dyn clarp_ports::blobs::Blobs,
    record: &AgentRecord,
) -> Result<(), ApiError> {
    for portrait in &record.portraits {
        if portrait.created_by == "portrait_generation:openai"
            && !matches!(blobs.len(&picture_name(&portrait.sha256)), Ok(Some(_)))
            && let Some(id) = &portrait.media_asset_id
            && fleet
                .kept_book(crate::fleet::portrait_jobs::BYTES, id.clone())
                .await?
                .is_some()
        {
            return Err(ApiError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "Portraits are being published",
            ));
        }
    }
    Ok(())
}

/// What the apps are told of an agent's portraits, as its record has them now.
fn told(app: &App, record: &AgentRecord, portraits: &[Portrait]) -> Value {
    let worn = worn(app, record);
    let digest = worn.as_ref().map(|(digest, _)| digest.as_str());
    let whose = (record.session.as_str(), record.agent_id.as_str());
    kept::told(whose, portraits, digest, there(app))
}

fn declined(why: kept::Refused) -> ApiError {
    let status = StatusCode::from_u16(why.status).unwrap_or(StatusCode::BAD_REQUEST);
    ApiError::new(status, why.why)
}

/// `GET /agent-portraits?session=`: the portraits an agent keeps, the one it wears first.
/// Reading changes nothing: they are shown as the next change will find them.
async fn portraits(
    State(app): State<App>,
    axum::extract::Query(asked): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let session = asked.get("session").map_or("", |said| said.trim());
    let directory = app.fleet.directory();
    let member = directory.by_session(session);
    let record = &member
        .ok_or_else(|| declined(kept::UNKNOWN_SESSION))?
        .record;
    gallery_ready(&app.fleet, app.blobs.as_ref(), record).await?;
    let worn = worn(&app, record);
    let wears = worn.as_ref().map(|(digest, kind)| (digest.as_str(), *kind));
    let made = (record.agent_id.as_str(), record.created_at_ms);
    let shown = kept::as_read(&record.portraits, made, wears, there(&app));
    Ok(Json(told(&app, record, &shown)))
}

/// `GET /agent-portraits/<id>/content`: a portrait's picture. One that a change recorded
/// is handed out as the kind of picture it is; the picture an agent wears that nothing
/// has recorded yet as the file an agent's picture is kept as.
async fn portrait(State(app): State<App>, Path(id): Path<String>, headers: HeaderMap) -> Response {
    let directory = app.fleet.directory();
    let range = media::header_text(&headers, "range");
    for member in &directory.members {
        let record = &member.record;
        let recorded = record.portraits.iter();
        if let Some(found) = recorded.into_iter().find(|kept| kept.portrait_id == id) {
            if let Err(why) = gallery_ready(&app.fleet, app.blobs.as_ref(), record).await {
                return why.into_response();
            }
            let name = picture_name(&found.sha256);
            return media::send_kept(&app.blobs, name, &found.mime_type, &range);
        }
        if let Some(digest) = record.avatar.as_deref()
            && kept::worn_id(&record.agent_id, digest) == id
        {
            return media::send_kept(&app.blobs, picture_name(digest), "image/jpeg", &range);
        }
    }
    ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response()
}

/// `POST /agent-portraits` with `{action, session, ...}`: a picture the agent published is
/// kept as one of its portraits (`add_media_asset` with `asset_id`), or one of its
/// portraits is made the picture it wears (`select_primary` with `portrait_id`). Every
/// change first finds the picture the agent wears among its portraits, or records it. The
/// rules and the words are the Python Host's (`clarp_kernel::kept_portraits`).
async fn change_portraits(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    // Nothing at all is an empty object, as on the Python Host.
    let said = match serde_json::from_slice::<Value>(&body) {
        _ if body.is_empty() => json!({}),
        Ok(said @ Value::Object(_)) => said,
        _ => return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json")),
    };
    let text = |key: &str| said[key].as_str().map(python::strip).unwrap_or_default();
    let action = text("action");
    if !matches!(action, "add_media_asset" | "select_primary") {
        return Err(declined(kept::UNSUPPORTED));
    }
    let session = match text("session") {
        "" => clarp_kernel::addressed::DEFAULT_SESSION,
        session => session,
    };
    let _gallery = crate::portrait_generation::GALLERY_WRITE.lock().await;
    let directory = app.fleet.directory();
    let member = directory.by_session(session).cloned();
    let member = member.ok_or_else(|| declined(kept::UNKNOWN_SESSION))?;
    let record = &member.record;
    gallery_ready(&app.fleet, app.blobs.as_ref(), record).await?;
    let worn = worn(&app, record);
    let wears = worn.as_ref().map(|(digest, kind)| (digest.as_str(), *kind));
    let made = (record.agent_id.as_str(), record.created_at_ms);
    let mut rows = record.portraits.clone();
    kept::before_a_change(&mut rows, made, wears, there(&app)).map_err(declined)?;
    let now_worn = if action == "add_media_asset" {
        let asset_id = text("asset_id");
        let view = member.agent.view();
        let own = view
            .books
            .media
            .get(asset_id)
            .filter(|asset| !asset.named_only);
        let Some(asset) = own.cloned() else {
            let elsewhere = !media::kept_under(&app, asset_id).is_empty();
            return Err(declined(if elsewhere {
                kept::ANOTHERS_ASSET
            } else {
                kept::NO_SUCH_ASSET
            }));
        };
        // Its bytes are kept once more under the name an agent's picture is kept by, so
        // that a portrait is found there whatever file it was chosen from.
        let to = picture_name(&asset.sha256);
        let id = asset.asset_id.clone();
        let size = usize::try_from(asset.bytes).unwrap_or(usize::MAX);
        let blobs = app.blobs.clone();
        let copied = app
            .gallery
            .ask(move |store| {
                let bytes = store.read(&id, 0, size)?;
                blobs.put(&to, &bytes)?;
                Ok(())
            })
            .await;
        if let Err(why) = copied {
            return Err(match why {
                crate::gallery::Failure::Busy
                | crate::gallery::Failure::Stopped
                | crate::gallery::Failure::TimedOut => {
                    ApiError::new(StatusCode::SERVICE_UNAVAILABLE, why.words())
                }
                _ => declined(kept::ASSET_GONE),
            });
        }
        let published = Portrait {
            portrait_id: format!("portrait_{}", ids::secret(18).replace('-', "_")),
            media_asset_id: Some(asset.asset_id.clone()),
            sha256: asset.sha256.clone(),
            mime_type: asset.mime_type.clone(),
            created_by: match asset.created_by.as_str() {
                "" => "agent".to_string(),
                by => by.to_string(),
            },
            created_at: asset.created_at,
        };
        kept::keep(&mut rows, published).map_err(declined)?;
        None
    } else {
        let chosen = kept::chosen(&rows, text("portrait_id")).map_err(declined)?;
        if !there(&app)(chosen) {
            return Err(declined(kept::PORTRAIT_GONE));
        }
        Some(chosen.sha256.clone())
    };
    let selected = now_worn.is_some();
    let change = Amend::Portraits {
        kept: rows,
        worn: now_worn,
    };
    let amended = app.fleet.amend(record.agent_id.clone(), change).await?;
    let record = amended.map_err(|_| declined(kept::UNKNOWN_SESSION))?;
    if selected {
        // The agent wears another picture: the apps read their lists again.
        let news = json!({ "type": "agent-roster", "kind": "portrait-selected", "session": record.session });
        app.hub.publish(news, app.clock.now_ms());
    }
    Ok(Json(told(&app, &record, &record.portraits)))
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

async fn avatar_settings(State(app): State<App>) -> Json<Value> {
    use clarp_kernel::portraits::{MODEL_SETTING, model_avatars};
    Json(json!({"model_avatars":model_avatars(app.fleet.directory().setting(MODEL_SETTING))}))
}

async fn set_avatar_settings(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let value = serde_json::from_slice::<Value>(&body).ok();
    let on = value
        .as_ref()
        .and_then(|v| v["model_avatars"].as_bool())
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "model_avatars required"))?;
    app.fleet
        .set(vec![(
            clarp_kernel::portraits::MODEL_SETTING.to_owned(),
            on.to_string(),
        )])
        .await?;
    Ok(Json(json!({"model_avatars":on})))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route(
            "/avatar-settings",
            get(avatar_settings).post(set_avatar_settings),
        )
        .route("/agent-portraits", get(portraits).post(change_portraits))
        .route("/agent-portraits/{id}/content", get(portrait))
        .route("/notification-avatars/{agent}", get(pushed_face))
        .route("/personas", post(create))
        .route("/personas/update", post(update))
        .route("/personas/{name}", delete(remove))
        .route("/persona-avatars/{id}", get(avatar))
        .route("/avatars/{agent}", get(agent_avatar))
}
