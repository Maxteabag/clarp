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

pub(crate) mod call;
mod download;
pub(crate) mod guidance;
pub(crate) mod install;
pub(crate) mod inventory;
mod managed;
mod staging;
mod variants;
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
    pub(crate) installs: install::Desk,
    pub(crate) vocab: crate::vocabulary::Desk,
    pub(crate) workspace: Arc<crate::vocabulary::workspace::Cache>,
    pub(crate) judge: crate::vocabulary::judge::Judge,
    /// The Host's own engine, and the model it runs (`small.en`). `None`: this Host has
    /// none of its own.
    own: Option<(Arc<Whisper>, String)>,
    inventory: inventory::Inventory,
    variants: variants::Variants,
    settings_lock: tokio::sync::Mutex<()>,
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
const LONG_FORM_MODEL_KEY: &str = "transcription.long_form_model";
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
    pub(crate) fn ready(&self) -> bool {
        self.own.as_ref().is_some_and(|(engine, _)| engine.ready())
    }
    fn available(&self) -> bool {
        self.own.is_some() || PROVIDERS.iter().any(|p| self.has(p.id))
    }

    /// The engines this Host was configured with. The Host's own is started at once, so
    /// that its model is loaded when the first clip comes.
    pub async fn new(config: &Config, fleet: &crate::fleet::FleetHandle) -> Result<Self, String> {
        let downloader = download::SpeechDownloader {
            python: PathBuf::from(&config.whisper_python),
            script: config
                .whisper_worker
                .with_file_name("transcription-download.py"),
        };
        Self::with_downloader(config, fleet, Box::new(downloader)).await
    }

    pub(crate) async fn with_downloader(
        config: &Config,
        fleet: &crate::fleet::FleetHandle,
        downloader: Box<dyn clarp_ports::model_download::ModelDownloader>,
    ) -> Result<Self, String> {
        let inventory = inventory::Inventory::open(fleet).await?;
        let installs = install::Desk::new(config.data_dir.join("models"), downloader);
        installs
            .recover(fleet, &inventory)
            .await
            .map_err(|e| format!("reconciling model installs: {e:?}"))?;
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
            let engine = Arc::new(whisper);
            (engine, config.whisper_model.clone())
        });
        let active = own
            .as_ref()
            .map(|(_, model)| format!("{OWN_PROVIDER}:{model}"));
        inventory
            .recover(fleet, &config.data_dir.join("models"), active.as_deref())
            .await?;
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
        Ok(Self {
            own,
            inventory,
            installs,
            variants: Default::default(),
            judge: Default::default(),
            vocab: Default::default(),
            workspace: Default::default(),
            settings_lock: Default::default(),
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
        })
    }

    /// Sweep staging periodically without ever starting a download.
    pub async fn sweep_model_staging(
        &self,
        fleet: &crate::fleet::FleetHandle,
    ) -> Result<(), ApiError> {
        self.installs.sweep_orphans(fleet).await
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

    async fn long_model<'a>(
        &self,
        directory: &'a Directory,
        audio: &axum::body::Bytes,
        kind: &str,
    ) -> &'a str {
        let model = directory.setting(LONG_FORM_MODEL_KEY).unwrap_or("").trim();
        if model.is_empty() {
            return "";
        }
        let seconds = match heard::duration::wav_seconds(audio) {
            Some(s) => Some(s),
            None => match &self.own {
                Some((worker, _)) => {
                    let (worker, audio, kind) = (worker.clone(), audio.clone(), kind.to_string());
                    tokio::task::spawn_blocking(move || {
                        worker.probe(Clip {
                            audio: &audio,
                            content_type: &kind,
                        })
                    })
                    .await
                    .ok()
                    .flatten()
                }
                None => None,
            },
        };
        heard::duration::long_model(model, long_form_threshold(directory), seconds)
    }

    /// The engines a clip is tried on, in order: the first hears it, and each next one only
    /// when the one before failed. `requested` is what the app asked for by name (nothing:
    /// whatever this Host hears with), `own` are the engines of the agent the clip is for,
    /// which come before what an app asks for.
    fn order(
        &self,
        requested: &str,
        own: &[String],
        default_engine: &str,
        long_model: &str,
    ) -> Vec<String> {
        // The cloud engine chosen for this Host stands in for its own; what an app asks
        // for by name still comes first.
        let stands_in = heard::wants_cloud_engine(requested, own, false)
            && (heard::is_cloud(default_engine) || self.inventory.path(default_engine).is_some());
        let cloud = if stands_in { default_engine } else { "" };
        let plan = heard::plan(requested, own, cloud, long_model, false);
        let mut order = vec![plan.model];
        order.extend(plan.fallbacks);
        order
    }

    /// The engine a name stands for on this Host. With the engine comes what is said while
    /// it loads.
    fn engine_of(&self, model: &str) -> Result<Chosen, Refused> {
        if !self.available() {
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
        observed: &mut Option<String>,
        has_fallback: bool,
    ) -> Result<Heard, Told> {
        let variant =
            model.starts_with("faster-whisper:") && self.own_id().as_deref() != Some(model);
        let (chosen, selection) = if variant && let Some((default, _)) = &self.own {
            let (engine, guard) = self
                .variants
                .select(default, model, &self.inventory, has_fallback)
                .await?;
            (
                Chosen {
                    engine,
                    loading: format!("transcription model loading: {model}"),
                    provider: None,
                },
                Some(guard),
            )
        } else {
            (
                self.engine_of(model)
                    .map_err(|(status, why)| refuse(status, why))?,
                None,
            )
        };
        let handed = match chosen.provider {
            Some(provider) => languages::handed(language, provider)
                .map_err(|why| refuse(StatusCode::INTERNAL_SERVER_ERROR, why))?,
            None => {
                let own = if variant {
                    model.strip_prefix("faster-whisper:")
                } else {
                    self.own.as_ref().map(|(_, model)| model.as_str())
                };
                let knows = own.and_then(languages::own_language);
                knows.map(str::to_string)
            }
        };
        let inference = if chosen.provider.is_none() {
            Some(self.variants.inference().await?)
        } else {
            None
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
            let mut captured = None;
            let result = engine.hear_observed(clip, asked, &mut captured);
            let mut selection = selection;
            if let Some(slot) = selection.as_mut()
                && !engine.ready()
            {
                slot.take();
            }
            drop(engine);
            drop(inference);
            drop(selection);
            (result, captured)
        })
        .await;
        let (result, captured) = said.map_err(|_| {
            refuse(
                StatusCode::INTERNAL_SERVER_ERROR,
                "the clip could not be heard",
            )
        })?;
        *observed = captured;
        match result {
            Ok(said) => Ok(said),
            Err(Deaf::Loading) => Err(refuse(StatusCode::SERVICE_UNAVAILABLE, chosen.loading)),
            Err(Deaf::Busy) => Err(refuse(StatusCode::TOO_MANY_REQUESTS, heard::BUSY)),
            Err(Deaf::Failed(why)) => Err(refuse(StatusCode::INTERNAL_SERVER_ERROR, why)),
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

/// The chat a client says a recording is for.
const NAMED_CHAT: &str = "x-transcription-session";

async fn heard_in(app: &App, request: Request) -> Result<Value, Told> {
    let (parts, body) = request.into_parts();
    let headers = &parts.headers;
    let uid = header_text(headers, "x-utterance-id");
    let uid = if uid.is_empty() {
        header_text(headers, "x-transcription-id")
    } else {
        uid
    };
    let uid = clarp_kernel::python::first(clarp_kernel::python::strip(&uid), 128);
    let client_ts = header_text(headers, "x-client-ts")
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|t| t.is_finite())
        .map(|t| t as i64);
    let corrupt = |detail: Value| {
        crate::voice_timeline::record(
            app,
            "corrupt",
            json!({"utterance_id":uid,"client_ts":client_ts,"detail":detail}),
        )
    };
    let length = header_text(headers, "content-length");
    let n = if length.is_empty() {
        Some(0)
    } else {
        length.parse::<i64>().ok()
    };
    let Some(n) = n else {
        corrupt(json!({"reasons":["bad_content_length"]}));
        return Err(refuse(StatusCode::BAD_REQUEST, heard::BAD_SIZE));
    };
    if !(1..=heard::LARGEST_CLIP as i64).contains(&n) {
        corrupt(json!({"reasons":["bad_size"],"bytes":n}));
        return Err(refuse(StatusCode::BAD_REQUEST, heard::BAD_SIZE));
    }
    let length = n as usize;
    let audio = axum::body::to_bytes(body, length).await.map_err(|_| {
        corrupt(json!({"reasons":["upload_read_error"],"expected":length}));
        refuse(StatusCode::REQUEST_TIMEOUT, "incomplete audio upload")
    })?;
    if audio.len() != length {
        corrupt(json!({"reasons":["incomplete_upload"],"expected":length,"received":audio.len()}));
        return Err(refuse(
            StatusCode::REQUEST_TIMEOUT,
            "incomplete audio upload",
        ));
    }
    let requested = header_text(headers, "x-transcription-model");
    let content_type = match header_text(headers, "content-type") {
        none if none.is_empty() => "audio/webm".to_string(),
        given => given,
    };
    let hands_free = heard::truthy(Some(&header_text(headers, "x-hands-free")));
    let job = heard::job_id(Some(&header_text(headers, "x-transcription-id")))
        .map_err(|why| refuse(StatusCode::BAD_REQUEST, why))?;
    let scope = if headers.contains_key("x-clarp-call-id") {
        use crate::edge::Principal;
        let owner = match parts.extensions.get::<Principal>() {
            Some(Principal::Device { device_id, .. }) => device_id.as_str(),
            Some(Principal::Administrator) if app.config.auth_token.is_empty() => "",
            Some(Principal::Administrator) => clarp_kernel::calls::ADMINISTRATOR,
            Some(Principal::Tools) => {
                return Err(refuse(
                    StatusCode::FORBIDDEN,
                    "an agent's tools may ask for tools only",
                ));
            }
            None => return Err(refuse(StatusCode::UNAUTHORIZED, "authentication required")),
        };
        let id = header_text(headers, "x-clarp-call-id");
        let session = header_text(headers, "x-transcription-session");
        let scope = app
            .calls
            .ask(app, |calls, at, _, _| {
                let record = calls
                    .record(clarp_kernel::python::strip(&id))
                    .map(|(owner, call)| (owner, call.told(at.now_ms)));
                clarp_kernel::call_transcription::scope(
                    owner,
                    at.host_id,
                    &id,
                    &session,
                    record.as_ref().map(|(o, c)| (*o, c)),
                )
            })
            .await
            .map_err(|e| refuse(StatusCode::SERVICE_UNAVAILABLE, e.0))?
            .map_err(|e| {
                (
                    StatusCode::from_u16(e.status).unwrap_or(StatusCode::CONFLICT),
                    e.body,
                )
            })?;
        Some(scope)
    } else {
        None
    };

    let fingerprint = heard::scoped_fingerprint(
        &audio,
        &content_type,
        &requested,
        hands_free,
        scope.as_ref(),
    );

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
            Ok(Ok(Some(answer))) => {
                crate::voice_timeline::record(
                    app,
                    "retry",
                    json!({"utterance_id":uid,"client_ts":client_ts,
                    "trace_id":answer["trace_id"],"text":answer["text"],"detail":{"bytes":length,"content_type":content_type}}),
                );
                return Ok(heard::again(answer));
            }
            Ok(Err(why)) => return Err(refuse(StatusCode::CONFLICT, why)),
            Ok(Ok(None)) | Err(_) => {}
        }
    }

    let trace = ids::hex(8);
    let began = std::time::Instant::now();
    let directory = app.fleet.directory();
    // The chat a recording is for: the one the client names, else the one it last opened,
    // else the focus every client shares (`clarp_kernel::heard::transcription_session`). A
    // call's clip is its call's; what a client opened is read only for a clip that names no
    // chat the Host has.
    let known = |name: &str| directory.named(name).is_some();
    let target = if scope.is_some() {
        String::new()
    } else {
        let named = header_text(headers, NAMED_CHAT);
        match heard::transcription_session(&named, "", known) {
            none if none.is_empty() => {
                let who = parts
                    .extensions
                    .get::<crate::edge::Principal>()
                    .map(|principal| crate::edge::principal_name(app, principal))
                    .unwrap_or_default();
                let last = crate::edge::last_opened(app, &who).await;
                heard::transcription_session("", &last, known)
            }
            named => named,
        }
    };
    let recorded_for = match target.as_str() {
        "" => directory
            .focus
            .as_deref()
            .and_then(|id| directory.named(id)),
        target => directory.named(target),
    };
    let session = scope
        .as_ref()
        .map(|s| s.session.as_str())
        .or_else(|| recorded_for.map(|m| m.record.session.as_str()))
        .unwrap_or("");
    let failed = |err: Told| {
        let details = match err.0 {
            StatusCode::TOO_MANY_REQUESTS => Some(json!({"message":"whisper busy"})),
            StatusCode::INTERNAL_SERVER_ERROR => Some(
                json!({"message":clarp_kernel::python::first(err.1["error"].as_str().unwrap_or(""),300),"stage":"stt"}),
            ),
            _ => None,
        };
        if let Some(detail) = details {
            crate::voice_timeline::record(
                app,
                "error",
                json!({"utterance_id":uid,"client_ts":client_ts,"detail":detail}),
            );
        }
        err
    };
    let (said, report, run_id) = if scope.is_some() {
        let (said, report, id) = call::hear(
            app,
            session,
            &requested,
            hands_free,
            &trace,
            (&audio, &content_type),
        )
        .await
        .map_err(failed)?;
        (said, Some(report), id)
    } else {
        let (own, language) = spoken_to(&directory, recorded_for, &ears.language);
        let long = if own.is_empty() {
            ears.long_model(&directory, &audio, &content_type).await
        } else {
            ""
        };
        let order = ears.order(&requested, &own, &ears.default_engine(&directory), long);
        let selected = order.first().map_or(requested.as_str(), String::as_str);
        let (audit, id) =
            crate::vocabulary::compile(app, session, selected, hands_free, Some(&trace))
                .await
                .unwrap_or_default();
        let said = hear_chat(
            app,
            &ears,
            &order,
            &language,
            audit["payload"].as_str().unwrap_or(""),
            &audio,
            &content_type,
        )
        .await
        .map_err(failed)?;
        (said, None, id)
    };
    let retained_model = report
        .as_ref()
        .and_then(|r| r["effective_engine"].as_str())
        .unwrap_or(&requested)
        .to_string();
    // What the engine was sure was speech, unless it is something engines invent on silence.
    let text = crate::vocabulary::judge::kept(app, heard::joined(&said.segments)).await;
    let mut answer = heard::answer(&text, &trace, hands_free);
    // A yes or a no to an agent whose hand is up is a word to the herald, and no message:
    // the app is told, and sends nothing.
    if !text.is_empty() && app.mouth.heard(&text).await {
        answer["herald_consumed"] = json!(true);
    }
    let duration = (said.seconds != 0.0).then(|| (said.seconds * 10000.0).round_ties_even() / 10.0);
    crate::voice_timeline::record(
        app,
        "transcript",
        json!({"session":session,"trace_id":trace,"utterance_id":uid,
        "client_ts":client_ts,"duration_ms":duration,"text":text,"detail":{"bytes":length,"content_type":content_type,
        "hands_free":hands_free,"ends_terminal":answer["ends_terminal"],"herald_consumed":answer["herald_consumed"],
        "model":if requested.is_empty(){"server-default"}else{requested.as_str()}}}),
    );
    crate::voice_timeline::metrics(
        app,
        audio.clone(),
        &content_type,
        session,
        &trace,
        uid,
        text.trim().is_empty(),
    );
    if run_id > 0 {
        let finished = app
            .ears
            .vocab
            .finish(
                &app.fleet,
                run_id,
                text.clone(),
                began.elapsed().as_millis() as i64,
                report.as_ref(),
            )
            .await;
        if let Err((_, why)) = finished {
            app.fleet.unkept("vocabulary run", &why);
        }
        answer["vocab_run_id"] = json!(run_id);
    }
    if crate::vocabulary::audio::enabled(&app.fleet.directory()) {
        let root = app.config.data_dir.clone();
        let (trace, model, session) = (trace.clone(), retained_model, session.to_string());
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
    if let Some(report) = report {
        answer["stt"] = report;
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

async fn hear_chat(
    app: &App,
    ears: &Arc<Ears>,
    order: &[String],
    language: &str,
    listen_for: &str,
    audio: &axum::body::Bytes,
    content_type: &str,
) -> Result<Heard, Told> {
    let mut said = None;
    for (place, model) in order.iter().enumerate() {
        let started = app
            .capture
            .settings()
            .allows("transcript")
            .then(std::time::Instant::now);
        let heard = ears
            .heard_by(
                model,
                language,
                listen_for,
                (audio, content_type),
                &mut None,
                place + 1 < order.len(),
            )
            .await;
        let at = app.clock.now_ms();
        app.capture
            .transcription(at, heard.is_ok(), audio.len(), started);
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
    Ok(said)
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
fn spoken_to(
    directory: &Directory,
    looked_at: Option<&Arc<crate::fleet::Member>>,
    started_with: &str,
) -> (Vec<String>, String) {
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
async fn capabilities(State(app): State<App>) -> Result<Json<Value>, ApiError> {
    let ears = &app.ears;
    let mut models = ears.inventory.models();
    let registered: Vec<_> = models
        .iter()
        .filter_map(|r| r["id"].as_str().map(str::to_owned))
        .collect();
    if let (Some(id), Some((_, model))) = (ears.own_id(), &ears.own)
        && !registered.contains(&id)
    {
        models.insert(
            0,
            json!({
                "id": id, "name": heard::label(model), "provider": OWN_PROVIDER, "model": model,
                "weight": heard::weight(model),
            }),
        );
    }
    let usable = |row: &Value| row["available"] == true;
    models.extend(cloud_models(ears).into_iter().filter(usable));
    Ok(Json(json!({
        "available": !models.is_empty(),
        "default_model": ears.own_id().unwrap_or_default(),
        "engine": ears.default_engine(&app.fleet.directory()),
        "turn_taking": turn_taking(&app.fleet.directory()),
        "models": models,
        "catalog": install::catalog(&app.fleet, heard::inventory::catalog(&registered)).await?,
        "adapters": [],
    })))
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
    kept.and_then(|s| clarp_kernel::python::int(s.trim()))
        .unwrap_or(30)
        .clamp(1, 3600) as i64
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
    let call = call::policy(app, &directory);
    let (finds, usable) = finds_turns(ears, &engine);
    json!({
        "engine": engine,
        "language": languages::selected(directory.setting(languages::LANGUAGE_KEY), &ears.language),
        "agent_languages": told_map(languages::languages(directory.setting(languages::AGENT_LANGUAGES_KEY))),
        "agent_engines": told_map(languages::stored(directory.setting(languages::AGENT_ENGINES_KEY))),
        "call_languages": call.language,
        "call_engines": call.engines.join(","),
        "call_policy_error": call.error,
        "turn_taking": turn_taking(&directory),
        "retain_audio": crate::vocabulary::audio::enabled(&directory),
        "long_form_model":directory.setting(LONG_FORM_MODEL_KEY).unwrap_or("").trim(),
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
    let _save = app.ears.settings_lock.lock().await;
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
        Some(Value::String(model)) => {
            let model = model.trim();
            if !model.is_empty() && unknown(model) {
                return Err(bad(&format!("unknown transcription engine: {model}")));
            }
            settings.push((LONG_FORM_MODEL_KEY.to_string(), model.to_string()));
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
    let call = call::policy(&app, &directory);
    let (lang, models) = call
        .change(named("call_languages"), named("call_engines"), &now)
        .map_err(|why| bad(&why))?;
    for (key, value) in [
        (clarp_kernel::call_transcription::LANGUAGES, lang),
        (clarp_kernel::call_transcription::ENGINES, models),
    ] {
        if let Some(v) = value {
            settings.push((key.into(), v));
        }
    }
    app.fleet.set(settings).await?;
    let mut told = status(&app);
    told["ok"] = json!(true);
    Ok(Json(told))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/transcribe", post(transcribe))
        .route(
            "/transcription-guidance",
            get(guidance::get_guidance).post(guidance::set_guidance),
        )
        .route("/transcription-results/{job}", delete(forget))
        .route("/transcription-capabilities", get(capabilities))
        .route(
            "/transcription-models/install",
            post(install::install_model),
        )
        .route(
            "/transcription-models/remove",
            post(inventory::remove_model),
        )
        .route("/vocab/budgets", get(vocabulary_budgets))
        .route(
            "/transcription-providers",
            get(providers).post(set_providers),
        )
}
