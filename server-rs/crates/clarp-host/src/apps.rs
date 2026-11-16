//! What an app asks of any Host, whatever the Host says it has.
//!
//! An app gates few of its calls on what a Host lists (`capabilities.features`): the rest it
//! makes of every Host it is paired with. Counted on the Python Host's own log over three
//! days, three in ten of all requests were to routes this Host did not have, and most of
//! them were these: which agent the user is looking at, whether the app is in front, and
//! the lists an app's screens are made of. They are answered here: with what is so, where
//! this Host keeps it, and with a list that is empty where it keeps nothing of the kind.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use std::collections::HashMap;

use axum::extract::{Extension, Query, Request, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::media;
use clarp_kernel::told::{self, Humanness};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::edge::{ApiError, App, Principal};
use crate::ids;
use crate::media::header_text;
use crate::pairing::Scope;

fn bad(message: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, message)
}

/// `POST /focus` with `{session}`: the app says which agent its user is looking at. An agent
/// nobody has is nothing to look at, and no error either.
async fn focus(State(app): State<App>, body: axum::body::Bytes) -> Result<Json<Value>, ApiError> {
    let Ok(Value::Object(said)) = serde_json::from_slice::<Value>(&body) else {
        return Err(bad("bad json"));
    };
    let session = said.get("session").and_then(Value::as_str).unwrap_or("");
    let session = session.trim();
    if session.is_empty() {
        return Err(bad("session required"));
    }
    // Nobody is told: the app that says it is the one that knows.
    app.fleet.open(session.to_string()).await?;
    Ok(Json(json!({ "ok": true })))
}

/// `POST /application-activity`: an app reports whether it is in front and when its user
/// last touched it. Only for whoever may speak for a person: the administrator, or a device
/// with full scope.
async fn activity(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    if app.config.auth_token.is_empty() {
        return Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "authentication required",
        ));
    }
    let owner = match &principal {
        Principal::Administrator => "administrator".to_string(),
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => device_id.clone(),
        _ => {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "full device access required",
            ));
        }
    };
    let Ok(report @ Value::Object(_)) = serde_json::from_slice::<Value>(&body) else {
        return Err(bad("object required"));
    };
    let now = app.clock.now_ms();
    let taken = match app.activity.lock() {
        Ok(mut leases) => leases.report(&owner, &report, now),
        Err(_) => Err("activity is not kept right now".to_string()),
    };
    match taken {
        Ok(accepted) => Ok(Json(json!({ "accepted": accepted }))),
        Err(why) => Err(bad(&why)),
    }
}

/// The most the log of the apps' own lines may weigh before the older half of it is put
/// aside (`apps.log.1`, which the next time replaces).
const APP_LOG_MAX: u64 = 16 * 1024 * 1024;

/// One line at a time is written to the log of the apps.
static APP_LOG: Mutex<()> = Mutex::new(());

/// Writes lines to the log of the apps, under the data directory.
fn logged(directory: &Path, lines: &str) -> std::io::Result<()> {
    let _one_at_a_time = APP_LOG.lock();
    std::fs::create_dir_all(directory)?;
    let path = directory.join("apps.log");
    if std::fs::metadata(&path).is_ok_and(|kept| kept.len() > APP_LOG_MAX) {
        std::fs::rename(&path, directory.join("apps.log.1"))?;
    }
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)?;
    file.write_all(lines.as_bytes())
}

/// `POST /clog`: an app hands over lines of its own log, one (`{event, detail, ...}`) or
/// several (`{events: [...]}`). They are what says why something did not work on a phone,
/// so they are kept: in one file under the data directory, which does not grow without end.
/// An app keeps its lines and sends them again until a Host has taken them.
async fn app_log(State(app): State<App>, body: axum::body::Bytes) -> Result<Json<Value>, ApiError> {
    let Ok(said @ Value::Object(_)) = serde_json::from_slice::<Value>(&body) else {
        return Err(bad("bad json"));
    };
    let several = said.get("events").and_then(Value::as_array);
    let events: Vec<&Value> = match several {
        Some(events) => events.iter().collect(),
        None => vec![&said],
    };
    let at = crate::wire::iso(app.clock.now_ms());
    let mut lines = String::new();
    for event in events {
        let name = event.get("event").and_then(Value::as_str).unwrap_or("?");
        let name: String = name.chars().take(64).collect();
        let detail = match event.get("detail") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(text)) => text.clone(),
            Some(other) => other.to_string(),
        };
        // One event, one line: what an app says is not let break it.
        let line = format!("{at} app {name} {detail}");
        lines.push_str(&line.replace(['\n', '\r'], " "));
        lines.push('\n');
    }
    let directory = app.config.data_dir.join("logs");
    let kept = tokio::task::spawn_blocking(move || logged(&directory, &lines)).await;
    match kept {
        Ok(Ok(())) => Ok(Json(json!({ "ok": true }))),
        // Not kept: said so, and the app keeps its lines.
        _ => Ok(Json(json!({ "ok": true, "captured": false }))),
    }
}

