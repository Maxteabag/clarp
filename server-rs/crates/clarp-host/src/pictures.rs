//! The picture of a contact: what the provider of pictures is asked for it, and the picture
//! as a contact keeps it (`persona_avatar_generation.py`).
//!
//! The provider is behind a port ([`PictureMaker`]): OpenAI's `gpt-image-2` with the owner's
//! key, looked up when a picture is made and copied nowhere; a test gives the Host a stand-in
//! on this machine instead (`CLARP_PICTURE_ENDPOINT`), whose picture is made up. Nothing here
//! is run unless the owner turned the making of pictures on: it spends.

use std::process::Stdio;
use std::time::Duration;

use clarp_kernel::python;
use serde_json::{Value, json};

use clarp_kernel::contacts::picture::{ANSWER_MOST, MADE_MOST};
pub use clarp_kernel::contacts::picture::{KEPT_MOST, MODEL};

/// Where a picture is asked, unless a stand-in is given.
const PROVIDER: &str = "https://api.openai.com/v1/images/generations";

/// The provider of pictures: the bytes of a picture for what it is asked, with the owner's
/// key, or why not.
pub trait PictureMaker: Send + Sync {
    fn make(&self, key: &str, prompt: &str) -> Result<Vec<u8>, String>;
}

/// OpenAI's images, or a stand-in at `endpoint` that answers as they do.
pub struct OpenAiPictures {
    client: ureq::Agent,
    url: String,
}

impl OpenAiPictures {
    /// `endpoint`: a stand-in on this machine; empty, OpenAI itself.
    pub fn new(endpoint: &str) -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(180)))
            .max_redirects(0)
            .http_status_as_error(false)
            .build();
        let url = if endpoint.is_empty() {
            PROVIDER.to_string()
        } else {
            endpoint.to_string()
        };
        Self {
            client: config.into(),
            url,
        }
    }
}

/// What a picture is asked with (`_request_generation`).
pub fn asked(prompt: &str) -> Value {
    json!({
        "model": MODEL,
        "prompt": prompt,
        "size": "1024x1024",
        "quality": "medium",
        "output_format": "jpeg",
        "output_compression": 85,
    })
}

/// The picture in an answer of the provider: `data[0].b64_json`, decoded.
pub fn answered(status: u16, body: &[u8]) -> Result<Vec<u8>, String> {
    if status != 200 {
        let detail = String::from_utf8_lossy(&body[..body.len().min(1000)]);
        return Err(format!(
            "OpenAI avatar generation failed: HTTP {status}: {detail}"
        ));
    }
    if body.len() as u64 > ANSWER_MOST {
        return Err("OpenAI image response exceeded safety limit".to_string());
    }
    let invalid = || "OpenAI image response was invalid".to_string();
    let said: Value = serde_json::from_slice(body).map_err(|_| invalid())?;
    let encoded = said["data"][0]["b64_json"].as_str().ok_or_else(invalid)?;
    let made = python::b64decode(encoded).ok_or_else(invalid)?;
    if made.is_empty() || made.len() > MADE_MOST {
        return Err("image provider returned empty or oversized content".to_string());
    }
    Ok(made)
}

/// Why a picture was not made, as it is kept: its kind and status in the provider's words,
/// never the owner's key and never what was asked (an answer may quote either), cut at a
/// thousand signs as the Python Host cuts a job's reason.
pub fn scrubbed(why: &str, key: &str, prompt: &str) -> String {
    let mut said = why.to_string();
    for (secret, put) in [(key, "<the owner's key>"), (prompt, "<what was asked>")] {
        if !secret.is_empty() {
            said = said.replace(secret, put);
        }
    }
    said.chars().take(1000).collect()
}

impl PictureMaker for OpenAiPictures {
    fn make(&self, key: &str, prompt: &str) -> Result<Vec<u8>, String> {
        self.asked_of(key, prompt)
            .map_err(|why| scrubbed(&why, key, prompt))
    }
}

