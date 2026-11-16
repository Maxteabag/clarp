//! Speech in at the Host's edge: `POST /transcribe`, and what the apps are told about the
//! engines that listen.
//!
//! A person speaks, the app records a clip and hands it over; the Host has an engine turn it
//! into text and answers with the text and the trace the message will travel under. The app
//! then sends that text as the user's message: nothing is sent to an agent from here.
//!
//! The rules are the kernel's ([`clarp_kernel::heard`], the Python Host's); the engines are
//! behind one port ([`clarp_speech::listen`]). What is here is the request and its answer,
//! which engine a clip goes to on this Host, and what is kept so that a request that comes
//! twice is answered once: an answer is kept under the name of its job for thirty days,
//! beside the Host's data, and nothing of a clip is kept at all.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime};

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::heard::{self, OWN_PROVIDER, PROVIDERS};
use clarp_speech::listen::{Asked, Clip, Deaf, Listener};
use clarp_speech::scribe::Scribe;
use clarp_speech::whisper::{Whisper, Worker};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::config::Config;
use crate::edge::App;
use crate::ids;
use crate::media::header_text;

/// How long an answer is kept for a request that may come again.
const KEPT_FOR: Duration = Duration::from_secs(30 * 24 * 60 * 60);

/// How long a clip waits for an engine that hears one at a time. What an app sends here is
/// the whole of what was said, not a guess along the way: it waits rather than be dropped.
const WAITS: Duration = Duration::from_secs(10);

/// How many of the fleet's names an engine is asked to listen for.
const NAMES: usize = 40;

/// The text that `key` has in the table `[table]` of a file written in TOML, when it has a
/// plain one there. This reads as much of TOML as a key in a table takes: tables in
/// brackets, a value in double quotes with its escapes, or in single quotes as it stands.
pub fn text_in(file: &str, table: &str, key: &str) -> Option<String> {
    let mut inside = false;
    for line in file.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix('[') {
            // `[[name]]` begins a list of tables, which holds no key of ours.
            let named = rest.split(']').next().map(str::trim);
            inside = !rest.starts_with('[') && named == Some(table);
            continue;
        }
        if !inside {
            continue;
        }
        let Some((name, value)) = line.split_once('=') else {
            continue;
        };
        if name.trim().trim_matches('"') == key {
            return plain(value.trim());
        }
    }
    None
}

/// A value of TOML that is one plain text on one line.
fn plain(value: &str) -> Option<String> {
    if let Some(rest) = value.strip_prefix('\'') {
        // Three quotes begin a text of several lines, which no key is.
        if rest.starts_with("''") {
            return None;
        }
        return rest.split_once('\'').map(|(text, _)| text.to_string());
    }
    let rest = value.strip_prefix('"')?;
    if rest.starts_with("\"\"") {
        return None;
    }
    let mut out = String::new();
    let mut signs = rest.chars();
    while let Some(sign) = signs.next() {
        match sign {
            '"' => return Some(out),
            '\\' => {
                let escaped = signs.next()?;
                let digits = match escaped {
                    'u' => 4,
                    'U' => 8,
                    _ => 0,
                };
                if digits > 0 {
                    let code: String = signs.by_ref().take(digits).collect();
                    let sign = u32::from_str_radix(&code, 16).ok().and_then(char::from_u32);
                    out.push(sign.filter(|_| code.len() == digits)?);
                    continue;
                }
                out.push(match escaped {
                    'n' => '\n',
                    't' => '\t',
                    'r' => '\r',
                    'b' => '\u{8}',
                    'f' => '\u{c}',
                    '"' => '"',
                    '\\' => '\\',
                    _ => return None,
                });
            }
            other => out.push(other),
        }
    }
    None // never closed
}

/// Where the keys of engines that are reached over the network are found: a file of the
/// Python Host's kind first, this Host's own environment after it. A key is read when a clip
/// is to be heard and kept nowhere, so what the owner changes there is what is used next,
/// and no key is copied into this Host.
#[derive(Debug, Clone, Default)]
pub struct Keys {
    file: Option<PathBuf>,
}

impl Keys {
    pub fn new(file: Option<PathBuf>) -> Self {
        Self { file }
    }

    /// The key of this provider, when there is one.
    pub fn of(&self, provider: &str) -> Option<String> {
        let variable = match provider {
            "elevenlabs" => "ELEVEN_API_KEY",
            _ => return None,
        };
        let written = self
            .file
            .as_ref()
            .and_then(|file| std::fs::read_to_string(file).ok())
            .and_then(|text| text_in(&text, provider, "api_key"))
            .filter(|key| !key.is_empty());
        written.or_else(|| std::env::var(variable).ok().filter(|key| !key.is_empty()))
    }
}