/// The most a file a user attaches may weigh.
const UPLOAD_MAX: usize = 50 * 1024 * 1024;

/// One upload at a time is looked up and kept under its id: the same file sent twice at
/// once is kept once.
static UPLOADS: Mutex<()> = Mutex::new(());

/// Why a file a user attached was not kept.
enum NotKept {
    /// The id it came under is another file's.
    Another(&'static str),
    Disk(std::io::Error),
}

impl From<std::io::Error> for NotKept {
    fn from(error: std::io::Error) -> Self {
        Self::Disk(error)
    }
}

fn hex(digest: &[u8]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Writes a file whole or not at all: beside its place first, then moved into it.
fn written_whole(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("upload");
    let beside = path.with_file_name(format!(".{name}.{}", ids::hex(6)));
    let done = (|| {
        let mut file = std::fs::File::create(&beside)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&beside, path)
    })();
    if done.is_err() {
        let _ = std::fs::remove_file(&beside);
    }
    done
}

/// Keeps a file a user attached in `directory`, which is under `root`. Under an id it is
/// kept once: sent again as it was, it is the file that came before. The rules, the names
/// on the disk and the words are the Python Host's (`server/lib/upload_results.py`).
fn kept(
    root: &Path,
    directory: &Path,
    id: Option<&str>,
    name: &str,
    content_type: &str,
    blob: &[u8],
) -> Result<PathBuf, NotKept> {
    std::fs::create_dir_all(directory)?;
    let directory = std::fs::canonicalize(directory)?;
    let Some(id) = id else {
        // No id: every request keeps a new file.
        let path = directory.join(format!("{}-{name}", ids::hex(4)));
        std::fs::write(&path, blob)?;
        return Ok(path);
    };
    let _one_at_a_time = UPLOADS.lock();
    let key = hex(&Sha256::digest(id.as_bytes()));
    let sha256 = hex(&Sha256::digest(blob));
    let records = root.join(".upload-records");
    std::fs::create_dir_all(&records)?;
    let record_path = records.join(format!("{key}.json"));
    let said = |record: &Value, field: &str| record[field].as_str().unwrap_or("").to_string();
    if record_path.exists() {
        let read = std::fs::read(&record_path).ok();
        let record = read.and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        let Some(record) = record else {
            return Err(NotKept::Another(
                "upload id has an unreadable existing record",
            ));
        };
        let same = said(&record, "upload_id") == id
            && said(&record, "session_dir") == directory.to_string_lossy()
            && said(&record, "name") == name
            && said(&record, "content_type") == content_type
            && said(&record, "sha256") == sha256
            && record["size"].as_u64() == u64::try_from(blob.len()).ok();
        if !same {
            return Err(NotKept::Another("upload id reused for different content"));
        }
        let existing = PathBuf::from(said(&record, "path"));
        if std::fs::read(&existing).ok().as_deref() != Some(blob) {
            return Err(NotKept::Another(
                "upload id record does not match the stored file",
            ));
        }
        return Ok(existing);
    }
    // A name that begins with what the id gives is this upload's: one that was kept before
    // its record was written is found again by it.
    let prefix = format!("u-{}-", &key[..24]);
    let expected = directory.join(format!("{prefix}{name}"));
    let mut there: Vec<PathBuf> = Vec::new();
    for entry in std::fs::read_dir(&directory)? {
        let path = entry?.path();
        let named = path.file_name().and_then(|name| name.to_str());
        if named.is_some_and(|named| named.starts_with(&prefix)) {
            there.push(path);
        }
    }
    match there.as_slice() {
        [] => written_whole(&expected, blob)?,
        [one] if *one == expected && std::fs::read(one).ok().as_deref() == Some(blob) => {}
        _ => return Err(NotKept::Another("upload id reused for different content")),
    }
    // Its fields in the order of their names, as the Python Host writes a record.
    let record = json!({
        "content_type": content_type, "name": name, "path": expected.to_string_lossy(),
        "session_dir": directory.to_string_lossy(), "sha256": sha256, "size": blob.len(),
        "upload_id": id,
    });
    written_whole(
        &record_path,
        clarp_kernel::python::canonical(&record).as_bytes(),
    )?;
    Ok(expected)
}

/// `POST /upload`: a user attaches a file to what they say. Its bytes are the body; the
/// agent it is for, its name and the id the app gave it are in headers. It is kept on this
/// machine and answered with where: the app puts that into the message, and the agent's
/// tool reads the file from there.
async fn upload(State(app): State<App>, request: Request) -> Result<Json<Value>, ApiError> {
    let (parts, body) = request.into_parts();
    let headers = &parts.headers;
    let length = header_text(headers, "content-length")
        .parse::<usize>()
        .ok()
        .filter(|length| (1..=UPLOAD_MAX).contains(length))
        .ok_or_else(|| bad("bad size"))?;
    let blob = axum::body::to_bytes(body, length)
        .await
        .ok()
        .filter(|blob| blob.len() == length)
        .ok_or_else(|| ApiError::new(StatusCode::REQUEST_TIMEOUT, "incomplete upload"))?;
    // Which agent it is for: the one that is named, else the one the user is looking at.
    let mut session = header_text(headers, "x-session");
    if session.is_empty() {
        let directory = app.fleet.directory();
        let looked_at = directory.focus.as_deref().and_then(|focus| {
            let members = directory.members.iter();
            members
                .map(|member| &member.record)
                .find(|record| record.agent_id == focus)
        });
        session = looked_at.map_or_else(String::new, |record| record.session.clone());
    }
    let content_type = header_text(headers, "content-type");
    let name = media::upload_name(
        &media::unquoted(&header_text(headers, "x-file-name")),
        &content_type,
    );
    let given = header_text(headers, "x-upload-id");
    let id = match (given.is_empty(), media::upload_id(&given)) {
        (true, _) => None,
        (false, Some(id)) => Some(id.to_string()),
        (false, None) => return Err(bad("invalid upload id")),
    };
    let root = app.config.data_dir.join("uploads");
    let directory = root.join(media::upload_directory(&session));
    let (name_kept, size) = (name.clone(), blob.len());
    let done = tokio::task::spawn_blocking(move || {
        kept(
            &root,
            &directory,
            id.as_deref(),
            &name_kept,
            &content_type,
            &blob,
        )
    })
    .await;
    let path = match done {
        Ok(Ok(path)) => path,
        Ok(Err(NotKept::Another(why))) => return Err(ApiError::new(StatusCode::CONFLICT, why)),
        Ok(Err(NotKept::Disk(error))) => {
            eprintln!("clarp-host: a file a user attached could not be kept: {error}");
            return Err(ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                error.to_string(),
            ));
        }
        Err(error) => {
            eprintln!("clarp-host: keeping a file a user attached was cut off: {error}");
            return Err(ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "the file could not be kept",
            ));
        }
    };
    Ok(Json(json!({
        "ok": true, "path": path.to_string_lossy(), "name": name, "size": size,
        "session": session,
    })))
}

