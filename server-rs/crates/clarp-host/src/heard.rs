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
//!
//! A clip is for the agent its user is looking at. That agent can have a language and
//! engines of its own ([`clarp_kernel::languages`]): its first engine hears the clip, and
//! the next one only when the one before failed, so that one provider's outage does not
//! lose what was said. Only the last one's failure is the answer. These are settings the
//! owner makes from an app (`POST /transcription-providers`), kept with the fleet.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime};

use axum::extract::{Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_kernel::heard::{self, OWN_PROVIDER, PROVIDERS};
use clarp_kernel::languages;
use clarp_kernel::said::Who;
use clarp_speech::chirp::Chirp;
use clarp_speech::listen::{Asked, Clip, Deaf, Heard, Listener};
use clarp_speech::nova::Nova;
use clarp_speech::scribe::Scribe;
use clarp_speech::whisper::{self, Whisper, Worker};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::config::Config;
use crate::edge::{ApiError, App};
use crate::fleet::Directory;
use crate::ids;
use crate::media::header_text;
use crate::owner::{self, OwnerFile};

/// How long an answer is kept for a request that may come again.
const KEPT_FOR: Duration = Duration::from_secs(30 * 24 * 60 * 60);

/// How long a clip waits for an engine that hears one at a time. What an app sends here is
/// the whole of what was said, not a guess along the way: it waits rather than be dropped.
const WAITS: Duration = Duration::from_secs(10);

/// The Host's engines that listen, and what it keeps for them.
pub struct Ears {
    pub(crate) judge: crate::vocabulary::judge::Judge,
    /// The Host's own engine, and the model it runs (`small.en`). `None`: this Host has
    /// none of its own.
    own: Option<(Arc<dyn Listener>, String)>,
    /// ElevenLabs Scribe, which is there when its key is.
    scribe: Arc<dyn Listener>,
    /// Deepgram Nova, which is there when its key is.
    nova: Arc<dyn Listener>,
    ink: Arc<dyn Listener>,
    ink_whisper: Arc<dyn Listener>,
    /// Google's Chirp, which is there when the owner's file names a project and his
    /// account's file can be read.
    chirp: Arc<dyn Listener>,
    /// Where keys are read: the owner's own file, then this Host's environment.
    keys: OwnerFile,
    /// The engine that hears a clip nobody named one for, unless the owner set another
    /// from an app: `local`, or a cloud engine's name.
    engine: String,
    /// The language that is spoken to this Host's agents, unless the owner set another.
    language: String,
    /// Where answers are kept for requests that come again.
    kept: PathBuf,
    /// The jobs that are being answered now, so that the same job is answered once at a time.
    jobs: Mutex<HashMap<String, Arc<tokio::sync::Mutex<()>>>>,
}

/// The names a client gives the engines over the network that this Host has.
const SCRIBE: &str = "elevenlabs:scribe_v2";
const NOVA: &str = "deepgram:nova-3";
const CHIRP: &str = "google:chirp_3";

/// Where the owner's choice of the engine that hears by default is kept, and how a turn of
/// speech is found to end (the Python Host's keys).
const ENGINE_KEY: &str = "transcription.engine";
const TURN_TAKING_KEY: &str = "transcription.turn_taking";
const LONG_FORM_THRESHOLD_KEY: &str = "transcription.long_form_threshold_sec";

/// An engine that was chosen for a clip.
struct Chosen {
    engine: Arc<dyn Listener>,
    /// What is said while it loads.
    loading: String,
    /// The provider of an engine over the network. `None`: the Host's own.
    provider: Option<&'static str>,
}

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
                // Half the cores, as the Python Host gives its own: 4 where the count is not known.
                cpu_threads: whisper::threads_for(
                    std::thread::available_parallelism().map_or(4, std::num::NonZero::get),
                ),
                language: languages::own_language(&config.whisper_model).map(str::to_string),
                scratch: config.run_dir.join("heard"),
                ..Worker::new(&config.whisper_python, &config.whisper_source)
            };
            let whisper = Whisper::new(worker);
            whisper.warm();
            let engine: Arc<dyn Listener> = Arc::new(whisper);
            (engine, config.whisper_model.clone())
        });
        let keys = OwnerFile::new(config.speech_keys.clone());
        let read = keys.clone();
        let mut scribe = Scribe::new("scribe_v2", Box::new(move || read.key("elevenlabs")));
        if !config.scribe_endpoint.is_empty() {
            scribe = scribe.at(config.scribe_endpoint.clone());
        }
        let read = keys.clone();
        let mut nova = Nova::new("nova-3", Box::new(move || read.key("deepgram")));
        if !config.nova_endpoint.is_empty() {
            nova = nova.at(config.nova_endpoint.clone());
        }
        // The owner's account at Google is read where he keeps it, each time it is needed.
        let read = keys.clone();
        let account = move || read.read().and_then(|file| owner::google_speech(&file));
        let mut chirp = Chirp::new("chirp_3", Box::new(account));
        if !config.google_speech_endpoint.is_empty() {
            chirp = chirp.at(
                config.google_token_endpoint.clone(),
                config.google_speech_endpoint.clone(),
            );
        }
        if !config.chirp_decoder.is_empty() {
            chirp = chirp.decoded_by(vec![config.chirp_decoder.clone()]);
        }
        let ink = |model| -> Arc<dyn Listener> {
            let owner = keys.clone();
            Arc::new(clarp_speech::ink::Ink::new(
                model,
                Box::new(move || owner.key("cartesia")),
                config.ink_endpoint.clone(),
                &config.chirp_decoder,
            ))
        };
        Self {
            own,
            judge: Default::default(),
            scribe: Arc::new(scribe),
            nova: Arc::new(nova),
            ink: ink("ink-2"),
            ink_whisper: ink("ink-whisper"),
            chirp: Arc::new(chirp),
            keys,
            engine: config.transcription_engine.clone(),
            language: config.speech_language.clone(),
            kept: config.data_dir.join("transcriptions"),
            jobs: Mutex::default(),
        }
    }

    /// The key of a provider of engines, where the owner has one for it: read now, kept
    /// nowhere.
    pub(crate) fn key(&self, provider: &str) -> Option<String> {
        self.keys.key(provider)
    }

    /// The name a client gives the Host's own engine.
    fn own_id(&self) -> Option<String> {
        let (_, model) = self.own.as_ref()?;
        Some(format!("{OWN_PROVIDER}:{model}"))
    }

    /// Whether a provider's engines can be used on this Host: it is built here and has
    /// what it needs: its key, or for Google the owner's project and account.
    fn has(&self, provider: &str) -> bool {
        match provider {
            "elevenlabs" | "deepgram" | "cartesia" => self.keys.key(provider).is_some(),
            "google" => {
                let file = self.keys.read();
                file.is_some_and(|file| owner::google_speech(&file).is_some())
            }
            _ => false,
        }
    }

    /// The engine that hears a clip nobody named one for: what the owner set from an app,
    /// else what this Host was started with.
    pub(crate) fn default_engine(&self, directory: &Directory) -> String {
        let set = directory.setting(ENGINE_KEY).map(str::trim);
        let set = set.filter(|engine| !engine.is_empty());
        set.unwrap_or(&self.engine).to_string()
    }

    /// The engines a clip is tried on, in order: the first hears it, and each next one only
    /// when the one before failed. `requested` is what the app asked for by name (nothing:
    /// whatever this Host hears with), `own` are the engines of the agent the clip is for,
    /// which come before what an app asks for.
    fn order(&self, requested: &str, own: &[String], default_engine: &str) -> Vec<String> {
        // The cloud engine chosen for this Host stands in for its own; what an app asks
        // for by name still comes first.
        let stands_in =
            heard::wants_cloud_engine(requested, own, false) && heard::is_cloud(default_engine);
        let cloud = if stands_in { default_engine } else { "" };
        let plan = heard::plan(requested, own, cloud, "", false);
        let mut order = vec![plan.model];
        order.extend(plan.fallbacks);
        order
    }

    /// The engine a name stands for on this Host. With the engine comes what is said while
    /// it loads.
    fn engine_of(&self, model: &str) -> Result<Chosen, Refused> {
        let built = ["elevenlabs", "deepgram", "google"];
        if self.own.is_none() && !built.iter().any(|provider| self.has(provider)) {
            return Err((StatusCode::SERVICE_UNAVAILABLE, heard::DISABLED.to_string()));
        }
        let own_id = self.own_id();
        let is_own =
            matches!(model, "" | "local" | "server-default") || own_id.as_deref() == Some(model);
        if is_own {
            let Some((engine, _)) = &self.own else {
                return Err((StatusCode::SERVICE_UNAVAILABLE, heard::DISABLED.to_string()));
            };
            let loading = match model {
                "" => heard::LOADING.to_string(),
                named => format!("transcription model loading: {named}"),
            };
            return Ok(Chosen {
                engine: engine.clone(),
                loading,
                provider: None,
            });
        }
        if !heard::is_cloud(model) {
            return Err((StatusCode::BAD_REQUEST, heard::not_installed(model)));
        }
        let Some((provider, _)) = heard::cloud_model(model) else {
            let unknown = format!("unknown transcription engine: {model}");
            return Err((StatusCode::INTERNAL_SERVER_ERROR, unknown));
        };
        let engine = match model {
            SCRIBE => &self.scribe,
            NOVA => &self.nova,
            "cartesia:ink-2" => &self.ink,
            "cartesia:ink-whisper" => &self.ink_whisper,
            CHIRP => &self.chirp,
            // An engine the contract lists and this Host does not have.
            _ => return Err((StatusCode::BAD_REQUEST, heard::not_installed(model))),
        };
        if !self.has(provider.id) {
            let no_key = format!("{} API key is not configured", provider.id);
            return Err((StatusCode::INTERNAL_SERVER_ERROR, no_key));
        }
        Ok(Chosen {
            engine: engine.clone(),
            loading: heard::LOADING.to_string(),
            provider: Some(provider.id),
        })
    }

    /// Has one engine hear a clip. `language` is the language of the agent the clip is
    /// for: an engine over the network is handed it as that engine takes it, and the
    /// Host's own is told what its model knows.
    async fn heard_by(
        self: &Arc<Self>,
        model: &str,
        language: &str,
        listen_for: &str,
        clip: (&axum::body::Bytes, &str),
    ) -> Result<Heard, Told> {
        let chosen = self
            .engine_of(model)
            .map_err(|(status, why)| refuse(status, why))?;
        let handed = match chosen.provider {
            Some(provider) => languages::handed(language, provider)
                .map_err(|why| refuse(StatusCode::INTERNAL_SERVER_ERROR, why))?,
            None => {
                let own = self.own.as_ref().map(|(_, model)| model.as_str());
                let knows = own.and_then(languages::own_language);
                knows.map(str::to_string)
            }
        };
        let (engine, listen_for) = (chosen.engine, listen_for.to_string());
        let (audio, kind) = (clip.0.clone(), clip.1.to_string());
        let said = tokio::task::spawn_blocking(move || {
            let clip = Clip {
                audio: &audio,
                content_type: &kind,
            };
            let asked = Asked {
                listen_for: &listen_for,
                language: handed.as_deref(),
                wait: WAITS,
            };
            engine.hear(clip, asked)
        })
        .await;
        match said {
            Ok(Ok(said)) => Ok(said),
            Ok(Err(Deaf::Loading)) => Err(refuse(StatusCode::SERVICE_UNAVAILABLE, chosen.loading)),
            Ok(Err(Deaf::Busy)) => Err(refuse(StatusCode::TOO_MANY_REQUESTS, heard::BUSY)),
            Ok(Err(Deaf::Failed(why))) => Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, why)),
            Err(_) => {
                let cut_off = "the clip could not be heard";
                Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, cut_off))
            }
        }
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

    /// Forgets the answer to a job: the app has what it asked for and will not ask again.
    fn forget(&self, job: &str) {
        let _ = std::fs::remove_file(self.file_of(job));
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
        // A clip that names its call is heard by its call's own rules, or not at all: it is
        // never taken as said in a chat. This Host has calls, and not yet rules of their own
        // for how each is heard; an app sends such a clip only to a Host that says it has.
        let why = "how the clips of a call are heard is not built on this Host yet";
        let none = json!({ "ok": false, "error": why });
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

    // The clip is for the agent its user is looking at: that agent's engines, when it has
    // its own, and its language.
    let directory = app.fleet.directory();
    let (own, language) = spoken_to(&directory, &ears.language);
    let order = ears.order(&requested, &own, &ears.default_engine(&directory));
    let trace = ids::hex(8);
    let session = directory
        .focus
        .as_deref()
        .and_then(|id| directory.named(id))
        .map_or("", |m| m.record.session.as_str());
    let selected = order.first().map_or(requested.as_str(), String::as_str);
    let began = std::time::Instant::now();
    let (audit, run_id) =
        crate::vocabulary::compile(app, session, selected, hands_free, Some(&trace))
            .await
            .unwrap_or_default();
    let listen_for = audit["payload"].as_str().unwrap_or("");
    let mut said = None;
    for (place, model) in order.iter().enumerate() {
        let heard = ears
            .heard_by(
                model,
                &language,
                listen_for,
                (&audio, content_type.as_str()),
            )
            .await;
        match heard {
            Ok(heard) => {
                said = Some(heard);
                break;
            }
            Err((status, why)) => {
                // Why, and nothing of what was said: an engine's words hold none of it.
                let engine = if model.is_empty() {
                    "the Host's own"
                } else {
                    model
                };
                let words = why["error"].as_str().unwrap_or("");
                if place + 1 == order.len() {
                    if status == StatusCode::INTERNAL_SERVER_ERROR {
                        eprintln!(
                            "clarp-host: a clip was not turned into text ({engine}): {words}"
                        );
                    }
                    return Err((status, why));
                }
                eprintln!(
                    "clarp-host: {engine} did not hear a clip, the next engine is asked: {words}"
                );
            }
        }
    }
    let Some(said) = said else {
        let nobody = "no engine heard the clip";
        return Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, nobody));
    };
    // What the engine was sure was speech, unless it is something engines invent on silence.
    let text = crate::vocabulary::judge::kept(app, heard::joined(&said.segments)).await;
    let mut answer = heard::answer(&text, &trace, hands_free);
    if run_id > 0 {
        crate::vocabulary::finish(
            app.config.data_dir.clone(),
            run_id,
            text.clone(),
            began.elapsed().as_millis() as i64,
        )
        .await;
        answer["vocab_run_id"] = json!(run_id);
    }
    if crate::vocabulary::audio::enabled(&app.fleet.directory()) {
        let root = app.config.data_dir.clone();
        let (trace, model, session) = (trace.clone(), requested.clone(), session.to_string());
        let now = app.clock.now_ms();
        let _ = tokio::task::spawn_blocking(move || {
            crate::vocabulary::audio::retain(
                &root,
                crate::vocabulary::audio::Clip {
                    trace: &trace,
                    data: &audio,
                    kind: &content_type,
                    session: &session,
                    run: run_id,
                    model: &model,
                },
                now,
            )
        })
        .await;
    }
    // A yes or a no to an agent whose hand is up is a word to the herald, and no message:
    // the app is told, and sends nothing.
    if !text.is_empty() && app.mouth.heard(&text).await {
        answer["herald_consumed"] = json!(true);
    }
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

