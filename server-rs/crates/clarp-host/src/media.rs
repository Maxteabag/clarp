//! The files agents publish, as the Host serves them.
//!
//! The rules are in [`clarp_kernel::media`]. An agent's tool sends a file's bytes as they
//! are, with what the file is called in the headers. The Host reads what it is, keeps the
//! bytes under a name made of what they are, and writes the record into the log of the
//! agent it was published for, in that order: a record never names bytes that are not
//! there. The routes answer as the Python Host's do (`server/lib/routes/media.py` and
//! `_handle_media_content` in `server.py`): the same statuses, the same words, the same
//! headers on a file that is fetched.
//!
//! Publishing has two doors with one handler behind them: `/media`, which the
//! administrator's token opens, and `/tools/media`, which the token a turn was given for
//! its agent's tools opens too.

use std::sync::Arc;

use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::media::{self, Asset, Part};
use clarp_ports::blobs::Blobs;
use futures_util::stream;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::edge::{ApiError, App};
use crate::ids;

/// How much of a file is read and sent at a time: what a response holds in memory, however
/// large the file is.
const CHUNK: usize = 256 * 1024;

pub fn routes() -> Router<App> {
    Router::new()
        .route("/media", get(list).post(publish))
        .route("/tools/media", post(publish))
        .route("/media/", get(no_id))
        .route("/media/{*asset_id}", get(content))
}

/// A header's value as text, without the space around it. Bytes that are no plain signs
/// are read one for one, as the Python Host reads a header.
fn header_text(headers: &HeaderMap, name: &str) -> String {
    let value = headers.get(name).map(HeaderValue::as_bytes).unwrap_or(b"");
    let text: String = value.iter().map(|byte| char::from(*byte)).collect();
    text.trim().to_string()
}

fn bad(why: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, why)
}

/// `POST /media`: an agent publishes a file. The body is the file; `X-Session` says whose
/// it is, `X-File-Name` and `X-Caption` what it is called (written with `%XX`),
/// `Content-Type` what it is declared as.
async fn publish(State(app): State<App>, request: Request) -> Result<Json<Value>, ApiError> {
    let (parts, body) = request.into_parts();
    let headers = &parts.headers;
    // How long the file is has to be said before it is read, and within what is taken.
    let length = header_text(headers, "content-length")
        .parse::<usize>()
        .ok()
        .filter(|length| (1..=media::MAX_BYTES).contains(length))
        .ok_or_else(|| bad("bad size"))?;
    let blob = axum::body::to_bytes(body, length)
        .await
        .ok()
        .filter(|blob| blob.len() == length)
        .ok_or_else(|| bad("incomplete body"))?;

    let session = header_text(headers, "x-session");
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let name = media::unquoted(&header_text(headers, "x-file-name"));
    let caption = media::unquoted(&header_text(headers, "x-caption"));
    let created_by = header_text(headers, "x-created-by");
    let member = app
        .member(&session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "unknown session"))?;
    let reading = media::read(&blob, &name, &header_text(headers, "content-type"))
        .map_err(|refused| bad(refused.words()))?;

    // The bytes first, on a thread that may wait for the disk.
    let (blobs, bytes, extension) = (app.blobs.clone(), blob.clone(), reading.extension.clone());
    let kept = tokio::task::spawn_blocking(move || {
        let sha256: String = Sha256::digest(&bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        blobs
            .put(&media::blob_name(&sha256, &extension), &bytes)
            .map(|()| sha256)
    })
    .await;
    let sha256 = match kept {
        Ok(Ok(sha256)) => sha256,
        Ok(Err(error)) => {
            eprintln!("clarp-host: a published file could not be kept: {error}");
            return Err(ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                error.to_string(),
            ));
        }
        Err(error) => {
            eprintln!("clarp-host: keeping a published file was cut off: {error}");
            return Err(ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "the file could not be kept",
            ));
        }
    };
    // Then what is known of it, in the log of the agent it is for.
    let asset = Asset {
        asset_id: format!("asset_{}", ids::secret(18).replace('-', "_")),
        session,
        source_name: reading.name,
        sha256,
        extension: reading.extension,
        mime_type: reading.mime,
        bytes: blob.len() as u64,
        width: reading.width,
        height: reading.height,
        caption: media::kept_caption(&caption),
        created_by: media::kept_publisher(&created_by),
        created_at: app.clock.now_ms(),
    };
    member.agent.publish(asset.clone()).await?;

    // The row twice, as the Python Host answers: under `asset`, and field for field beside it.
    let row = asset.public();
    let mut answer = json!({ "ok": true, "asset": row });
    if let (Some(answer), Some(fields)) = (answer.as_object_mut(), row.as_object()) {
        answer.extend(fields.clone());
    }
    Ok(Json(answer))
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    session: Option<String>,
    limit: Option<String>,
}