/// `GET /status?session=`: whether the Host is up, and whether an agent is in a turn. What
/// runs the turns is this one process: it is there when this answers.
async fn status(
    State(app): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Json<Value> {
    let session = query.get("session").map_or("", |given| given.trim());
    let directory = app.fleet.directory();
    let in_a_turn = |session: &str| {
        let member = directory.by_session(session);
        member.is_some_and(|member| member.agent.view().status.live.is_some())
    };
    let sessions = directory
        .members
        .iter()
        .map(|member| &member.record.session);
    let active = sessions.filter(|session| in_a_turn(session)).count();
    // What an installer stamped this installation with; a Host run from a build has none.
    let build = match app.config.deployed_version.as_str() {
        "" => "development",
        stamped => stamped,
    };
    Json(json!({
        "session": session,
        "busy": in_a_turn(session),
        "runtime": {
            "available": true, "reason": "", "release_id": "embedded", "fenced": false,
            "sealed": false, "draining": false, "drain": { "phase": "idle" },
            "active_turns": active, "observed_at": app.clock.now_ms(),
        },
        "deployed_version": build,
        "release_id": build,
    }))
}

/// The lists an app's screens are made of, for what this Host keeps nothing of: said as
/// the Python Host says a list, and empty. An app that reads them shows nothing there,
/// where an answer of "no such route" would be an error on its screen.
///
/// Teams, janitors and what the Host does by itself are not built, and no voice speaks
/// here yet (what listens is in `heard.rs`). Which models an agent can be given is a
/// catalogue this Host does not keep yet; an agent's model is set by name (`/agent-llm`).
async fn teams() -> Json<Value> {
    Json(json!({ "teams": [] }))
}

async fn janitors() -> Json<Value> {
    Json(json!({
        "janitors": [], "providers": [], "runtime_available": false, "templates": [],
        "triggers": [],
    }))
}

/// `GET /controller-actions`: what a button of a controller (a Flic, the Watch) can run
/// instead of what it does by itself, and which of its gestures runs what
/// (`docs/controller-actions.md`). An app asks this of every Host again and again. Nothing
/// can be defined on this Host yet, so there is nothing to run and nothing mapped: the
/// answer is the Python Host's for a Host where nobody defined anything, with the kinds of
/// action, the buttons and the gestures that Host knows.
async fn controller_actions() -> Json<Value> {
    Json(json!({
        "revision": 0,
        "actions": [],
        "mappings": [],
        "types": ["http_post", "agent_prompt"],
        "buttons": ["primary", "secondary"],
        "gestures": [
            "double-click", "hold", "rotate-clockwise", "rotate-counterclockwise", "single-click",
            "swipe-down", "swipe-left", "swipe-right", "swipe-up",
        ],
    }))
}

/// `GET /controller-actions/audit?limit=`: what was defined and run, newest first. Nothing
/// was, here.
async fn controller_audit(
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let limit = query.get("limit").map_or("", |limit| limit.as_str());
    // A limit is a whole number, as Python reads one: an empty one is the usual fifty.
    if !limit.is_empty() && clarp_kernel::python::int(limit).is_none() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "limit must be an integer",
        ));
    }
    Ok(Json(json!({ "entries": [] })))
}