/// The Host's engines that listen, and what it keeps for them.
pub struct Ears {
    /// The Host's own engine, and the model it runs (`small.en`). `None`: this Host has
    /// none of its own.
    own: Option<(Arc<dyn Listener>, String)>,
    /// ElevenLabs Scribe, which is there when its key is.
    scribe: Arc<dyn Listener>,
    keys: Keys,
    /// The engine that hears a clip nobody named one for: `local`, or a cloud engine's name.
    engine: String,
    /// The language that is spoken to this Host's agents.
    language: String,
    /// Where answers are kept for requests that come again.
    kept: PathBuf,
    /// The jobs that are being answered now, so that the same job is answered once at a time.
    jobs: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
}

/// The name a client gives the one cloud engine this Host has.
const SCRIBE: &str = "elevenlabs:scribe_v2";

/// Why a clip was not turned into text: the status and the words an app is given.
type Refused = (StatusCode, String);

impl Ears {
    /// The engines this Host was configured with. The Host's own is started at once, so
    /// that its model is loaded when the first clip comes.
    pub fn new(config: &Config) -> Self {
        let own = (!config.whisper_python.is_empty()).then(|| {
            let worker = Worker {
                args: vec![config.whisper_worker.to_string_lossy().to_string()],
                compute_type: config.whisper_compute.clone(),
                language: Some(config.speech_language.clone()).filter(|said| !said.is_empty()),
                scratch: config.run_dir.join("heard"),
                ..Worker::new(&config.whisper_python, &config.whisper_source)
            };
            let whisper = Whisper::new(worker);
            whisper.warm();
            let engine: Arc<dyn Listener> = Arc::new(whisper);
            (engine, config.whisper_model.clone())
        });
        let keys = Keys::new(config.speech_keys.clone());
        let read = keys.clone();
        let mut scribe = Scribe::new("scribe_v2", Box::new(move || read.of("elevenlabs")));
        if !config.scribe_endpoint.is_empty() {
            scribe = scribe.at(config.scribe_endpoint.clone());
        }
        Self {
            own,
            scribe: Arc::new(scribe),
            keys,
            engine: config.transcription_engine.clone(),
            language: config.speech_language.clone(),
            kept: config.data_dir.join("transcriptions"),
            jobs: Mutex::default(),
        }
    }

    /// The name a client gives the Host's own engine.
    fn own_id(&self) -> Option<String> {
        let (_, model) = self.own.as_ref()?;
        Some(format!("{OWN_PROVIDER}:{model}"))
    }

    /// Whether a provider's engines can be used on this Host: it is built here and has
    /// its key.
    fn has(&self, provider: &str) -> bool {
        provider == "elevenlabs" && self.keys.of(provider).is_some()
    }

    /// Which engine hears a clip for which the app asked for `requested` (nothing: whatever
    /// this Host hears with). With the engine comes what is said while it loads.
    fn chosen(&self, requested: &str) -> Result<(Arc<dyn Listener>, String), Refused> {
        if self.own.is_none() && !self.has("elevenlabs") {
            return Err((StatusCode::SERVICE_UNAVAILABLE, heard::DISABLED.to_string()));
        }
        // The cloud engine chosen for this Host stands in for its own; what an app asks
        // for by name still comes first.
        let stands_in =
            heard::wants_cloud_engine(requested, &[], false) && heard::is_cloud(&self.engine);
        let cloud = if stands_in { self.engine.as_str() } else { "" };
        let model = heard::plan(requested, &[], cloud, "", false).model;
        let own_id = self.own_id();
        let is_own = matches!(model.as_str(), "" | "local" | "server-default")
            || own_id.as_deref() == Some(model.as_str());
        if is_own {
            let Some((engine, _)) = &self.own else {
                return Err((StatusCode::SERVICE_UNAVAILABLE, heard::DISABLED.to_string()));
            };
            let loading = match model.as_str() {
                "" => heard::LOADING.to_string(),
                named => format!("transcription model loading: {named}"),
            };
            return Ok((engine.clone(), loading));
        }
        if !heard::is_cloud(&model) {
            return Err((StatusCode::BAD_REQUEST, heard::not_installed(&model)));
        }
        let Some((provider, _)) = heard::cloud_model(&model) else {
            let unknown = format!("unknown transcription engine: {model}");
            return Err((StatusCode::INTERNAL_SERVER_ERROR, unknown));
        };
        if model != SCRIBE {
            // An engine the contract lists and this Host does not have.
            return Err((StatusCode::BAD_REQUEST, heard::not_installed(&model)));
        }
        if self.keys.of(provider.id).is_none() {
            let no_key = format!("{} API key is not configured", provider.id);
            return Err((StatusCode::INTERNAL_SERVER_ERROR, no_key));
        }
        Ok((self.scribe.clone(), heard::LOADING.to_string()))
    }

