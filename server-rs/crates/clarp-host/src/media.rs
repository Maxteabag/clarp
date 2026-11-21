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
//!
//! A file that moves here with its agent keeps the id it had on the Host it came from
//! (`X-Asset-Id`, with `X-Created-At`): the agent's conversation names it by that id. Only
//! the administrator says so, and the same file under the same id is taken once, however
//! often it comes.

use std::sync::Arc;

use axum::body::{Body, Bytes};
use axum::extract::{Extension, Path, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::gallery::{Image, Refusal};
use clarp_kernel::media::{self, Asset, Part};
use clarp_ports::blobs::Blobs;
use futures_util::stream;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::Member;
use crate::ids;
use crate::media_manage::{self, Denied};

/// How much of a file is read and sent at a time: what a response holds in memory, however
/// large the file is.
const CHUNK: usize = 256 * 1024;

pub fn routes() -> Router<App> {
    Router::new()
        .route("/media", get(list).post(publish))
        .route("/tools/media", post(publish))
        .route("/media/", get(no_id))
        .route("/media/deleted", get(media_manage::deleted))
        .route("/media/delete", post(media_manage::delete_batch))
        .route(
            "/media/{*asset_id}",
            get(content)
                .delete(media_manage::delete)
                .post(media_manage::favorite),
        )
}

/// A header's value as text, without the space around it. Bytes that are no plain signs
/// are read one for one, as the Python Host reads a header.
pub(crate) fn header_text(headers: &HeaderMap, name: &str) -> String {
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
async fn publish(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Json<Value>, Denied> {
    let (parts, body) = request.into_parts();
    let headers = &parts.headers;
    // A file that comes with an agent that moves here says which id it had, and whether it
    // only comes along because the agent names it: a file the Host it was on does not say
    // is the agent's own.
    let (moved_id, moved_at) = (
        header_text(headers, "x-asset-id"),
        header_text(headers, "x-created-at"),
    );
    let named_only = !moved_id.is_empty() && header_text(headers, "x-named-only") == "1";
    if !moved_id.is_empty() {
        if principal != Principal::Administrator {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "only the administrator says which id a file has",
            )
            .into());
        }
        let plain = |byte: u8| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-';
        if moved_id.len() > 180 || !moved_id.bytes().all(plain) {
            return Err(bad("bad asset id").into());
        }
    }
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
        return Err(bad("session required").into());
    }
    let name = media::unquoted(&header_text(headers, "x-file-name"));
    let caption = media::unquoted(&header_text(headers, "x-caption"));
    let created_by = header_text(headers, "x-created-by");
    let member = app
        .member(&session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "unknown session"))?;
    let reading = media::read(&blob, &name, &header_text(headers, "content-type"))
        .map_err(|refused| bad(refused.words()))?;

    let (sha256, blob) = tokio::task::spawn_blocking(move || {
        let digest = Sha256::digest(&blob)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        (digest, blob)
    })
    .await
    .map_err(|_| {
        ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "the file could not be kept",
        )
    })?;
    // Imported files still use the agent's historical idempotency rule.
    let existing = member.agent.view();
    let known = (!moved_id.is_empty())
        .then(|| existing.books.media.get(&moved_id))
        .flatten()
        .filter(|asset| named_only || !asset.named_only)
        .cloned();
    let asset = Asset {
        asset_id: if moved_id.is_empty() {
            format!("asset_{}", ids::secret(18).replace('-', "_"))
        } else {
            moved_id
        },
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
        created_at: moved_at
            .parse::<i64>()
            .ok()
            .filter(|at| *at > 0)
            .unwrap_or_else(|| app.clock.now_ms()),
        named_only,
    };
    if known
        .as_ref()
        .is_some_and(|known| known.sha256 != asset.sha256)
    {
        return Err(media_manage::error(
            &asset.asset_id,
            crate::gallery::Failure::Taken,
        ));
    }
    let asset = known.unwrap_or(asset);
    let image = Image::new(asset.clone(), member.record.agent_id.clone());
    let kept = media_manage::publish(&app, image, blob)
        .await
        .map_err(|why| media_manage::error(&asset.asset_id, why))?;
    media_manage::journal(&app, &member, asset.clone())
        .await
        .map_err(ApiError::from)?;
    let row = Image { asset, ..kept };
    Ok(Json(answered(
        row.row(media_manage::used_as(&app.fleet.directory(), &row)),
    )))
}

/// What publishing is answered with: the row twice, as the Python Host answers, under
/// `asset` and field for field beside it.
fn answered(row: Value) -> Value {
    let mut answer = json!({ "ok": true, "asset": row });
    if let (Some(answer), Some(fields)) = (answer.as_object_mut(), row.as_object()) {
        answer.extend(fields.clone());
    }
    answer
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    session: Option<String>,
    limit: Option<String>,
    #[serde(default)]
    before: String,
    #[serde(default)]
    favorites: String,
}

