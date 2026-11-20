//! Explicit generation of two alternatives to the current primary portrait.
use crate::{
    agent::Unavailable,
    edge::{ApiError, App, Principal},
    fleet::{
        Member, picture_name,
        portrait_jobs::{Admission, BYTES, PENDING, Publication},
    },
};
use axum::{
    Json, Router,
    body::Bytes,
    extract::{Extension, Query, State},
    http::StatusCode,
    routing::get,
};
use clarp_kernel::{
    jobs::{JobCommand, JobStatus, Worker},
    media::Asset,
    portrait_generation as rules,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, sync::Arc};

/// Held only while a gallery is changed, never during either paid request.
pub(crate) static GALLERY_WRITE: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn source(app: &App, member: &Member) -> Option<(String, &'static str, u64)> {
    let digest = member.record.avatar.as_deref()?;
    let name = picture_name(digest);
    let len = app.blobs.len(&name).ok()??;
    let head = app.blobs.read(&name, 0, 12).ok()?;
    Some((
        name,
        clarp_kernel::kept_portraits::picture_kind(&head)?,
        len,
    ))
}

async fn capability(app: &App, member: Option<&Member>) -> Result<Value, ApiError> {
    let Some(member) = member else {
        return Ok(rules::capability(false, false, None, Value::Null, 0));
    };
    let (job, count) = app
        .fleet
        .portrait_status(rules::job_id(&member.record.agent_id))
        .await?;
    let job = job.map_or(Value::Null, |row| {
        crate::wire::computer_job(&app.server_id, &row.job, app.clock.now_ms())
    });
    let primary = source(app, member);
    Ok(rules::capability(
        true,
        app.ears.key("openai").is_some(),
        primary.as_ref().map(|(_, mime, len)| (*mime, *len)),
        job,
        count,
    ))
}

fn session(app: &App, sid: &str) -> Option<Arc<Member>> {
    let sid = if sid.is_empty() {
        clarp_kernel::addressed::DEFAULT_SESSION
    } else {
        sid.trim()
    };
    app.fleet
        .directory()
        .anyone(sid)
        .filter(|m| m.record.session == sid)
        .cloned()
}

async fn status(
    State(app): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let member = session(&app, query.get("session").map_or("", String::as_str));
    Ok(Json(capability(&app, member.as_deref()).await?))
}

async fn start(
    State(app): State<App>,
    Extension(who): Extension<Principal>,
    body: Bytes,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    // The general door also admits a local caller on an unprotected installation.
    // Only an authenticated owner or full device may authorize a paid edit.
    if matches!(who, Principal::Administrator) && app.config.auth_token.is_empty() {
        return Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "authentication required",
        ));
    }
    let data = crate::personas::object(&body)?;
    let sid = data
        .get("session")
        .filter(|v| clarp_kernel::python::truthy(v))
        .map(|v| {
            v.as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| clarp_kernel::python::repr(v))
        })
        .unwrap_or_default();
    let member = session(&app, &sid);
    let cap = capability(&app, member.as_deref()).await?;
    match rules::start(member.is_some(), &cap) {
        rules::Start::Refused(why) => return Err(ApiError::new(StatusCode::CONFLICT, why)),
        rules::Start::Reuse => {
            return Ok((
                StatusCode::ACCEPTED,
                Json(json!({"job":cap["job"],"capability":cap})),
            ));
        }
        rules::Start::Begin => {}
    }
    let member =
        member.ok_or_else(|| ApiError::new(StatusCode::CONFLICT, "unknown Agent session"))?;
    let id = member.record.agent_id.clone();
    let job = match app.fleet.begin_portraits(id.clone()).await? {
        Admission::Existing(job) => job,
        Admission::Refused(reason) => return Err(ApiError::new(StatusCode::CONFLICT, reason)),
        Admission::Begun(job) => {
            let worker = app.clone();
            let generation = job.generation;
            tokio::spawn(async move {
                run(worker, id, generation).await;
            });
            job
        }
    };
    Ok((
        StatusCode::ACCEPTED,
        Json(
            json!({"job":crate::wire::computer_job(&app.server_id,&job,app.clock.now_ms()),"capability":cap}),
        ),
    ))
}