    /// The one who may answer this job now. A job without a name is nobody's twice.
    fn turn_for(&self, job: &str) -> Option<Arc<tokio::sync::Mutex<()>>> {
        if job.is_empty() {
            return None;
        }
        let mut jobs = self.jobs.lock().unwrap_or_else(PoisonError::into_inner);
        // Jobs nobody waits for any more are forgotten here, where the list is held anyway.
        jobs.retain(|_, turn| Arc::strong_count(turn) > 1);
        Some(jobs.entry(job.to_string()).or_default().clone())
    }

    fn file_of(&self, job: &str) -> PathBuf {
        let name: String = Sha256::digest(job.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        self.kept.join(format!("{name}.json"))
    }

    /// What was answered to this job before, when it was: `Err` when it was another clip.
    fn answered(&self, job: &str, fingerprint: &str) -> Result<Option<Value>, &'static str> {
        let Ok(text) = std::fs::read_to_string(self.file_of(job)) else {
            return Ok(None);
        };
        let Ok(kept) = serde_json::from_str::<Value>(&text) else {
            return Ok(None); // half written by a Host that was cut off: as good as not there
        };
        if kept["fingerprint"].as_str() != Some(fingerprint) {
            return Err(heard::REUSED_ID);
        }
        Ok(Some(kept["answer"].clone()))
    }

    /// Keeps the answer to a job, whole or not at all.
    fn keep(&self, job: &str, fingerprint: &str, answer: &Value) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.kept)?;
        let file = self.file_of(job);
        let being_written = file.with_extension(format!("{}.part", ids::hex(4)));
        let kept = json!({ "fingerprint": fingerprint, "answer": answer });
        std::fs::write(&being_written, kept.to_string())?;
        std::fs::rename(&being_written, &file)
    }

    /// Forgets the answers nobody will ask for again.
    pub fn forget_old(&self) {
        let Ok(files) = std::fs::read_dir(&self.kept) else {
            return;
        };
        let now = SystemTime::now();
        for file in files.flatten() {
            let written = file.metadata().and_then(|about| about.modified());
            let old =
                written.is_ok_and(|at| now.duration_since(at).is_ok_and(|age| age > KEPT_FOR));
            if old {
                let _ = std::fs::remove_file(file.path());
            }
        }
    }
}

/// What an app is told when a clip was not turned into text: a status, and a body that
/// says why under `error`, as every refusal of the Host does.
type Told = (StatusCode, Value);

fn refuse(status: StatusCode, why: impl Into<String>) -> Told {
    (status, json!({ "error": why.into() }))
}

/// The names an engine is asked to listen for: what the agents on this Host are called.
fn names(app: &App) -> String {
    let directory = app.fleet.directory();
    let mut names: Vec<&str> = Vec::new();
    for member in &directory.members {
        let record = &member.record;
        let there = record.deleted_at_ms.is_none() && record.archived_at_ms.is_none();
        let name = record.persona.trim();
        if there && !name.is_empty() && !name.contains(',') && !names.contains(&name) {
            names.push(name);
        }
        if names.len() == NAMES {
            break;
        }
    }
    names.join(", ")
}

/// `POST /transcribe`: a clip of speech is the body. Headers say which engine the app asks
/// for (`X-Transcription-Model`), what the job is called (`X-Transcription-ID`) and whether
/// it was said hands-free (`X-Hands-Free`). Answered with the text and the trace the
/// message will travel under when the app sends it.
async fn transcribe(State(app): State<App>, request: Request) -> Response {
    match heard_in(&app, request).await {
        Ok(answer) => Json(answer).into_response(),
        Err((status, why)) => (status, Json(why)).into_response(),
    }
}