/// `DELETE /transcription-results/<job>`: the app has sent what was said and will not ask
/// for this job's answer again. A job that is not kept is forgotten just the same.
async fn forget(State(app): State<App>, Path(job): Path<String>) -> Response {
    let job = match heard::job_id(Some(&job)) {
        Ok(job) => job,
        Err(why) => {
            let (status, why) = refuse(StatusCode::BAD_REQUEST, why);
            return (status, Json(why)).into_response();
        }
    };
    let ears = app.ears.clone();
    // Not in the middle of the job's own answer: after it.
    let turn = ears.turn_for(&job);
    let _held = match &turn {
        Some(turn) => Some(turn.lock().await),
        None => None,
    };
    if !job.is_empty() {
        let _ = tokio::task::spawn_blocking(move || ears.forget(&job)).await;
    }
    Json(json!({ "ok": true })).into_response()
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

/// The engines and the language of the agent a clip is for: the agent its user is looking
/// at. Its own engines, in the order they are tried (none: it is heard like every agent),
/// and its own language, else the Host's: what the owner set from an app, else what the
/// Host was started with.
fn spoken_to(directory: &Directory, started_with: &str) -> (Vec<String>, String) {
    let looked_at = directory.focus.as_deref().and_then(|agent_id| {
        let mut members = directory.members.iter();
        members.find(|member| member.record.agent_id == agent_id)
    });
    let who = looked_at.map(|member| Who {
        name: &member.record.persona,
        persona: &member.record.persona,
        session: &member.record.session,
    });
    let engines = languages::stored(directory.setting(languages::AGENT_ENGINES_KEY));
    let own = who.and_then(|who| languages::of_agent(&engines, who));
    let spoken = languages::languages(directory.setting(languages::AGENT_LANGUAGES_KEY));
    let language = match who.and_then(|who| languages::of_agent(&spoken, who)) {
        Some(own) => own.to_string(),
        None => languages::selected(directory.setting(languages::LANGUAGE_KEY), started_with),
    };
    (own.map(languages::engines).unwrap_or_default(), language)
}

/// A map of single agents as an app is told it: each name with its text.
fn told_map(entries: Vec<(String, String)>) -> Value {
    let fields = entries.into_iter();
    Value::Object(
        fields
            .map(|(name, text)| (name, Value::String(text)))
            .collect(),
    )
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
        "engine": ears.default_engine(&app.fleet.directory()),
        "turn_taking": turn_taking(&app.fleet.directory()),
        "models": models,
        "catalog": [],
        "adapters": [],
    }))
}