pub(crate) fn routes() -> Router<App> {
    Router::new().route("/agent-portrait-generation", get(status).post(start))
}

pub(crate) fn asset(
    member: &Member,
    bytes: &[u8],
    index: usize,
    now: i64,
) -> Result<Asset, String> {
    let mime = rules::generated_kind(bytes)?;
    let extension = match mime {
        "image/png" => ".png",
        "image/webp" => ".webp",
        "image/jpeg" => ".jpg",
        // generate_two validates both signatures before resolving the media extensions.
        other => return Err(format!("'{other}'")),
    };
    let (width, height) = clarp_kernel::media::image_size(bytes, mime);
    Ok(Asset {
        asset_id: format!("asset_{}", crate::ids::secret(18).replace('-', "_")),
        session: member.record.session.clone(),
        source_name: format!("generated-portrait-{index}{extension}"),
        sha256: format!("{:x}", Sha256::digest(bytes)),
        extension: extension.into(),
        mime_type: mime.into(),
        bytes: bytes.len() as u64,
        width,
        height,
        caption: format!("Generated portrait alternative {index}"),
        created_by: "portrait_generation:openai".into(),
        created_at: now,
        named_only: false,
    })
}

async fn run(app: App, agent: String, generation: u64) {
    let id = rules::job_id(&agent);
    let start = JobCommand::Start {
        id: id.clone(),
        generation: Some(generation),
        worker: Worker::none(),
    };
    if !matches!(app.fleet.change_computer_job(start).await,Ok(Ok(change)) if change.job.as_ref().is_some_and(|j|j.generation==generation && j.status==JobStatus::Running))
    {
        return;
    }
    let work = async {
        let member = app
            .fleet
            .directory()
            .named(&agent)
            .cloned()
            .ok_or("unknown Agent session")?;
        let (name, mime, len) =
            source(&app, &member).ok_or("primary portrait content unavailable")?;
        if !rules::supported(mime) {
            return Err("primary portrait format is not supported".into());
        }
        if len > rules::MAX_IMAGE as u64 {
            return Err("primary portrait exceeds the input size limit".into());
        }
        let store = app.blobs.clone();
        let raw = tokio::task::spawn_blocking(move || store.read(&name, 0, rules::MAX_IMAGE + 1))
            .await
            .map_err(|_| "primary portrait read interrupted")?
            .map_err(|_| "primary portrait content unavailable")?;
        if raw.len() as u64 != len {
            return Err("primary portrait content unavailable".into());
        }
        let raw = Arc::new(raw);
        let key = Arc::new(
            app.ears
                .key("openai")
                .ok_or("OpenAI API key is not configured")?,
        );
        let mut pair = Vec::with_capacity(2);
        for prompt in rules::PROMPTS {
            if !app
                .fleet
                .computer_job(id.clone())
                .await
                .map_err(|e| e.0)?
                .is_some_and(|row| {
                    row.job.generation == generation && row.job.status == JobStatus::Running
                })
            {
                return Ok(None);
            }
            let source = raw.clone();
            let key = key.clone();
            let base = app.config.picture_endpoint.clone();
            let result = tokio::task::spawn_blocking(move || {
                crate::pictures::OpenAiPictures::new(&base).edit(&key, prompt, &source, mime)
            })
            .await
            .map_err(|_| "portrait worker interrupted")??;
            pair.push(result);
        }
        let [first, second]: [Vec<u8>; 2] =
            pair.try_into().map_err(|_| "two portraits required")?;
        rules::distinct(&first, &second)?;
        let now = app.clock.now_ms();
        let publication = Publication {
            agent_id: agent.clone(),
            assets: [
                asset(&member, &first, 1, now)?,
                asset(&member, &second, 2, now)?,
            ],
        };
        Ok::<_, String>(Some((publication, [first, second])))
    };
    tokio::pin!(work);
    let ended = app.fleet.wait_computer_job_end(id.clone(), generation);
    tokio::pin!(ended);
    let interval = std::time::Duration::from_secs(30);
    let mut beats = tokio::time::interval_at(tokio::time::Instant::now() + interval, interval);
    let result = loop {
        tokio::select! {
            biased;
            _=&mut ended=>return,
            result=&mut work=>break result,
            _=beats.tick()=>{
                let beat=JobCommand::Heartbeat{id:id.clone(),generation:Some(generation),worker:Worker::none(),source:"worker".into()};
                if !matches!(app.fleet.change_computer_job(beat).await, Ok(Ok(_))){return;}
            }
        }
    };
    let failure = match result {
        Ok(None) => return,
        Ok(Some((publication, bytes))) => {
            let _gallery = GALLERY_WRITE.lock().await;
            match app
                .fleet
                .accept_portraits(id.clone(), generation, publication.clone(), bytes)
                .await
            {
                Ok(false) => return,
                Ok(true) => {
                    drop(_gallery);
                    let mut delay = 1;
                    while let Err(error) = deliver(&app.fleet, &app.blobs, &publication).await {
                        app.fleet.unkept(PENDING, &error.0);
                        tokio::time::sleep(std::time::Duration::from_secs(delay)).await;
                        delay = (delay * 2).min(60);
                        if matches!(
                            app.fleet
                                .kept(PENDING, publication.assets[0].asset_id.clone())
                                .await,
                            Ok(None)
                        ) {
                            break;
                        }
                    }
                    return;
                }
                Err(error) => error.0,
            }
        }
        Err(error) => error,
    };
    let _ = app
        .fleet
        .change_computer_job(JobCommand::Finish {
            id: id.clone(),
            generation,
            worker: Worker::none(),
            failed: Some(failure),
        })
        .await;
}