/// Defining an action, mapping a gesture to one and running one are not built.
async fn no_controller_actions() -> ApiError {
    ApiError::new(
        StatusCode::NOT_IMPLEMENTED,
        "controller actions are not built on this Host yet",
    )
}

/// Where the owner's choice of how much a tool's use is explained is kept.
const DETAIL_LEVEL_KEY: &str = "tool_explanations.detail_level";
/// What it is where nobody chose: balanced.
const DETAIL_LEVEL: i64 = 2;

/// Whether the use of a tool is explained in plain words on its row, and for which reader
/// (`docs/live-items.md`, section 6). Nothing explains here yet, so it is off whatever was
/// asked for: an app then shows the tool's own command on the row, which is there. The
/// level is kept for the day something explains.
pub(crate) fn explanations_told(app: &App) -> Value {
    let directory = app.fleet.directory();
    let kept = directory.setting(DETAIL_LEVEL_KEY);
    let level = kept
        .and_then(|level| level.trim().parse::<i64>().ok())
        .map_or(DETAIL_LEVEL, |level| level.clamp(0, 4));
    json!({ "enabled": false, "detail_level": level })
}

async fn explanations(State(app): State<App>) -> Json<Value> {
    Json(explanations_told(&app))
}

/// `POST /tool-explanations/settings`: a change of a part of it. The rules and the words a
/// change is refused in are the Python Host's; turning on what is not built is refused,
/// and says so.
async fn set_explanations(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let bad = |why: &str| ApiError::new(StatusCode::BAD_REQUEST, why);
    let Ok(Value::Object(changes)) = serde_json::from_slice::<Value>(&body) else {
        return Err(bad("object required"));
    };
    match changes.get("enabled") {
        None | Some(Value::Bool(false)) => {}
        Some(Value::Bool(true)) => {
            return Err(bad("tool explanations are not built on this Host yet"));
        }
        Some(_) => return Err(bad("enabled must be true or false")),
    }
    if let Some(level) = changes.get("detail_level") {
        // A whole number from 0 to 4, and nothing that only reads like one.
        let whole = level.as_i64().filter(|level| (0..=4).contains(level));
        let Some(level) = whole else {
            return Err(bad("detail_level must be an integer from 0 to 4"));
        };
        let kept = vec![(DETAIL_LEVEL_KEY.to_string(), level.to_string())];
        app.fleet.set(kept).await?;
    }
    Ok(Json(explanations_told(&app)))
}