/// How the end of a turn of speech is found, as the owner set it: on the phone (`native`),
/// unless he chose that the engine finds it on a stream (`provider`).
fn turn_taking(directory: &Directory) -> &'static str {
    match directory.setting(TURN_TAKING_KEY) {
        Some("provider") => "provider",
        _ => "native",
    }
}

/// Whether the engine that is chosen is one that finds the ends of turns itself, and
/// whether this Host can use it.
fn finds_turns(ears: &Ears, engine: &str) -> (bool, bool) {
    let chosen = heard::cloud_model(engine);
    let finds = chosen.is_some_and(|(provider, _)| provider.turn_detection == "own");
    let usable = chosen.is_some_and(|(provider, _)| ears.has(provider.id));
    (finds, finds && usable)
}

/// `GET /transcription-providers`: what the settings of speech in are on this Host, and
/// what could be chosen. Where a turn of speech ends is found on the phone itself, unless
/// the engine that hears finds it on a stream (`listening.rs`).
async fn providers(State(app): State<App>) -> Json<Value> {
    Json(status(&app))
}

/// How long a recording is before it would go to a stronger engine, as it is kept: thirty
/// seconds unless the owner set another.
fn long_form_threshold(directory: &Directory) -> i64 {
    let kept = directory.setting(LONG_FORM_THRESHOLD_KEY);
    kept.and_then(|kept| kept.parse().ok()).unwrap_or(30)
}