/// The result was accepted durably. Replay only local, idempotent publication after a crash.
pub(crate) async fn deliver(
    fleet: &crate::fleet::FleetHandle,
    store: &Arc<dyn clarp_ports::blobs::Blobs>,
    publication: &Publication,
) -> Result<(), Unavailable> {
    let member = fleet.directory().named(&publication.agent_id).cloned();
    if let Some(member) = member {
        for asset in &publication.assets {
            let blobs = store.clone();
            let name = asset.blob();
            let picture = picture_name(&asset.sha256);
            // Normally the fleet has already filled both immutable names before the gallery changed.
            if matches!(blobs.len(&name),Ok(Some(n)) if n==asset.bytes)
                && matches!(blobs.len(&picture),Ok(Some(n)) if n==asset.bytes)
            {
                continue;
            }
            let raw = fleet
                .kept(BYTES, asset.asset_id.clone())
                .await?
                .ok_or_else(|| Unavailable("missing committed portrait bytes".into()))?;
            tokio::task::spawn_blocking(move || {
                blobs.put(&name, &raw)?;
                blobs.put(&picture, &raw)
            })
            .await
            .map_err(|_| Unavailable("portrait write interrupted".into()))?
            .map_err(|e| Unavailable(e.to_string()))?;
        }
        let view = member.agent.view();
        if !publication
            .assets
            .iter()
            .all(|asset| view.books.media.get(&asset.asset_id) == Some(asset))
        {
            member
                .agent
                .publish_portraits(publication.assets.clone())
                .await?;
        }
    }
    let [first, second] = &publication.assets;
    fleet
        .change_together(
            vec![],
            vec![
                (PENDING, first.asset_id.clone()),
                (BYTES, first.asset_id.clone()),
                (BYTES, second.asset_id.clone()),
            ],
        )
        .await
}

pub async fn recover(app: &App) -> Result<(), Unavailable> {
    for (_, raw) in app.fleet.kept_all(PENDING).await? {
        let publication = serde_json::from_slice(&raw)
            .map_err(|e| Unavailable(format!("invalid portrait publication: {e}")))?;
        deliver(&app.fleet, &app.blobs, &publication).await?;
    }
    for job in app
        .fleet
        .computer_jobs()
        .await?
        .into_iter()
        .filter(|job| job.kind == rules::KIND && job.status.is_active())
    {
        app.fleet
            .change_computer_job(JobCommand::Finish {
                id: job.id,
                generation: job.generation,
                worker: Worker::none(),
                failed: Some("portrait_worker_interrupted".into()),
            })
            .await?
            .map_err(|e| Unavailable(e.to_string()))?;
    }
    Ok(())
}
