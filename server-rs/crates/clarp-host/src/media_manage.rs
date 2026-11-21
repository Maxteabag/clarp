//! Gallery routes and recovery for the media writer.

use axum::Json;
use axum::body::Bytes;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use clarp_kernel::gallery::{self, Image, Refusal};
use clarp_kernel::media::Asset;
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal, principal_name};
use crate::fleet::Directory;
use crate::gallery::Failure;

pub(crate) enum Denied {
    Error(ApiError),
    Gone { id: String, at: i64 },
}

impl From<ApiError> for Denied {
    fn from(error: ApiError) -> Self {
        Self::Error(error)
    }
}

impl IntoResponse for Denied {
    fn into_response(self) -> Response {
        match self {
            Self::Error(error) => error.into_response(),
            Self::Gone { id, at } => (
                StatusCode::GONE,
                Json(json!({"error": "removed", "asset_id": id, "deleted_at": at})),
            )
                .into_response(),
        }
    }
}

pub(crate) fn error(id: &str, failure: Failure) -> Denied {
    let status = match failure {
        Failure::Refused(Refusal::Removed(at)) => {
            return Denied::Gone {
                id: id.to_owned(),
                at,
            };
        }
        Failure::Refused(Refusal::Unknown) => StatusCode::NOT_FOUND,
        Failure::Refused(Refusal::NotImage) => StatusCode::UNPROCESSABLE_ENTITY,
        Failure::Refused(_) => StatusCode::BAD_REQUEST,
        Failure::Taken => StatusCode::CONFLICT,
        Failure::Store(_) => StatusCode::INTERNAL_SERVER_ERROR,
        Failure::Busy | Failure::Stopped | Failure::TimedOut => StatusCode::SERVICE_UNAVAILABLE,
    };
    ApiError::new(status, failure.words()).into()
}

fn allow(headers: &HeaderMap) -> Result<(), Denied> {
    if crate::coordination::from_an_agent(headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not").into());
    }
    Ok(())
}

fn json_body(bytes: &Bytes) -> Result<Value, Denied> {
    serde_json::from_slice::<Value>(bytes)
        .map_err(|_| ApiError::new(StatusCode::BAD_REQUEST, "bad json").into())
}

pub(crate) fn used_as(directory: &Directory, image: &Image) -> Vec<Value> {
    let Some(member) = directory.named(&image.agent_id) else {
        return Vec::new();
    };
    let record = &member.record;
    if record.portraits.iter().any(|portrait| {
        portrait.media_asset_id.as_deref() == Some(image.asset.asset_id.as_str())
            || portrait.sha256 == image.asset.sha256
    }) {
        vec![json!({"kind": "portrait", "agent_id": record.agent_id, "persona": record.persona})]
    } else {
        Vec::new()
    }
}

fn changed(app: &App, image: &Image, change: &str, at: i64, favorite: Option<bool>) {
    let mut event = json!({
        "type": "media-changed",
        "session": image.asset.session,
        "agent_id": image.agent_id,
        "asset_id": image.asset.asset_id,
        "change": change,
        "at": at,
    });
    if let Some(favorite) = favorite {
        event["favorite"] = json!(favorite);
    }
    app.hub.publish(event, at);
}

pub(crate) async fn favorite(
    State(app): State<App>,
    Path(id): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, Denied> {
    let Some(id) = id
        .strip_suffix("/favorite")
        .filter(|id| !id.is_empty() && !id.contains('/'))
        .map(str::to_owned)
    else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "not_found").into());
    };
    allow(&headers)?;
    let wanted = gallery::wanted(&json_body(&body)?).map_err(|why| error(&id, why.into()))?;
    let operation = app.clone();
    let key = id.clone();
    let image = app
        .gallery
        .ask(move |store| {
            let now = operation.clock.now_ms();
            let (image, altered) = store.favorite(&key, wanted, now)?;
            if altered {
                changed(&operation, &image, "favorite", now, Some(wanted));
            }
            Ok(image)
        })
        .await
        .map_err(|why| error(&id, why))?;
    Ok(Json(image.row(used_as(&app.fleet.directory(), &image))))
}

fn removed(app: &App, image: &Image, is_new: bool) -> Result<Value, Failure> {
    let deletion = image.deletion.as_ref().ok_or(Refusal::Unknown)?;
    if is_new {
        changed(app, image, "deleted", deletion.at, None);
    }
    Ok(json!({
        "asset_id": image.asset.asset_id,
        "deleted": true,
        "deleted_at": deletion.at,
        "still_used_as": used_as(&app.fleet.directory(), image),
    }))
}

pub(crate) async fn delete(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Result<Json<Value>, Denied> {
    if id.is_empty() || id.contains('/') {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "not_found").into());
    }
    allow(&headers)?;
    let by = principal_name(&app, &principal);
    let operation = app.clone();
    let key = id.clone();
    app.gallery
        .ask(move |store| {
            let (image, is_new) = store.delete(&key, &by, operation.clock.now_ms())?;
            removed(&operation, &image, is_new)
        })
        .await
        .map(Json)
        .map_err(|why| error(&id, why))
}