async fn vocabulary_budgets(State(app): State<App>) -> Json<Value> {
    let directory = app.fleet.directory();
    Json(json!({
        "budgets": *clarp_kernel::vocabulary::BUDGETS,
        "engine": app.ears.default_engine(&directory),
        "models": cloud_models(&app.ears),
    }))
}

/// The settings of speech in, as an app's screen draws them.
fn status(app: &App) -> Value {
    let ears = &app.ears;
    let directory = app.fleet.directory();
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
    let engine = ears.default_engine(&directory);
    let (finds, usable) = finds_turns(ears, &engine);
    json!({
        "engine": engine,
        "language": languages::selected(directory.setting(languages::LANGUAGE_KEY), &ears.language),
        "agent_languages": told_map(languages::languages(directory.setting(languages::AGENT_LANGUAGES_KEY))),
        "agent_engines": told_map(languages::stored(directory.setting(languages::AGENT_ENGINES_KEY))),
        "call_languages": "",
        "call_engines": "",
        "call_policy_error": "",
        "turn_taking": turn_taking(&directory),
        "retain_audio": crate::vocabulary::audio::enabled(&directory),
        "long_form_model": "",
        "long_form_threshold_sec": long_form_threshold(&directory),
        "long_form_threshold_range": { "min": 1, "max": 3600 },
        "stream_path": "/stt/stream",
        "provider_turn_taking_available": usable,
        "turn_taking_options": [
            { "id": "native", "name": "Native (on-device VAD + Smart Turn)", "available": true },
            { "id": "provider", "name": "Provider-owned end of turn", "available": finds },
        ],
        "providers": listed,
        "models": models,
    })
}