impl OpenAiPictures {
    fn asked_of(&self, key: &str, prompt: &str) -> Result<Vec<u8>, String> {
        let body = asked(prompt).to_string();
        let mut response = self
            .client
            .post(&self.url)
            .header("Authorization", &format!("Bearer {key}"))
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .header("User-Agent", "clarp-persona-avatar/1")
            .send(body.as_bytes())
            .map_err(|why| format!("OpenAI avatar generation failed: {why}"))?;
        let status = response.status().as_u16();
        let read = response
            .body_mut()
            .with_config()
            .limit(ANSWER_MOST + 1)
            .read_to_vec()
            .map_err(|_| "OpenAI image response exceeded safety limit".to_string())?;
        answered(status, &read)
    }
}

/// Whether bytes begin as a JPEG, a PNG or a WebP picture does.
fn is_picture(bytes: &[u8]) -> bool {
    bytes.starts_with(b"\xff\xd8\xff")
        || bytes.starts_with(b"\x89PNG\r\n\x1a\n")
        || bytes.starts_with(b"RIFF")
}

/// A made picture as a contact keeps it (`_normalize_avatar_jpeg`): cut to a square at its
/// middle and scaled to 512 by 512 as a JPEG by `scaler` (ffmpeg), at a lower quality each
/// time while it is more than [`KEPT_MOST`]; where that cannot be done, the picture as it
/// came if it is a JPEG, a PNG or a WebP no larger than that.
pub fn kept(raw: &[u8], scaler: &str) -> Result<Vec<u8>, String> {
    // A place of its own for the scaler's two files, taken away after.
    static PLACES: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let place = std::env::temp_dir().join(format!(
        "clarp-avatar-{}-{}",
        std::process::id(),
        PLACES.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    let scaled = std::fs::create_dir(&place)
        .ok()
        .and_then(|()| scaled(raw, scaler, &place));
    let _ = std::fs::remove_dir_all(&place);
    if let Some(made) = scaled {
        return Ok(made);
    }
    if is_picture(raw) && raw.len() <= KEPT_MOST {
        return Ok(raw.to_vec());
    }
    Err("could not downsample generated avatar to <=512KB JPEG".to_string())
}

/// The picture scaled in `place`, where `scaler` can do it.
fn scaled(raw: &[u8], scaler: &str, place: &std::path::Path) -> Option<Vec<u8>> {
    {
        let (from, to) = (place.join("raw.img"), place.join("avatar.jpg"));
        if std::fs::write(&from, raw).is_ok() {
            for quality in ["3", "6", "10"] {
                let _ = std::fs::remove_file(&to);
                let scaled = crate::child::command(scaler)
                    .args(["-y", "-loglevel", "error", "-i"])
                    .arg(&from)
                    .args([
                        "-vf",
                        "crop='min(iw,ih)':'min(iw,ih)',scale=512:512:flags=lanczos",
                    ])
                    .args(["-frames:v", "1", "-q:v", quality])
                    .arg(&to)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
                if scaled.is_ok_and(|status| status.success())
                    && let Ok(made) = std::fs::read(&to)
                    && !made.is_empty()
                    && made.len() <= KEPT_MOST
                {
                    return Some(made);
                }
            }
        }
    }
    None
}

/// The kind of the computer's job that makes a contact's picture.
pub const KIND: &str = "persona-avatar";
/// Why a picture's job ended when the Host stopped in the middle of it: the reference's words
/// for an interrupted worker. It counts as a try.
pub const INTERRUPTED: &str = "persona_avatar_worker_interrupted";

/// The job of a contact's picture, by the contact's id: one a contact, whatever its tries.
pub fn job_id(persona_id: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(persona_id.as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("persona-avatar-{}", &hex[..20])
}

/// The admission and the registration of a picture's job are one step: two launches at once
/// never both begin the same contact's picture, nor count the same day twice.
static ASKING: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// A contact is asked its picture (`persona_avatar_generation.start_best_effort`): at a
/// launch of an agent without a picture or symbol of its own, at an assignment that minted or
/// made its contact, when the owner makes a persona without one. Nothing happens unless the
/// installation turned the making of pictures on (`--contact-pictures`), the contact is the
/// owner's own with neither picture nor symbol, there is a key, and the admission begins it
/// ([`clarp_kernel::contacts::picture::admitted`]). Never fails its caller; it waits for
/// nothing but the fleet.
pub async fn ask(app: &crate::edge::App, persona_name: &str, tier: &str, backend: &str) {
    use clarp_kernel::contacts::picture::{self, Tried};
    use clarp_kernel::jobs::{JobCommand, JobStatus, Worker};
    if !app.config.contact_pictures {
        return;
    }
    let persona = {
        let directory = app.fleet.directory();
        let [named, cartesia, gemini] = crate::owner::personas(&app.mouth.owners());
        let listed = directory.personas.listed(&clarp_kernel::personas::Owner {
            roster: &named,
            cartesia: &cartesia,
            gemini: &gemini,
        });
        listed.into_iter().find(|persona| {
            persona["builtin"] == false
                && clarp_kernel::personas::same(
                    persona["name"].as_str().unwrap_or(""),
                    persona_name.trim(),
                )
        })
    };
    let Some(persona) = persona else {
        return;
    };
    let text = |key: &str| persona[key].as_str().unwrap_or("").to_string();
    if !text("avatar_url").is_empty()
        || !text("avatar_symbol").is_empty()
        || app.ears.key("openai").is_none()
    {
        return;
    }
    let (persona_id, name) = (text("id"), text("name"));
    let resolved = {
        let given = tier.trim().to_lowercase();
        if !given.is_empty() {
            given
        } else if !clarp_kernel::personas::tier(&name).is_empty() {
            clarp_kernel::personas::tier(&name).to_string()
        } else {
            clarp_kernel::contacts::tier_of_tool(backend).to_string()
        }
    };
    let id = job_id(&persona_id);
    let _one = ASKING.lock().await;
    let now = app.clock.now_ms();
    let (Ok(before), Ok(all)) = (
        app.fleet.computer_job(id.clone()).await,
        app.fleet.computer_jobs().await,
    ) else {
        return;
    };
    // The pictures begun in the last day on this computer: every registration of the kind in the
    // jobs' timelines (`background_job_events`, status queued).
    let mut begun_today = 0u32;
    for job in all.iter().filter(|job| job.kind == KIND) {
        let Ok(Some(kept)) = app.fleet.computer_job(job.id.clone()).await else {
            continue;
        };
        let queued = kept.timeline.iter().filter(|record| {
            record.job.status == JobStatus::Queued && record.at_ms >= now - picture::DAILY_WINDOW_MS
        });
        begun_today += u32::try_from(queued.count()).unwrap_or(u32::MAX);
    }
    let tried = before.as_ref().map(|kept| Tried {
        status: kept.job.status.as_str(),
        generation: kept.job.generation,
        ended_ago_ms: Some(now - kept.job.terminal_at_ms.unwrap_or(kept.job.updated_at_ms)),
    });
    let again = tried.is_some_and(|tried| tried.status == "failed" || tried.status == "cancelled");
    let Some(generation) = picture::admitted(tried, begun_today) else {
        return;
    };
    let registered = app
        .fleet
        .change_computer_job(JobCommand::Register {
            queued: true,
            id: id.clone(),
            kind: KIND.to_string(),
            title: format!("Generate avatar for {name}"),
            detail: format!("Generating {resolved} Contact portrait"),
            worker: Worker::none(),
            restart_cancelled: true,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: Some(picture::HEARTBEAT_TIMEOUT_MS),
            metadata: json!({
                "persona_id": persona_id, "persona_name": name, "tier": resolved,
                "provider": "openai", "model": MODEL, "expire_queued": true,
            }),
        })
        .await;
    // Refused (an id an agent has, or the book's own refusal): not asked again now; not
    // answered: the contact's next launch asks again.
    let Ok(Ok(change)) = registered else {
        return;
    };
    let Some(job) = change
        .job
        .filter(|job| job.generation == generation && job.status == JobStatus::Queued)
    else {
        return;
    };
    // A failed picture begun again: the apps are told the contact changed, as the job
    // watcher tells them.
    if again {
        app.hub
            .publish(crate::wire::roster_persona_updated(&name), now);
    }
    let app = app.clone();
    let prompt = clarp_kernel::contacts::portrait_prompt(&name, &resolved, backend);
    tokio::spawn(async move { make(&app, &id, job.generation, &persona_id, &name, &prompt).await });
}

/// Whether the picture's job is still this try, under way: every effect of the try is
/// guarded by it. A job cancelled, or begun again, is not this try any more.
async fn current(app: &crate::edge::App, id: &str, generation: u64) -> bool {
    use clarp_kernel::jobs::JobStatus;
    matches!(
        app.fleet.computer_job(id.to_string()).await,
        Ok(Some(kept)) if kept.job.generation == generation && kept.job.status == JobStatus::Running
    )
}

/// One try of a contact's picture, the Host's own task (`generate_for_persona`): the
/// provider asked with the owner's key as it is now, the picture made what a contact keeps,
/// kept, and the job ended either way; the apps told the contact changed.
async fn make(
    app: &crate::edge::App,
    id: &str,
    generation: u64,
    persona_id: &str,
    name: &str,
    prompt: &str,
) {
    use clarp_kernel::jobs::{JobCommand, Worker};
    let started = JobCommand::Start {
        id: id.to_string(),
        generation: Some(generation),
        worker: Worker::none(),
    };
    if !matches!(app.fleet.change_computer_job(started).await, Ok(Ok(_))) {
        return;
    }
    let work = async {
        if !current(app, id, generation).await {
            return Err(None);
        }
        let Some(key) = app.ears.key("openai") else {
            return Err(Some("OpenAI API key is not configured".to_string()));
        };
        let endpoint = app.config.picture_endpoint.clone();
        let asked = prompt.to_string();
        let raw =
            tokio::task::spawn_blocking(move || OpenAiPictures::new(&endpoint).make(&key, &asked))
                .await
                .map_err(|_| Some("the picture was not made".to_string()))?
                .map_err(Some)?;
        let picture = tokio::task::spawn_blocking(move || kept(&raw, "ffmpeg"))
            .await
            .map_err(|_| Some("the picture was not made".to_string()))?
            .map_err(Some)?;
        if picture.len() > KEPT_MOST {
            return Err(Some(
                "generated avatar exceeds Contact size limit".to_string(),
            ));
        }
        if !current(app, id, generation).await {
            return Err(None);
        }
        app.fleet
            .picture_made(persona_id.to_string(), picture)
            .await
            .map_err(|why| Some(why.0))
    };
    // The work beside the end of its try: a cancel, a finish or a new try ends the waiting,
    // and the work with it (what the provider still answers is not read).
    let made = tokio::select! {
        made = work => made,
        _ = app.fleet.wait_computer_job_end(id.to_string(), generation) => Err(None),
    };
    // Not this try any more (cancelled, or begun again): nothing more is its to say.
    let failed = match made {
        Err(None) => return,
        Ok(_) => None,
        Err(Some(why)) => Some(why),
    };
    let told_by_keeping = failed.is_none();
    let finished = JobCommand::Finish {
        id: id.to_string(),
        generation,
        worker: Worker::none(),
        failed,
    };
    let _ = app.fleet.change_computer_job(finished).await;
    if !told_by_keeping {
        app.hub.publish(
            crate::wire::roster_persona_updated(name),
            app.clock.now_ms(),
        );
    }
}

/// The contacts whose picture is under way, by the persona's id, with how it stands
/// (`queued`, `running`): what persona items and agent rows say as `avatar_generation_status`
/// (`avatar_generation_status_for_persona`). Nothing while the installation did not turn the
/// making of pictures on.
pub async fn under_way(app: &crate::edge::App) -> std::collections::HashMap<String, String> {
    let mut found = std::collections::HashMap::new();
    if !app.config.contact_pictures {
        return found;
    }
    let Ok(all) = app.fleet.computer_jobs().await else {
        return found;
    };
    for job in all
        .into_iter()
        .filter(|job| job.kind == KIND && job.status.is_active())
    {
        if let Some(persona_id) = job.metadata["persona_id"].as_str() {
            found.insert(persona_id.to_string(), job.status.as_str().to_string());
        }
    }
    found
}

/// At the Host's start, a picture's job that was under way when the Host stopped ends failed
/// in the reference's words for an interrupted worker, and that counts as a try. Nothing is
/// asked of a provider at a start.
pub async fn interrupted(app: &crate::edge::App) {
    use clarp_kernel::jobs::{JobCommand, Worker};
    let Ok(all) = app.fleet.computer_jobs().await else {
        return;
    };
    for job in all
        .into_iter()
        .filter(|job| job.kind == KIND && job.status.is_active())
    {
        let finished = JobCommand::Finish {
            id: job.id.clone(),
            generation: job.generation,
            worker: Worker::none(),
            failed: Some(INTERRUPTED.to_string()),
        };
        let _ = app.fleet.change_computer_job(finished).await;
        let name = job.metadata["persona_name"]
            .as_str()
            .unwrap_or("")
            .to_string();
        app.hub.publish(
            crate::wire::roster_persona_updated(&name),
            app.clock.now_ms(),
        );
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    /// A made-up picture: the bytes a JPEG begins with, and nothing of anybody's.
    const MADE_UP: &[u8] = b"\xff\xd8\xff\xe0 a made-up picture of nobody";

    #[test]
    fn a_picture_is_asked_as_the_python_host_asks_it() {
        assert_eq!(
            asked("A portrait"),
            json!({ "model": "gpt-image-2", "prompt": "A portrait", "size": "1024x1024",
                    "quality": "medium", "output_format": "jpeg", "output_compression": 85 })
        );
    }

    #[test]
    fn the_answer_of_the_provider_is_read_as_the_python_host_reads_it() {
        let body = json!({ "data": [{ "b64_json": python::b64encode(MADE_UP) }] }).to_string();
        assert_eq!(answered(200, body.as_bytes()).unwrap(), MADE_UP);
        let refused = answered(429, b"too many");
        assert_eq!(
            refused.unwrap_err(),
            "OpenAI avatar generation failed: HTTP 429: too many"
        );
        for nothing in [
            &b"{}"[..],
            b"not json",
            br#"{"data":[{"b64_json":"!!"}]}"#,
            br#"{"data":[{"b64_json":""}]}"#,
        ] {
            assert!(
                answered(200, nothing).is_err(),
                "{}",
                String::from_utf8_lossy(nothing)
            );
        }
    }

    #[test]
    fn why_a_picture_was_not_made_never_holds_the_key_nor_what_was_asked() {
        let quoted = "OpenAI avatar generation failed: HTTP 400: key sk-test-1 rejected for A portrait of Kit";
        let kept = scrubbed(quoted, "sk-test-1", "A portrait of Kit");
        assert!(
            !kept.contains("sk-test-1") && !kept.contains("A portrait of Kit"),
            "{kept}"
        );
        assert!(
            kept.starts_with("OpenAI avatar generation failed: HTTP 400"),
            "{kept}"
        );
        assert_eq!(scrubbed(&"x".repeat(5000), "", "").chars().count(), 1000);
    }

    #[test]
    fn a_picture_that_cannot_be_scaled_is_kept_as_it_came_only_where_it_is_small_and_a_picture() {
        let nowhere = "a-program-that-is-not-there";
        assert_eq!(kept(MADE_UP, nowhere).unwrap(), MADE_UP);
        assert!(kept(b"no picture at all", nowhere).is_err());
        let mut large = MADE_UP.to_vec();
        large.resize(KEPT_MOST + 1, 0);
        assert!(kept(&large, nowhere).is_err());
    }
}