async fn heard_in(app: &App, request: Request) -> Result<Value, Told> {
    let (parts, body) = request.into_parts();
    let headers = &parts.headers;
    let length = header_text(headers, "content-length")
        .parse::<usize>()
        .ok()
        .filter(|length| (1..=heard::LARGEST_CLIP).contains(length))
        .ok_or_else(|| refuse(StatusCode::BAD_REQUEST, heard::BAD_SIZE))?;
    let audio = axum::body::to_bytes(body, length)
        .await
        .ok()
        .filter(|audio| audio.len() == length)
        .ok_or_else(|| refuse(StatusCode::REQUEST_TIMEOUT, "incomplete audio upload"))?;
    let requested = header_text(headers, "x-transcription-model");
    let content_type = match header_text(headers, "content-type") {
        none if none.is_empty() => "audio/webm".to_string(),
        given => given,
    };
    let hands_free = heard::truthy(Some(&header_text(headers, "x-hands-free")));
    let job = heard::job_id(Some(&header_text(headers, "x-transcription-id")))
        .map_err(|why| refuse(StatusCode::BAD_REQUEST, why))?;
    let fingerprint = heard::fingerprint(&audio, &content_type, &requested, hands_free);
    if headers.contains_key("x-clarp-call-id") {
        // A clip that names its call is a call's, or nothing: it is never taken as said in
        // a chat. This Host has no calls yet.
        let none = json!({ "ok": false, "error": "calls are not available on this Host yet" });
        return Err((StatusCode::CONFLICT, none));
    }

    // The same job is answered once at a time: a request that comes again while the first
    // is still being heard waits for it, and is then given what the first was given.
    let ears = app.ears.clone();
    let turn = ears.turn_for(&job);
    let _held = match &turn {
        Some(turn) => Some(turn.lock().await),
        None => None,
    };
    if !job.is_empty() {
        let (asked, named, print) = (ears.clone(), job.clone(), fingerprint.clone());
        let before = tokio::task::spawn_blocking(move || asked.answered(&named, &print)).await;
        match before {
            Ok(Ok(Some(answer))) => return Ok(heard::again(answer)),
            Ok(Err(why)) => return Err(refuse(StatusCode::CONFLICT, why)),
            Ok(Ok(None)) | Err(_) => {}
        }
    }

    let (engine, loading) = ears
        .chosen(&requested)
        .map_err(|(status, why)| refuse(status, why))?;
    let listen_for = names(app);
    let language = Some(ears.language.clone()).filter(|language| !language.is_empty());
    let (clip, kind) = (audio.clone(), content_type.clone());
    let said = tokio::task::spawn_blocking(move || {
        let clip = Clip {
            audio: &clip,
            content_type: &kind,
        };
        let asked = Asked {
            listen_for: &listen_for,
            language: language.as_deref(),
            wait: WAITS,
        };
        engine.hear(clip, asked)
    })
    .await;
    let said = match said {
        Ok(Ok(said)) => said,
        Ok(Err(Deaf::Loading)) => return Err(refuse(StatusCode::SERVICE_UNAVAILABLE, loading)),
        Ok(Err(Deaf::Busy)) => return Err(refuse(StatusCode::TOO_MANY_REQUESTS, heard::BUSY)),
        Ok(Err(Deaf::Failed(why))) => {
            // Why, and nothing of what was said: an engine's words hold none of it.
            eprintln!("clarp-host: a clip was not turned into text: {why}");
            return Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, why));
        }
        Err(error) => {
            eprintln!("clarp-host: hearing a clip was cut off: {error}");
            let cut_off = "the clip could not be heard";
            return Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, cut_off));
        }
    };
    // What the engine was sure was speech, unless it is something engines invent on silence.
    let text = heard::kept(&heard::joined(&said.segments));
    let answer = heard::answer(&text, &ids::hex(8), hands_free);
    if !job.is_empty() {
        let (keeps, kept) = (ears.clone(), answer.clone());
        let done = tokio::task::spawn_blocking(move || keeps.keep(&job, &fingerprint, &kept)).await;
        if !matches!(done, Ok(Ok(()))) {
            eprintln!("clarp-host: the answer to a clip could not be kept");
            let not_kept = "transcription result persistence failed";
            return Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, not_kept));
        }
    }
    Ok(answer)
}

/// The engines of the contract's providers as an app lists them, each with whether it can
/// be used on this Host.
fn cloud_models(ears: &Ears) -> Vec<Value> {
    let mut rows = Vec::new();
    for provider in &PROVIDERS {
        let available = ears.has(provider.id);
        for model in provider.models {
            rows.push(json!({
                "id": model.id, "name": model.name, "provider": provider.id,
                "model": model.model, "weight": "cloud", "available": available,
                "credential": provider.credential, "turn_detection": provider.turn_detection,
                "biasing": model.biasing,
                "budget": { "unit": "terms", "capacity": model.capacity,
                            "max_term_chars": model.max_term_chars },
            }));
        }
    }
    rows
}