/// `POST /transcription-providers`: the owner changes the settings of speech in from an
/// app. Only what a save names is changed. The rules, their order and the words a change
/// is refused in are the Python Host's (`stt_providers.update_settings`); what that Host
/// can be set to and this one has not built is refused, and says so.
async fn set_providers(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let bad = |why: &str| ApiError::new(StatusCode::BAD_REQUEST, why);
    // No body at all is an empty save.
    let data = match body.is_empty() {
        true => serde_json::Map::new(),
        false => match serde_json::from_slice::<Value>(&body) {
            Ok(Value::Object(data)) => data,
            _ => return Err(bad("bad json")),
        },
    };
    let named = |key: &str| data.get(key).filter(|value| !value.is_null());
    let texts = ["engine", "turn_taking"].map(|key| data.get(key));
    if texts.iter().flatten().any(|value| !value.is_string()) {
        return Err(bad("engine and turn_taking must be strings"));
    }
    let directory = app.fleet.directory();
    let mut settings: Vec<(String, String)> = Vec::new();
    // An engine the contract lists is one that can be named, whether this Host can use it
    // or not; a name with a colon that the contract does not list is a model of the
    // Host's own kind, which nobody checks here.
    let unknown = |engine: &str| {
        heard::cloud_model(engine).is_none() && (heard::is_cloud(engine) || !engine.contains(':'))
    };
    let engine = data.get("engine").and_then(Value::as_str).map(|engine| {
        let engine = engine.trim();
        if engine.is_empty() { "local" } else { engine }.to_string()
    });
    if let Some(engine) = &engine {
        if engine != "local" && unknown(engine) {
            return Err(bad(&format!("unknown transcription engine: {engine}")));
        }
        settings.push((ENGINE_KEY.to_string(), engine.clone()));
    }
    let strategy = data
        .get("turn_taking")
        .and_then(Value::as_str)
        .map(|strategy| {
            let strategy = strategy.trim();
            if strategy.is_empty() {
                "native"
            } else {
                strategy
            }
            .to_string()
        });
    if let Some(strategy) = &strategy
        && !matches!(strategy.as_str(), "native" | "provider")
    {
        return Err(bad(&format!("unknown turn-taking strategy: {strategy}")));
    }
    match named("retain_audio") {
        Some(Value::Bool(value)) => {
            settings.push(("transcription.retain_audio".into(), value.to_string()))
        }
        None => {}
        Some(_) => return Err(bad("retain_audio must be boolean")),
    }
    match named("long_form_model") {
        None => {}
        Some(Value::String(model)) if model.trim().is_empty() => {}
        Some(Value::String(model)) if unknown(model.trim()) => {
            let model = model.trim();
            return Err(bad(&format!("unknown transcription engine: {model}")));
        }
        Some(Value::String(_)) => {
            return Err(bad(
                "long recordings go to no other engine on this Host yet (long_form_model)",
            ));
        }
        Some(_) => return Err(bad("long_form_model must be a string")),
    }
    match named("long_form_threshold_sec") {
        None => {}
        Some(Value::Number(seconds)) if seconds.is_i64() || seconds.is_u64() => {
            let Some(seconds) = seconds
                .as_i64()
                .filter(|seconds| (1..=3600).contains(seconds))
            else {
                return Err(bad("long_form_threshold_sec must be between 1 and 3600"));
            };
            settings.push((LONG_FORM_THRESHOLD_KEY.to_string(), seconds.to_string()));
        }
        Some(_) => return Err(bad("long_form_threshold_sec must be an integer")),
    }
    if let Some(language) = named("language") {
        let language = languages::code(language).map_err(bad)?;
        settings.push((languages::LANGUAGE_KEY.to_string(), language));
    }
    if let Some(change) = named("agent_engines") {
        let known = |engine: &str| heard::cloud_model(engine).is_some();
        let kept = directory.setting(languages::AGENT_ENGINES_KEY);
        let changed = languages::engines_changed(kept, change, &known).map_err(|why| bad(&why))?;
        settings.push((languages::AGENT_ENGINES_KEY.to_string(), changed));
    }
    if let Some(change) = named("agent_languages") {
        let kept = directory.setting(languages::AGENT_LANGUAGES_KEY);
        let changed = languages::languages_changed(kept, change).map_err(|why| bad(&why))?;
        settings.push((languages::AGENT_LANGUAGES_KEY.to_string(), changed));
    }
    // The engine and how a turn's end is found are looked at together, each as this save
    // names it or else as it is kept: the engine finds the ends of turns only where it is
    // one that can.
    let now = engine.unwrap_or_else(|| app.ears.default_engine(&directory));
    let found_how = strategy.as_deref().unwrap_or(turn_taking(&directory));
    if found_how == "provider" && !finds_turns(&app.ears, &now).0 {
        return Err(bad(
            "provider-owned turn taking needs an engine that detects turns",
        ));
    }
    if let Some(strategy) = strategy {
        settings.push((TURN_TAKING_KEY.to_string(), strategy));
    }
    if named("call_languages").is_some() || named("call_engines").is_some() {
        return Err(bad(
            "how the clips of a call are heard is not built on this Host yet",
        ));
    }
    app.fleet.set(settings).await?;
    let mut told = status(&app);
    told["ok"] = json!(true);
    Ok(Json(told))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/transcribe", post(transcribe))
        .route("/transcription-results/{job}", delete(forget))
        .route("/transcription-capabilities", get(capabilities))
        .route("/vocab/budgets", get(vocabulary_budgets))
        .route(
            "/transcription-providers",
            get(providers).post(set_providers),
        )
}
