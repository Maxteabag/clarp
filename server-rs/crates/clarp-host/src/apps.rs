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

use axum::extract::{Extension, Request, State};
use axum::http::StatusCode;
use axum::routing::post;
use axum::{Json, Router};
use clarp_kernel::media;
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
    app.fleet.select(session.to_string()).await?;
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

pub fn routes() -> Router<App> {
    Router::new()
        .route("/focus", post(focus))
        .route("/application-activity", post(activity))
        .route("/upload", post(upload))
}