/// Whether agents are told how they speak, as an app is told it.
fn personalities_told(app: &App) -> Value {
    let directory = app.fleet.directory();
    let enabled = crate::telling::personalities(directory.setting(told::PERSONALITIES_KEY));
    json!({ "enabled": enabled })
}

/// `GET /personalities/settings`: whether an agent's tool is told how the agent speaks, or
/// its name alone.
async fn personalities(State(app): State<App>) -> Json<Value> {
    Json(json!({ "settings": personalities_told(&app) }))
}

/// `POST /personalities/settings` with `{enabled}`: turns it on or off, for every agent's
/// next turn. Anything that counts as true in Python turns it on, as there.
async fn set_personalities(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let given = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null)
    };
    let Value::Object(changes) = given else {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    };
    if let Some(enabled) = changes.get("enabled") {
        let kept = if clarp_kernel::live::truthy(enabled) {
            "1"
        } else {
            "0"
        };
        let kept = vec![(told::PERSONALITIES_KEY.to_string(), kept.to_string())];
        app.fleet.set(kept).await?;
    }
    Ok(Json(
        json!({ "ok": true, "settings": personalities_told(&app) }),
    ))
}

/// How human voices sound on this Host, as the owner's settings keep it.
fn humanness_kept(app: &App) -> Humanness {
    let directory = app.fleet.directory();
    Humanness::stored(
        directory.setting(told::HUMANNESS_KEY),
        directory.setting(told::HUMANNESS_OF_AGENTS_KEY),
    )
}

/// `GET /voice-humanness`: how human spoken answers sound, from 0 to 10: the Host's level
/// and the levels single agents have of their own.
async fn humanness(State(app): State<App>) -> Json<Value> {
    Json(humanness_kept(&app).told())
}

/// `POST /voice-humanness` with `{default?, agents?: {name: level or nothing}}`: a change
/// of it, for every spoken turn that starts afterwards. The rules and the words a change
/// is refused in are the Python Host's.
async fn set_humanness(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    // No body is an empty change; what is no JSON is refused as what is no object.
    let given = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null)
    };
    let after = humanness_kept(&app)
        .changed(&given)
        .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let (usual, of_agents) = after.kept();
    let kept = vec![
        (told::HUMANNESS_KEY.to_string(), usual),
        (told::HUMANNESS_OF_AGENTS_KEY.to_string(), of_agents),
    ];
    app.fleet.set(kept).await?;
    Ok(Json(humanness_kept(&app).told()))
}

/// `GET /dirs?path=`: the directories a path that is being typed can go on with, for the
/// field an app has for where an agent works.
async fn dirs(Query(query): Query<HashMap<String, String>>) -> Json<Value> {
    let asked = query.get("path").cloned().unwrap_or_default();
    let looked = move || crate::config::directories(&asked, &crate::config::home());
    let (base, matches) = tokio::task::spawn_blocking(looked)
        .await
        .unwrap_or_default();
    Json(json!({ "base": base, "matches": matches }))
}

async fn automation_settings() -> Json<Value> {
    Json(json!({ "special_treatment": false }))
}

async fn model_options(State(app): State<App>) -> Json<Value> {
    Json(json!({
        "schema_version": 2,
        "observed_at": crate::wire::iso(app.clock.now_ms()),
        "freshness": "fresh",
        "freshness_note": "This Host keeps no catalogue of models yet.",
        "freshness_scope": "response_observation",
        "providers": {},
    }))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/status", get(status))
        .route("/focus", post(focus))
        .route("/application-activity", post(activity))
        .route("/upload", post(upload))
        .route("/clog", post(app_log))
        .route("/teams", get(teams))
        .route("/janitors", get(janitors))
        .route("/automation-settings", get(automation_settings))
        .route("/dirs", get(dirs))
        .route(
            "/personalities/settings",
            get(personalities).post(set_personalities),
        )
        .route("/voice-humanness", get(humanness).post(set_humanness))
        .route("/agent-model-options", get(model_options))
        .route(
            "/tool-explanations/settings",
            get(explanations).post(set_explanations),
        )
        .route(
            "/controller-actions",
            get(controller_actions).post(no_controller_actions),
        )
        .route("/controller-actions/audit", get(controller_audit))
        .route("/controller-actions/{*rest}", post(no_controller_actions))
}