/// `GET /transcription-capabilities`: which engines an app may ask for by name, and which
/// one hears a clip nobody named one for.
async fn capabilities(State(app): State<App>) -> Json<Value> {
    let ears = &app.ears;
    let mut models = Vec::new();
    if let (Some(id), Some((_, model))) = (ears.own_id(), &ears.own) {
        models.push(json!({
            "id": id, "name": heard::label(model), "provider": OWN_PROVIDER, "model": model,
            "weight": heard::weight(model),
        }));
    }
    let usable = |row: &Value| row["available"] == true;
    models.extend(cloud_models(ears).into_iter().filter(usable));
    Json(json!({
        "available": !models.is_empty(),
        "default_model": ears.own_id().unwrap_or_default(),
        "engine": ears.engine,
        "turn_taking": "native",
        "models": models,
        "catalog": [],
        "adapters": [],
    }))
}

/// `GET /transcription-providers`: what the settings of speech in are on this Host, and
/// what could be chosen. Where a turn of speech ends is found on the phone itself.
async fn providers(State(app): State<App>) -> Json<Value> {
    let ears = &app.ears;
    let models = cloud_models(ears);
    let listed: Vec<Value> = PROVIDERS
        .iter()
        .map(|provider| {
            json!({
                "id": provider.id, "name": provider.name, "kind": "cloud",
                "credential": provider.credential, "turn_detection": provider.turn_detection,
                "available": ears.has(provider.id),
            })
        })
        .collect();
    Json(json!({
        "engine": ears.engine,
        "language": ears.language,
        "agent_languages": {},
        "agent_engines": {},
        "call_languages": "",
        "call_engines": "",
        "call_policy_error": "",
        "turn_taking": "native",
        "retain_audio": false,
        "long_form_model": "",
        "long_form_threshold_sec": 30,
        "long_form_threshold_range": { "min": 1, "max": 3600 },
        "stream_path": "",
        "provider_turn_taking_available": false,
        "turn_taking_options": [
            { "id": "native", "name": "Native (on-device VAD + Smart Turn)", "available": true },
            { "id": "provider", "name": "Provider-owned end of turn", "available": false },
        ],
        "providers": listed,
        "models": models,
    }))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/transcribe", post(transcribe))
        .route("/transcription-capabilities", get(capabilities))
        .route("/transcription-providers", get(providers))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_key_is_read_from_its_table_of_a_file_in_toml() {
        let file = r#"
# The Python Host's own file, as much of it as matters here.
[server]
api_key = "the server's, which is another"

[elevenlabs]  # speech
model = 'eleven_v3'
api_key = "sk_\"quoted\"\\ and é \n end" # the key
speed = 1.1

[[voices]]
api_key = "in a list of tables"

[whisper]
model = "small.en"
compute = 'int8'
enabled = true
"#;
        assert_eq!(
            text_in(file, "elevenlabs", "api_key").as_deref(),
            Some("sk_\"quoted\"\\ and \u{e9} \n end")
        );
        assert_eq!(
            text_in(file, "elevenlabs", "model").as_deref(),
            Some("eleven_v3")
        );
        assert_eq!(
            text_in(file, "whisper", "model").as_deref(),
            Some("small.en")
        );
        assert_eq!(text_in(file, "whisper", "compute").as_deref(), Some("int8"));
        // What is no plain text is none: a number, a truth, a table that is not there.
        assert_eq!(text_in(file, "elevenlabs", "speed"), None);
        assert_eq!(text_in(file, "whisper", "enabled"), None);
        assert_eq!(text_in(file, "voices", "api_key"), None);
        assert_eq!(text_in(file, "deepgram", "api_key"), None);
        assert_eq!(text_in("[a]\nkey = \"never closed", "a", "key"), None);
        assert_eq!(
            text_in("[a]\nkey = \"\"\"several\nlines\"\"\"", "a", "key"),
            None
        );
        assert_eq!(
            text_in("[a]\n\"key\" = \"quoted name\"", "a", "key").as_deref(),
            Some("quoted name")
        );
        assert_eq!(
            text_in("key = \"before any table\"\n[a]\n", "a", "key"),
            None
        );
    }

    #[test]
    fn a_key_is_read_from_the_file_each_time_it_is_needed() {
        let dir = std::env::temp_dir().join(format!("clarp-keys-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("config.toml");
        std::fs::write(&file, "[elevenlabs]\napi_key = \"from-the-file\"\n").unwrap();
        let keys = Keys::new(Some(file.clone()));
        assert_eq!(keys.of("elevenlabs").as_deref(), Some("from-the-file"));
        assert_eq!(
            keys.of("deepgram"),
            None,
            "an engine this Host does not have"
        );
        // The owner changes the key: the next clip is heard with the new one.
        std::fs::write(&file, "[elevenlabs]\napi_key = \"changed\"\n").unwrap();
        assert_eq!(keys.of("elevenlabs").as_deref(), Some("changed"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