pub(crate) async fn delete_batch(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, Denied> {
    allow(&headers)?;
    let body = json_body(&body)?;
    let ids: Vec<String> = gallery::ids(&body)
        .map_err(|why| error("", why.into()))?
        .into_iter()
        .map(str::to_owned)
        .collect();
    let by = principal_name(&app, &principal);
    let operation = app.clone();
    app.gallery
        .ask(move |store| {
            let mut results = Vec::with_capacity(ids.len());
            for id in ids {
                let value = match store.delete(&id, &by, operation.clock.now_ms()) {
                    Ok((row, is_new)) => removed(&operation, &row, is_new)?,
                    Err(Failure::Refused(why)) => json!({
                        "asset_id": id, "deleted": false, "error": why.words(),
                    }),
                    Err(error) => return Err(error),
                };
                results.push(value);
            }
            Ok(json!({ "results": results }))
        })
        .await
        .map(Json)
        .map_err(|why| error("", why))
}

pub(crate) async fn deleted(
    State(app): State<App>,
    RawQuery(query): RawQuery,
) -> Result<Json<Value>, Denied> {
    let pairs: Vec<_> =
        url::form_urlencoded::parse(query.as_deref().unwrap_or("").as_bytes()).collect();
    // The reference drops empty values before selecting the first one. Whitespace is
    // still a value here and is stripped only when it is parsed as a limit.
    let first = |name: &str| {
        pairs
            .iter()
            .find(|(key, value)| key == name && !value.is_empty())
            .map(|(_, value)| value.as_ref())
    };
    let (since, limit) = gallery::deletion_query(first("since").unwrap_or(""), first("limit"))
        .map_err(|why| error("", why.into()))?;
    let session = clarp_kernel::python::strip(first("session").unwrap_or("")).to_owned();
    let clock = app.clock.clone();
    app.gallery
        .ask(move |store| {
            Ok(json!(store.index.deleted(
                &session,
                since,
                limit,
                clock.now_ms()
            )))
        })
        .await
        .map(Json)
        .map_err(|why| error("", why))
}

pub(crate) async fn publish(app: &App, image: Image, bytes: Bytes) -> Result<Image, Failure> {
    let operation = app.clone();
    app.gallery
        .ask(move |store| {
            let (kept, is_new) = store.publish(image, &bytes)?;
            if is_new
                && !kept.asset.named_only
                && clarp_kernel::media::is_image(&kept.asset.mime_type)
            {
                changed(&operation, &kept, "created", kept.asset.created_at, None);
            }
            Ok(kept)
        })
        .await
}

pub(crate) async fn journal(
    app: &App,
    member: &crate::fleet::Member,
    asset: Asset,
) -> Result<(), crate::agent::Unavailable> {
    let id = asset.asset_id.clone();
    if member.agent.view().books.media.get(&id) != Some(&asset) {
        tokio::time::timeout(
            std::time::Duration::from_secs(3),
            member.agent.publish(asset),
        )
        .await
        .map_err(|_| {
            crate::agent::Unavailable("the media publication journal did not answer in time".into())
        })??;
    }
    let agent_id = member.record.agent_id.clone();
    app.gallery
        .ask(move |store| store.delivered(&agent_id, &id))
        .await
        .map_err(|why| crate::agent::Unavailable(why.words().to_owned()))
}

pub async fn import_history(app: &App) -> Result<(), Failure> {
    let directory = app.fleet.directory();
    let mut images = Vec::new();
    for member in directory.members.iter().chain(&directory.janitors) {
        let view = member.agent.view();
        images.extend(
            view.books
                .media
                .assets()
                .map(|asset| Image::new(asset.clone(), member.record.agent_id.clone())),
        );
    }
    images.sort_by_key(|image| image.asset.named_only);
    app.gallery.ask(move |store| store.import(images)).await
}

pub async fn recover(app: &App) {
    let directory = app.fleet.directory();
    match app.gallery.ask(|store| store.undelivered()).await {
        Ok(pending) => {
            for image in pending {
                if let Some(member) = directory.named(&image.agent_id)
                    && let Err(why) = journal(app, member, image.asset).await
                {
                    eprintln!(
                        "clarp-host: a gallery publication awaits its journal: {}",
                        why.0
                    );
                }
            }
        }
        Err(why) => eprintln!("clarp-host: gallery recovery unavailable: {}", why.words()),
    }
    let left = app.fleet.left_out();
    let safe = left.not_run_counts().is_empty()
        && !left.counts().contains_key("agent")
        && directory
            .members
            .iter()
            .chain(&directory.janitors)
            .all(|member| member.agent.view().broken.is_none());
    if let Err(why) = app
        .gallery
        .ask(move |store| {
            store.recover(safe);
            Ok(())
        })
        .await
    {
        eprintln!(
            "clarp-host: gallery reclamation is pending: {}",
            why.words()
        );
    }
}