/// `GET /media?session=...`: an agent's gallery, the images it published, the newest
/// first. An agent the Host does not have has an empty one.
async fn list(State(app): State<App>, Query(query): Query<ListQuery>) -> Json<Value> {
    let session = query.session.unwrap_or_default().trim().to_string();
    let limit = media::page_size(query.limit.as_deref());
    let assets: Vec<Value> = app.member(&session).map_or_else(Vec::new, |member| {
        let view = member.agent.view();
        let images = view.books.media.images(limit);
        images.into_iter().map(Asset::public).collect()
    });
    Json(json!({ "session": session, "assets": assets }))
}

/// The published file with this id, whichever agent published it.
fn published(app: &App, asset_id: &str) -> Option<Asset> {
    let directory = app.fleet.directory();
    directory
        .members
        .iter()
        .find_map(|member| member.agent.view().books.media.get(asset_id).cloned())
}

fn not_found() -> Response {
    ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response()
}

async fn no_id() -> Response {
    ApiError::plain(StatusCode::BAD_REQUEST, "bad asset id").into_response()
}

/// What a browser may do with a file an agent made: show it as what the Host says it is,
/// and run nothing in it.
fn guarded(mut response: Response) -> Response {
    let headers = response.headers_mut();
    headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static("sandbox; default-src 'none'"),
    );
    response
}

/// `GET /media/<id>`: the file's bytes, or the part of them a `Range` header asks for. A
/// player asks for a recording in parts.
async fn content(
    State(app): State<App>,
    Path(asset_id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let asset_id = asset_id.trim_matches('/');
    if asset_id.is_empty() || asset_id.contains('/') {
        return no_id().await;
    }
    let Some(asset) = published(&app, asset_id) else {
        return not_found();
    };
    let name = asset.blob();
    let total = match app.blobs.len(&name) {
        Ok(Some(total)) => total,
        Ok(None) => return not_found(),
        Err(error) => {
            eprintln!("clarp-host: a published file could not be read: {error}");
            return not_found();
        }
    };
    let (status, first, last) = match media::byte_range(&header_text(&headers, "range"), total) {
        Part::Missing => {
            let range = format!("bytes */{total}");
            let said = [
                (header::CONTENT_TYPE, "text/plain".to_string()),
                (header::CONTENT_RANGE, range),
            ];
            return guarded((StatusCode::RANGE_NOT_SATISFIABLE, said).into_response());
        }
        Part::Whole => (StatusCode::OK, 0, total.saturating_sub(1)),
        Part::Bytes { first, last } => (StatusCode::PARTIAL_CONTENT, first, last),
    };
    let length = if total == 0 { 0 } else { last - first + 1 };
    let mut said = vec![
        (header::CONTENT_TYPE, asset.mime_type.clone()),
        (header::CONTENT_LENGTH, length.to_string()),
    ];
    if status == StatusCode::PARTIAL_CONTENT {
        let range = format!("bytes {first}-{last}/{total}");
        said.push((header::CONTENT_RANGE, range));
    }
    let mut response = Response::new(parts_of(app.blobs.clone(), name, first, length));
    *response.status_mut() = status;
    for (name, value) in said {
        if let Ok(value) = HeaderValue::from_str(&value) {
            response.headers_mut().insert(name, value);
        }
    }
    guarded(response)
}

/// A file's bytes from `first` on, `length` of them, read and sent a part at a time.
fn parts_of(blobs: Arc<dyn Blobs>, name: String, first: u64, length: u64) -> Body {
    let parts = stream::unfold((first, length), move |(at, left)| {
        let (blobs, name) = (blobs.clone(), name.clone());
        async move {
            if left == 0 {
                return None;
            }
            let most = usize::try_from(left).map_or(CHUNK, |left| left.min(CHUNK));
            let read = tokio::task::spawn_blocking(move || blobs.read(&name, at, most)).await;
            match read {
                Ok(Ok(part)) if !part.is_empty() => {
                    let sent = part.len() as u64;
                    let rest = (at + sent, left.saturating_sub(sent));
                    Some((Ok(Bytes::from(part)), rest))
                }
                // The file is shorter than it was, or cannot be read: the answer ends short
                // of the length it promised, and the connection ends with it.
                _ => {
                    let short = std::io::Error::other("a published file ended early");
                    Some((Err(short), (at, 0)))
                }
            }
        }
    });
    Body::from_stream(parts)
}