async fn list(
    State(app): State<App>,
    Query(query): Query<ListQuery>,
) -> Result<Json<Value>, Denied> {
    let session = query.session.unwrap_or_default().trim().to_owned();
    let limit = media::page_size(query.limit.as_deref());
    let favorites = matches!(query.favorites.trim(), "1" | "true");
    let directory = app.fleet.directory();
    app.gallery
        .ask(move |store| {
            let page = store
                .index
                .page(&session, favorites, &query.before, limit)?;
            let assets: Vec<_> = page
                .rows
                .into_iter()
                .map(|image| image.row(media_manage::used_as(&directory, image)))
                .collect();
            Ok(json!({
                "session": session, "assets": assets, "next_before": page.next_before,
                "total": page.total, "favorites_total": page.favorites_total
            }))
        })
        .await
        .map(Json)
        .map_err(|why| media_manage::error("", why))
}

/// The published file with this id, whichever agent published it.
/// What the agents of this Host have under an id: the agent that published the file, and
/// each agent that brought it along because it names it.
pub(crate) fn kept_under(app: &App, asset_id: &str) -> Vec<Asset> {
    let directory = app.fleet.directory();
    let of = |member: &Arc<Member>| member.agent.view().books.media.get(asset_id).cloned();
    directory.members.iter().filter_map(of).collect()
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
    let id = asset_id.to_owned();
    let key = id.clone();
    let (image, total) = match app.gallery.ask(move |store| store.content(&key)).await {
        Ok(found) => found,
        Err(crate::gallery::Failure::Refused(Refusal::Unknown)) => return not_found(),
        Err(why) => return media_manage::error(&id, why).into_response(),
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
        (header::CONTENT_TYPE, image.asset.mime_type),
        (header::CONTENT_LENGTH, length.to_string()),
    ];
    if status == StatusCode::PARTIAL_CONTENT {
        let range = format!("bytes {first}-{last}/{total}");
        said.push((header::CONTENT_RANGE, range));
    }
    let mut response = Response::new(gallery_parts(app.gallery, id, first, length));
    *response.status_mut() = status;
    for (name, value) in said {
        if let Ok(value) = HeaderValue::from_str(&value) {
            response.headers_mut().insert(name, value);
        }
    }
    guarded(response)
}

fn gallery_parts(writer: crate::gallery::Writer, id: String, first: u64, length: u64) -> Body {
    let parts = stream::unfold((first, length), move |(offset, remaining)| {
        let writer = writer.clone();
        let id = id.clone();
        async move {
            if remaining == 0 {
                return None;
            }
            let most = usize::try_from(remaining).map_or(CHUNK, |left| left.min(CHUNK));
            match writer.ask(move |store| store.read(&id, offset, most)).await {
                Ok(bytes) if !bytes.is_empty() => {
                    let sent = bytes.len() as u64;
                    Some((Ok(Bytes::from(bytes)), (offset + sent, remaining - sent)))
                }
                Ok(_) => Some((Err(std::io::Error::other("incomplete media")), (offset, 0))),
                Err(why) => Some((Err(std::io::Error::other(why.words())), (offset, 0))),
            }
        }
    });
    Body::from_stream(parts)
}

/// A file that is kept under a name, as the Python Host sends any file (`_send_file`): all
/// of it or the part a `Range` header asks for, as the kind it is said to be, and never to
/// be kept by whoever fetched it.
pub(crate) fn send_kept(blobs: &Arc<dyn Blobs>, name: String, kind: &str, range: &str) -> Response {
    let Ok(Some(total)) = blobs.len(&name) else {
        return not_found();
    };
    let (status, first, last) = match media::byte_range(range, total) {
        Part::Missing => {
            let said = [
                (header::CONTENT_TYPE, "text/plain".to_string()),
                (header::CONTENT_RANGE, format!("bytes */{total}")),
            ];
            return (StatusCode::RANGE_NOT_SATISFIABLE, said).into_response();
        }
        Part::Whole => (StatusCode::OK, 0, total.saturating_sub(1)),
        Part::Bytes { first, last } => (StatusCode::PARTIAL_CONTENT, first, last),
    };
    let length = if total == 0 { 0 } else { last - first + 1 };
    let mut said = vec![
        (header::CONTENT_TYPE, kind.to_string()),
        (header::CONTENT_LENGTH, length.to_string()),
        (header::CACHE_CONTROL, "no-store".to_string()),
        (header::ACCEPT_RANGES, "bytes".to_string()),
    ];
    if status == StatusCode::PARTIAL_CONTENT {
        said.push((
            header::CONTENT_RANGE,
            format!("bytes {first}-{last}/{total}"),
        ));
    }
    let mut response = Response::new(parts_of(blobs.clone(), name, first, length));
    *response.status_mut() = status;
    for (name, value) in said {
        if let Ok(value) = HeaderValue::from_str(&value) {
            response.headers_mut().insert(name, value);
        }
    }
    response
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
