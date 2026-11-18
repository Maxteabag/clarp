//! Speech out at the Host's edge: what an agent says aloud becomes clips, and the apps fetch
//! and play them.
//!
//! A reply marks what is for the ear, and each marked piece waits in the agent's log as a
//! line ([`clarp_kernel::spoken`]). Here is the one that speaks them: it takes an agent's
//! next line, has an engine say it into a file under the Host's data, and tells the apps
//! where the clip is before its first byte is there, so that a listener hears the beginning
//! of a long line while its end is still being made. One line of an agent at a time, in the
//! order they were said; a few agents at once.
//!
//! Who speaks, and in which voice, is the owner's choice, read from his file each time
//! ([`crate::owner`]) by the rules of [`clarp_kernel::said`]. The engines are behind one
//! port ([`clarp_speech::speak`]): when the first cannot begin, the next does, and what was
//! begun in one voice is never begun again in another. A line nothing on this Host can say
//! is over before a clip is announced: no app waits for sound that never comes.
//!
//! What lasts is in the agent's log: the line, the clip it became, whether the clip is
//! whole, what an app said of playing it. What is held here is only how far a clip that is
//! being made has come. A Host that is started again holds nothing, and finds in the logs
//! what it still owes the ear.
//!
//! The routes answer as the Python Host's do (`clip_stream.py`, `audio_growing.py`,
//! `routes/voice_timeline.py`, `clip_records.py`, `message_audio.py`): the same statuses,
//! words and headers.

use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path as FilePath, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::herald::{self, Attempt, Herald, Moment, Play, Taken};
use clarp_kernel::media::{self, Part};
use clarp_kernel::python;
use clarp_kernel::said::{self, Who};
use clarp_kernel::spoken::{self, Line, LineState, Meant};
use clarp_ports::clock::Clock;
use clarp_speech::cartesia::Cartesia;
use clarp_speech::gemini::{Gemini, Surface};
use clarp_speech::speak::{Mute, Speaker, said_by_the_first_that_can};
use futures_util::stream;
use serde_json::{Map, Value, json};
use tokio::sync::{Semaphore, mpsc, watch};
use tokio::time::Instant;
use toml::Table;

use crate::agent::{Spoke, View};
use crate::config::Config;
use crate::edge::{ApiError, App};
use crate::fleet::{AgentRecord, Directory, FleetHandle, Member};
use crate::hub::Hub;
use crate::media::header_text;
use crate::owner::{self, OwnerFile};
use crate::wire;

/// How long a player waits for more of a clip that is being made, and for a clip to be
/// whole (the Python Host's `CHUNK_WAIT_SEC` and `MAX_GROW_WAIT_SEC`).
const WAITS: Duration = Duration::from_secs(30);

/// How much of a clip is read and sent at a time.
const CHUNK: usize = 64 * 1024;

/// How many engines speak at once on this Host. An agent has one line spoken at a time;
/// this is how many agents are spoken for together.
const AT_ONCE: usize = 4;

/// How often clips nobody will play again are cleared away.
const SWEPT_EVERY: Duration = Duration::from_secs(60 * 60);

/// How many bytes of clips are kept to be played again (`REPLAY_RETAIN_MAX_BYTES`).
const KEPT_BYTES: u64 = 256 * 1024 * 1024;

/// What is written of a line on a Host whose owner turned speech off.
const SPEECH_OFF: &str = "speech out is turned off on this Host";

/// The engines that speak which are built into this Host.
pub(crate) const BUILT: [&str; 5] = [
    said::CARTESIA,
    said::GEMINI,
    "deepgram",
    said::ELEVENLABS,
    "google",
];

/// The way to the one that speaks. Telling it never waits.
#[derive(Clone)]
pub struct Speaks {
    tx: mpsc::UnboundedSender<String>,
}

impl Speaks {
    /// An agent has something to say, or may have.
    pub fn wake(&self, agent_id: &str) {
        let _ = self.tx.send(agent_id.to_string());
    }
}

/// Who was told to speak before the one that speaks runs: kept, and spoken once it does.
pub struct Waiting {
    rx: mpsc::UnboundedReceiver<String>,
}

/// The way to the one that speaks, and what waits for it.
pub fn mouth() -> (Speaks, Waiting) {
    let (tx, rx) = mpsc::unbounded_channel();
    (Speaks { tx }, Waiting { rx })
}

/// Everything the one that speaks is given.
pub struct Works {
    pub config: Arc<Config>,
    pub fleet: FleetHandle,
    pub hub: Arc<Hub>,
    pub clock: Arc<dyn Clock + Send + Sync>,
}

/// Whether a clip that is being made is still being made.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Made {
    Making,
    Whole,
    Failed,
}

/// How far a clip that is being made has come.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Progress {
    /// How many of its bytes are in its file.
    bytes: u64,
    made: Made,
}

/// A clip that is being made: its file grows, and whoever plays it is told when it has.
struct Growing {
    name: String,
    path: PathBuf,
    progress: watch::Sender<Progress>,
}

impl Growing {
    fn now(&self) -> Progress {
        *self.progress.borrow()
    }
}

/// A clip the herald holds for its agent's turn: what it takes to announce it then.
#[derive(Debug, Clone)]
struct HeldClip {
    agent_id: String,
    clip: u64,
    name: String,
    trace_id: Option<String>,
}

/// What the herald's own clips are called, before what any clip is called (the Python
/// Host's name for them).
const HERALD_FILE: &str = "herald-internal-";

/// The one that speaks, and where its clips are.
pub struct Mouth {
    /// Where the owner's choices and keys are read.
    owner: OwnerFile,
    /// Where clips are kept: `<data>/audio`.
    audio: PathBuf,
    /// The language the agents of this Host speak.
    language: String,
    /// Stand-ins on this machine in place of the engines' own addresses. Empty: the real.
    cartesia_at: String,
    deepgram_at: String,
    eleven_at: String,
    google_at: String,
    google_token_at: String,
    gemini_at: String,
    /// The clips that are being made now, by their number.
    growing: Mutex<HashMap<u64, Arc<Growing>>>,
    /// The number of the newest clip. A clip's number is the moment it was begun, and no
    /// two clips of this Host have the same.
    newest: Mutex<u64>,
    at_once: Semaphore,
    fleet: FleetHandle,
    hub: Arc<Hub>,
    clock: Arc<dyn Clock + Send + Sync>,
    /// Cartesia's own list of voices as it was last fetched, and when.
    cartesia_voices: Mutex<Option<(std::time::Instant, Arc<Vec<Value>>)>>,
    /// Where the previews of voices are kept: `<data>/voice-previews`.
    previews: PathBuf,
    preview_locks: [tokio::sync::Mutex<()>; 16],
    /// The herald: what is held for agents whose user is with another, and whose hand is
    /// up ([`clarp_kernel::herald`]). One thing at a time happens to it: whoever asks it
    /// keeps it until what it said is told to the apps and written, so that a clip is
    /// never let go before its line knows it.
    herald: tokio::sync::Mutex<Herald<HeldClip>>,
}

/// How long Cartesia's list of voices is good for.
const LIST_KEPT: Duration = Duration::from_secs(15 * 60);

impl Mouth {
    /// The owner's file as it is now. Nothing named, or nothing readable: an empty one.
    pub(crate) fn owners(&self) -> Table {
        let mut file = self.owner.read().unwrap_or_default();
        let directory = self.fleet.directory();
        for (section, key, setting) in [
            ("tts", "provider", "speech.provider"),
            ("tts", "fallback", "speech.fallback"),
            ("local_tts", "voice", "speech.local_voice"),
        ] {
            if let Some(value) = directory.setting(setting) {
                let table = file
                    .entry(section)
                    .or_insert_with(|| toml::Value::Table(Table::new()));
                if let Some(table) = table.as_table_mut() {
                    table.insert(key.into(), toml::Value::String(value.into()));
                }
            }
        }
        file
    }

    pub async fn google_refusal(&self) -> Option<&'static str> {
        let owner = self.owner.clone();
        tokio::task::spawn_blocking(move || owner.google_refusal(&owner.read().unwrap_or_default()))
            .await
            .unwrap_or(Some("google_id_unverifiable"))
    }

    /// Whether an engine that speaks has its key.
    pub(crate) fn has_key(&self, provider: &str, owners: &Table) -> bool {
        if provider == "google" {
            owner::google_voice_error(owners).is_none()
        } else {
            self.owner.key(key_of(provider, owners)).is_some()
        }
    }

    /// The ids of Cartesia's voices as this Host last listed them, however long ago, without
    /// asking anybody: what a voice for a new persona is picked from first.
    pub(crate) fn cartesia_known(&self) -> Vec<String> {
        let kept = self
            .cartesia_voices
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let listed = kept.iter().flat_map(|(_, voices)| voices.iter());
        listed
            .filter_map(|voice| voice["id"].as_str().map(str::to_string))
            .collect()
    }

    /// Cartesia's own list of voices: the one fetched within the last quarter of an hour,
    /// or a new one. Without a key, or when Cartesia does not answer, there is none.
    pub(crate) async fn cartesia_list(self: &Arc<Self>) -> Arc<Vec<Value>> {
        self.cartesia_rows(false, false).await.unwrap_or_default()
    }

    pub(crate) async fn cartesia_rows(
        &self,
        force: bool,
        cached_preview: bool,
    ) -> Result<Arc<Vec<Value>>, String> {
        let kept = |mouth: &Self| {
            let kept = mouth
                .cartesia_voices
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            kept.clone()
        };
        if let Some((at, voices)) = kept(self)
            && !force
            && (cached_preview || at.elapsed() < LIST_KEPT)
        {
            return Ok(voices);
        }
        let Some(key) = self.owner.key("cartesia") else {
            return Ok(Arc::default());
        };
        let list_at = match self.cartesia_at.as_str() {
            "" => clarp_speech::cartesia::VOICES.to_string(),
            // A stand-in for the engine lists its voices beside where it speaks.
            stand_in => stand_in.replace("/tts/bytes", "/voices"),
        };
        let fetched =
            tokio::task::spawn_blocking(move || clarp_speech::cartesia::voices(&key, &list_at));
        match fetched.await {
            Ok(Ok(voices)) => {
                let voices = Arc::new(voices);
                let now = (std::time::Instant::now(), voices.clone());
                *self
                    .cartesia_voices
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner) = Some(now);
                Ok(voices)
            }
            Ok(Err(why)) => Err(why),
            Err(_) => Err("Cartesia catalogue thread ended".into()),
        }
    }

    pub(crate) async fn legacy_preview(
        &self,
        session: &str,
        voice: &str,
        text: &str,
    ) -> Result<String, String> {
        let owners = self.owners();
        let provider = owner::voices(&owners).provider;
        if provider == "none" {
            return Err("server audio is disabled: no voice provider".into());
        }
        let preview = self.preview(&provider, voice, text, Audition::Text(session)).await?;
        let Preview::Temporary(bytes) = preview else {
            return Err("text preview unexpectedly cached".into());
        };
        let (_, name, path, mut clip) = self.new_clip(session).map_err(|e| e.to_string())?;
        let write = tokio::task::spawn_blocking(move || {
            clip.write_all(&bytes).inspect_err(|_| {
                let _ = std::fs::remove_file(&path);
            })
        })
        .await
        .map_err(|_| "preview writer ended".to_string())?;
        write.map_err(|e| e.to_string())?;
        self.hub.publish(json!({"type":"audio","url":format!("/audio/{name}"),"name":name,"session":session,"preview":true}),self.now_ms());
        Ok(name)
    }
    /// Only the requested voice can populate its cache. When another voice
    /// answers we return the bytes to the requesting client for this play.
    pub(crate) async fn preview(
        &self,
        provider: &str,
        voice: &str,
        text: &str,
        audition: Audition<'_>,
    ) -> Result<Preview, String> {
        let (name, session)=match audition { Audition::Catalogue(key)=>(Some(key),""), Audition::Text(session)=>(None,session) };

        if name.is_some()
            && self
                .previews
                .symlink_metadata()
                .is_ok_and(|m| m.is_symlink())
        {
            return Err("preview cache unavailable".into());
        }
        let file = name.map(|n| self.previews.join(format!("{n}.mp3")));
        let _lock = if let Some(name) = name {
            let bucket =
                name.bytes().next().unwrap_or_default() as usize % self.preview_locks.len();
            Some(self.preview_locks[bucket].lock().await)
        } else {
            None
        };
        if let Some(file) = &file
            && file.symlink_metadata().is_ok_and(|m| m.is_file())
        {
            return Ok(Preview::Cached(file.clone()));
        }
        let owners = self.owners();
        let primary = self
            .engine(
                &owners,
                provider,
                if name.is_none() {
                    SpeechUse::TextPreview
                } else {
                    SpeechUse::VoicePreview
                },
            )
            .ok_or_else(|| format!("no engine named {provider} is built"))?;
        let voices = owner::voices(&owners);
        let route = said::Route {
            provider: provider.into(),
            voice: (name.is_some() || provider=="google").then(||voice.to_string()),
            fallback: voices.fallback.clone(),
        };
        let directory=self.fleet.directory();
        let persona=directory.named(session).map_or("",|m|m.record.persona.as_str());
        let clip=voices.for_clip(Some(voice), Who { name:persona,persona,session }, &route);
        let resolve=|engine:&str| resolved_voice(&owners,&voices,&clip,Some(voice),&route,engine);
        let fallback = route.fallback.as_str();
        let fallback = if fallback != provider
            && fallback != "none"
            && !fallback.is_empty()
            && self.has_key(fallback, &owners)
        {
            let chosen=resolve(fallback);
            self.engine(
                &owners,
                fallback,
                if name.is_none() && matches!(provider, "cartesia" | "elevenlabs") {
                    SpeechUse::TextPreview
                } else {
                    SpeechUse::Turn
                },
            )
            .zip(chosen)
        } else {
            None
        };
        let voice=resolve(provider).unwrap_or_default();
        let direct=name.is_some();
        let text = text.to_string();
        let dir = self.previews.clone();
        let _slot = self.at_once.acquire().await;
        tokio::task::spawn_blocking(move || {
            let mut bytes = Vec::new();
            let line=clarp_speech::speak::Line{text:&text,voice:&voice};
            let mut collect=|part:&[u8]|bytes.extend_from_slice(part);
            let result=if direct {primary.speak(line,&mut collect)} else {primary.speak_turn(line,&mut collect)};
            if let Err(error)=result {
                let why = match error {
                    Mute::Unavailable(why) | Mute::Failed { why, .. } => why,
                };
                if !matches!(
                    why.as_str(),
                    "voice_unavailable" | "google_id_collision" | "google_id_unverifiable"
                ) && let Some((engine, voice)) = fallback
                {
                    bytes.clear();
                    if engine
                        .speak_turn(
                            clarp_speech::speak::Line {
                                text: &text,
                                voice: &voice,
                            },
                            &mut |part| bytes.extend_from_slice(part),
                        )
                        .is_ok()
                    {
                        return Ok(Preview::Temporary(Bytes::from(bytes)));
                    }
                }
                return Err(why);
            }
            let Some(file) = file else {
                return Ok(Preview::Temporary(Bytes::from(bytes)));
            };
            std::fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&dir)
                .map_err(|e| e.to_string())?;
            let part = file.with_extension(format!("{}.part", crate::ids::hex(4)));
            let mut made = std::fs::OpenOptions::new()
                .mode(0o600)
                .create_new(true)
                .write(true)
                .open(&part)
                .map_err(|e| e.to_string())?;
            if let Err(e) = made
                .write_all(&bytes)
                .and_then(|()| std::fs::rename(&part, &file))
            {
                let _ = std::fs::remove_file(part);
                return Err(e.to_string());
            }
            Ok(Preview::Cached(file))
        })
        .await
        .map_err(|_| "speech engine thread ended".to_string())?
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum SpeechUse {
    Turn,
    VoicePreview,
    TextPreview,
}

pub(crate) enum Audition<'a>{ Catalogue(&'a str), Text(&'a str) }

pub(crate) enum Preview {
    Cached(PathBuf),
    Temporary(Bytes),
}

impl Preview {
    pub fn send(self, range: &str) -> Response {
        match self {
            Self::Cached(path) => send_file(path, range),
            Self::Temporary(bytes) => {
                let total = bytes.len() as u64;
                let part = media::byte_range(range, total);
                let mut headers = vec![
                    (header::CONTENT_TYPE, "audio/mpeg".into()),
                    (header::ACCEPT_RANGES, "bytes".into()),
                ];
                let (status, body) = match part {
                    Part::Missing => return no_such_part(&total.to_string(), true),
                    Part::Whole => (StatusCode::OK, bytes),
                    Part::Bytes { first, last } => {
                        headers.push((
                            header::CONTENT_RANGE,
                            format!("bytes {first}-{last}/{total}"),
                        ));
                        (
                            StatusCode::PARTIAL_CONTENT,
                            bytes.slice(first as usize..=last as usize),
                        )
                    }
                };
                headers.push((header::CONTENT_LENGTH, body.len().to_string()));
                answer(status, headers, Body::from(body))
            }
        }
    }
}

impl Waiting {
    /// Starts the one that speaks, on the Host's runtime, and answers the way to its clips.
    pub fn run(mut self, works: Works) -> Arc<Mouth> {
        let audio = works.config.data_dir.join("audio");
        let mouth = Arc::new(Mouth {
            owner: OwnerFile::new(works.config.speech_keys.clone()),
            newest: Mutex::new(newest_clip(&audio)),
            audio,
            language: works.config.speech_language.clone(),
            cartesia_at: works.config.cartesia_endpoint.clone(),
            deepgram_at: works.config.deepgram_voice_endpoint.clone(),
            eleven_at: works.config.eleven_voice_endpoint.clone(),
            google_at: works.config.google_voice_endpoint.clone(),
            google_token_at: works.config.google_token_endpoint.clone(),
            gemini_at: works.config.gemini_endpoint.clone(),
            growing: Mutex::default(),
            at_once: Semaphore::new(AT_ONCE),
            fleet: works.fleet,
            hub: works.hub,
            clock: works.clock,
            cartesia_voices: Mutex::default(),
            preview_locks: Default::default(),
            previews: works.config.data_dir.join("voice-previews"),
            herald: tokio::sync::Mutex::default(),
        });
        let speaking = mouth.clone();
        tokio::spawn(async move {
            while let Some(agent_id) = self.rx.recv().await {
                tokio::spawn(speaking.clone().speak_for(agent_id));
            }
        });
        let sweeping = mouth.clone();
        tokio::spawn(async move {
            loop {
                let (audio, now) = (sweeping.audio.clone(), sweeping.clock.now_ms());
                let _ = tokio::task::spawn_blocking(move || sweep(&audio, now)).await;
                tokio::time::sleep(SWEPT_EVERY).await;
            }
        });
        mouth
    }
}

/// The largest number a clip in this directory has: no new clip gets one below it.
fn newest_clip(audio: &FilePath) -> u64 {
    let Ok(entries) = std::fs::read_dir(audio) else {
        return 0;
    };
    let made = entries.flatten().filter_map(|entry| {
        let name = entry.file_name().into_string().ok()?;
        u64::try_from(said::clip_named(&name)?.0).ok()
    });
    made.max().unwrap_or(0)
}

/// Clears away the clips nobody will play again: the ones older than a line is kept in
/// mind, and the oldest beyond what is kept in bytes.
fn sweep(audio: &FilePath, now_ms: i64) {
    let Ok(entries) = std::fs::read_dir(audio) else {
        return;
    };
    let mut clips: Vec<(i64, u64, PathBuf)> = entries
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            // A herald's clip is a clip, under its own beginning of a name.
            let (made, _) = said::clip_named(name.strip_prefix(HERALD_FILE).unwrap_or(&name))?;
            Some((made, entry.metadata().ok()?.len(), entry.path()))
        })
        .collect();
    clips.sort_by_key(|clip| std::cmp::Reverse(clip.0));
    let mut kept = 0_u64;
    for (made, bytes, path) in clips {
        kept = kept.saturating_add(bytes);
        if now_ms - made > spoken::KEPT_FOR_MS || kept > KEPT_BYTES {
            let _ = std::fs::remove_file(path);
        }
    }
}

/// Who says a line, as the owner's file and the agent decide it.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Chosen {
    /// The owner turned speech off: nothing is said, and nobody is told.
    Silent,
    /// Nothing on this Host can say it. `told`: the apps are told so; an agent that simply
    /// has no voice stays silent without a word, as on the Python Host.
    Nobody { why: String, told: bool },
    /// These engines, tried in this order, each with the voice it speaks in.
    By(Vec<(String, String)>),
}

fn resolved_voice(file:&Table,voices:&said::Voices,clip:&said::ClipVoices,stored:Option<&str>,route:&said::Route,provider:&str)->Option<String>{
    let vertex = owner::gemini(file).vertex;

        let voice = match provider {
            said::CARTESIA => clip.cartesia.clone(),
            said::GEMINI => {
                let voice = clip.gemini.as_deref();
                voice.map(|voice| voices.gemini_voice_at(voice, vertex))
            }
            said::ELEVENLABS => clip.elevenlabs.clone(),
            "google" => said::voice_for(stored, route, "google")
                .or_else(|| {
                    stored
                        .map(str::trim)
                        .filter(|v| clarp_kernel::voices::google_has(v))
                        .map(str::to_string)
                })
                .or_else(|| Some("nb-NO-Chirp3-HD-Kore".into())),
            "deepgram" => said::voice_for(stored, route, "deepgram").or_else(|| {
                Some(
                    owner::text_at(file, &["deepgram", "model"])
                        .unwrap_or_else(|| "flux-haley-en".into()),
                )
            }),
            _ => None,
        };
        voice.filter(|voice| !voice.is_empty())
}

/// Which engines say an agent's line, and in which voices: the engine the owner chose for
/// it and the one he chose to be tried after, each when it is built into this Host, has a
/// voice for the agent and has its key.
fn chosen(file: &Table, record: &AgentRecord, has_key: &dyn Fn(&str) -> bool) -> Chosen {
    let voices = owner::voices(file);
    let who = Who {
        name: &record.persona,
        persona: &record.persona,
        session: &record.session,
    };
    let route = voices.route(who);
    if route.provider == "none" {
        return Chosen::Silent;
    }
    let stored = Some(record.voice_id.as_str()).filter(|voice| !voice.is_empty());
    let clip = voices.for_clip(stored, who, &route);
    let mut by: Vec<(String, String)> = Vec::new();
    // Why the engine the owner chose cannot say it, when it cannot.
    let mut first: Option<(String, bool)> = None;
    for provider in [route.provider.as_str(), route.fallback.as_str()] {
        if matches!(provider, "" | "none") || by.iter().any(|(known, _)| known == provider) {
            continue;
        }
        let cannot = match resolved_voice(file,&voices,&clip,stored,&route,provider) {
            None => {
                let persona = python::text(&record.persona);
                Some((
                    format!("no {provider} voice configured for {persona}"),
                    false,
                ))
            }
            Some(_) if !BUILT.contains(&provider) => {
                let why = format!("the engine {provider} is not built into this Host");
                Some((why, true))
            }
            Some(_) if !has_key(provider) => {
                Some((format!("the {provider} api_key is not configured"), true))
            }
            Some(voice) => {
                by.push((provider.to_string(), voice));
                None
            }
        };
        first = first.or(cannot);
    }
    match (by.is_empty(), first) {
        (false, _) => Chosen::By(by),
        (true, Some((why, told))) => Chosen::Nobody { why, told },
        (true, None) => Chosen::Silent,
    }
}

/// The name of the key an engine needs, as the owner's file is asked for it.
fn key_of(provider: &str, file: &Table) -> &'static str {
    match provider {
        said::CARTESIA => "cartesia",
        "deepgram" => "deepgram",
        said::ELEVENLABS => "elevenlabs",
        said::GEMINI if owner::gemini(file).vertex => "vertex",
        said::GEMINI => "gemini",
        _ => "",
    }
}

impl Mouth {
    fn now_ms(&self) -> i64 {
        self.clock.now_ms()
    }

    fn held(&self) -> std::sync::MutexGuard<'_, HashMap<u64, Arc<Growing>>> {
        self.growing.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The clip with this number, when it is being made here or was a moment ago.
    fn growing(&self, clip: u64) -> Option<Arc<Growing>> {
        self.held().get(&clip).cloned()
    }

    /// The clip whose file has this name, while it is being made.
    fn growing_named(&self, name: &str) -> Option<Arc<Growing>> {
        let held = self.held();
        let found = held.values().find(|growing| growing.name == name);
        found
            .filter(|growing| growing.now().made == Made::Making)
            .cloned()
    }

    /// An engine of this Host, with its key read where the owner keeps it at the moment it
    /// is needed.
    fn engine(&self, file: &Table, provider: &str, usage: SpeechUse) -> Option<Box<dyn Speaker>> {
        let keys = self.owner.clone();
        let key = key_of(provider, file);
        let read = Box::new(move || keys.key(key));
        match provider {
            "google" => {
                // Both legs must stay on the same side of the stand-in boundary.
                if self.google_at.is_empty() != self.google_token_at.is_empty() {
                    return None;
                }
                let owner = self.owner.clone();
                Some(Box::new(clarp_speech::google_voice::GoogleVoice {
                    endpoint: self.google_at.clone(),
                    token_url: self.google_token_at.clone(),
                    allowed: clarp_kernel::voices::google_has,
                    account: Box::new(move || {
                        let file = owner.read().unwrap_or_default();
                        if let Some(code) = owner.google_refusal(&file) {
                            return Err(code.into());
                        }
                        owner::google_voice_account(&file).map_err(str::to_string)
                    }),
                }))
            }
            said::ELEVENLABS => Some(Box::new(clarp_speech::eleven_voice::ElevenVoice {
                key: read,
                model: if usage == SpeechUse::VoicePreview {
                    "eleven_multilingual_v2".into()
                } else {
                    owner::text_at(file, &["elevenlabs", "model"])
                        .unwrap_or_else(|| "eleven_flash_v2_5".into())
                },
                speed: if usage == SpeechUse::VoicePreview {
                    1.0
                } else {
                    file.get("elevenlabs")
                        .and_then(|v| v.get("speed"))
                        .and_then(|v| v.as_float().or_else(|| v.as_integer().map(|n| n as f64)))
                        .unwrap_or(1.2)
                },
                base: self.eleven_at.clone(),
                file: usage == SpeechUse::TextPreview,
            })),
            "deepgram" => Some(Box::new(clarp_speech::deepgram_voice::DeepgramVoice {
                key: read,
                base: self.deepgram_at.clone(),
            })),
            said::CARTESIA => {
                let engine = Cartesia::new(owner::cartesia_model(file), &self.language, read);
                Some(Box::new(match self.cartesia_at.as_str() {
                    "" => engine,
                    stand_in => engine.at(stand_in),
                }))
            }
            said::GEMINI => {
                let choice = owner::gemini(file);
                let surface = if choice.vertex {
                    Surface::Vertex
                } else {
                    Surface::GeminiApi
                };
                let engine = Gemini::new(choice.model, surface, read);
                Some(Box::new(match self.gemini_at.as_str() {
                    "" => engine,
                    stand_in => engine.at(stand_in),
                }))
            }
            _ => None,
        }
    }

    /// A new clip of this agent's: its number, its name, and its file, which is empty.
    fn new_clip(&self, session: &str) -> std::io::Result<(u64, String, PathBuf, std::fs::File)> {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&self.audio)?;
        let mut newest = self.newest.lock().unwrap_or_else(PoisonError::into_inner);
        let now = u64::try_from(self.now_ms()).unwrap_or(0);
        let mut number = now.max(*newest + 1);
        loop {
            let name = said::clip_name(Some(session), i64::try_from(number).unwrap_or(i64::MAX));
            let path = self.audio.join(&name);
            let made = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path);
            match made {
                Ok(file) => {
                    *newest = number;
                    return Ok((number, name, path, file));
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => number += 1,
                Err(error) => return Err(error),
            }
        }
    }

    /// Says an agent's lines for as long as it has one that waits. An agent's lines are
    /// taken one at a time, so whoever else speaks for it meanwhile finds nothing to take.
    async fn speak_for(self: Arc<Self>, agent_id: String) {
        loop {
            // A few at once: the rest wait here, before a line is taken, so that a line
            // that was taken is one that is being said.
            let _slot = self.at_once.acquire().await;
            let directory = self.fleet.directory();
            let mut members = directory.members.iter();
            let Some(member) = members.find(|member| member.record.agent_id == agent_id) else {
                return;
            };
            let Ok(Some(line)) = member.agent.claim_line().await else {
                return;
            };
            let looked_at = directory.focus.as_deref() == Some(agent_id.as_str());
            Self::say(&self, member, line, looked_at).await;
        }
    }

    /// A line nothing said, or not all of it: it is over, and why is written. The apps are
    /// told when their user should know.
    async fn unsaid(&self, member: &Member, line: &Line, clip: Option<u64>, why: &str, told: bool) {
        let ended = member
            .agent
            .line_ended(line.id.clone(), clip, Err(why.to_string()));
        if matches!(ended.await, Ok(true)) && told {
            self.hub
                .publish(wire::tts_error(&member.record, why), self.now_ms());
        }
    }

    /// Says one line: into a clip the apps are told of before its first byte is there,
    /// unless the herald holds it for its agent's turn.
    async fn say(self: &Arc<Self>, member: &Arc<Member>, line: Line, looked_at: bool) {
        let record = &member.record;
        let owners = self.owners();
        let has_key = |provider: &str| self.has_key(provider, &owners);
        let (by, unavailable) = match chosen(&owners, record, &has_key) {
            Chosen::Silent => return self.unsaid(member, &line, None, SPEECH_OFF, false).await,
            Chosen::Nobody { why, told } => {
                let voices = owner::voices(&owners);
                let route = voices.route(Who {
                    name: &record.persona,
                    persona: &record.persona,
                    session: &record.session,
                });
                if route.fallback.is_empty() {
                    return self.unsaid(member, &line, None, &why, told).await;
                }
                (
                    vec![(route.provider, route.voice.unwrap_or_default())],
                    Some(why),
                )
            }
            Chosen::By(by) => (by, None),
        };
        let engines: Vec<(Box<dyn Speaker>, String, String)> = by
            .iter()
            .filter_map(|(provider, voice)| {
                Some((
                    self.engine(&owners, provider, SpeechUse::Turn)?,
                    voice.clone(),
                    provider.clone(),
                ))
            })
            .collect();
        let text = said::announced(&line.text, line.first, &record.persona, looked_at);
        let (clip, name, path, mut file) = match self.new_clip(&record.session) {
            Ok(new) => new,
            Err(error) => {
                let why = format!("the clip's file could not be made: {error}");
                return self.unsaid(member, &line, None, &why, true).await;
            }
        };
        let start = Progress {
            bytes: 0,
            made: Made::Making,
        };
        let growing = Arc::new(Growing {
            name: name.clone(),
            path: path.clone(),
            progress: watch::channel(start).0,
        });
        self.held().insert(clip, growing.clone());
        // The herald says whether the clip is played now. When it is, the apps are told
        // where it is, and then the line is written as having it; when it is held, the
        // line has it and nobody is told yet. A line that is not this one's any more
        // leaves a clip that is nobody's.
        let ours = HeldClip {
            agent_id: record.agent_id.clone(),
            clip,
            name: name.clone(),
            trace_id: line.trace_id.clone(),
        };
        let signs = i64::try_from(text.chars().count()).unwrap_or(i64::MAX);
        let mut heralding = self.herald.lock().await;
        let directory = self.fleet.directory();
        let open = open_session(&directory);
        let at = Moment {
            open: &open,
            now_ms: self.now_ms(),
        };
        let settings = herald_settings(&directory);
        let taken = heralding.ingest((&record.session, ours), (Some(signs), true), at, settings);
        let (event_id, raised) = match taken {
            Taken::Decided(plays) => (self.tell(&directory, plays, Some(clip), None).await, None),
            Taken::Raise(attempt) => (None, Some(attempt)),
        };
        let begun = member
            .agent
            .line_begun(line.id.clone(), (clip, name), by[0].clone(), event_id);
        if !matches!(begun.await, Ok(true)) {
            heralding.withdraw(&record.session, |held| held.clip == clip);
            drop(heralding);
            growing.progress.send_replace(Progress {
                bytes: 0,
                made: Made::Failed,
            });
            self.held().remove(&clip);
            let _ = std::fs::remove_file(&path);
            return;
        }
        drop(heralding);
        if let Some(attempt) = raised {
            // The agent raises its hand while its reply is being said into the clip.
            tokio::spawn(self.clone().raise(attempt, member.clone()));
        }
        let sink = growing.clone();
        let made = tokio::task::spawn_blocking(move || {
            if let Some(why) = unavailable {
                return (Err(why), 0);
            }
            let listed: Vec<(&dyn Speaker, &str)> = engines
                .iter()
                .map(|(engine, voice, _)| (engine.as_ref(), voice.as_str()))
                .collect();
            let mut written = 0_u64;
            let mut broken: Option<String> = None;
            let said = said_by_the_first_that_can(&listed, &text, &mut |piece| {
                if broken.is_some() {
                    return;
                }
                match file.write_all(piece) {
                    Ok(()) => {
                        written += piece.len() as u64;
                        sink.progress.send_replace(Progress {
                            bytes: written,
                            made: Made::Making,
                        });
                    }
                    Err(error) => broken = Some(error.to_string()),
                }
            });
            let spoke = match (said, broken) {
                (_, Some(error)) => Err(format!("the clip could not be written: {error}")),
                (Ok((place, _)), None) => Ok((engines[place].2.clone(), engines[place].1.clone())),
                (Err(Mute::Unavailable(why) | Mute::Failed { why, .. }), None) => Err(why),
            };
            (spoke, written)
        });
        let (spoke, written) = match made.await {
            Ok(made) => made,
            Err(_) => (Err("the engine's thread ended".to_string()), 0),
        };
        // Whoever plays the clip as it grows hears at once that it grows no more.
        growing.progress.send_replace(Progress {
            bytes: written,
            made: match spoke {
                Ok(_) => Made::Whole,
                Err(_) => Made::Failed,
            },
        });
        match spoke {
            Ok((provider, voice)) => {
                let said = Spoke {
                    bytes: written,
                    provider,
                    voice,
                };
                let _ = member
                    .agent
                    .line_ended(line.id.clone(), Some(clip), Ok(said))
                    .await;
            }
            Err(why) => {
                // A clip nothing came of is no file before anybody is told so, and is not
                // played later either.
                let mut heralding = self.herald.lock().await;
                heralding.withdraw(&record.session, |held| held.clip == clip);
                drop(heralding);
                if written == 0 {
                    let _ = std::fs::remove_file(&path);
                }
                self.unsaid(member, &line, Some(clip), &why, true).await;
            }
        }
        // From here on the clip is found by its line.
        self.held().remove(&clip);
    }
}

// ---- the herald --------------------------------------------------------------------------------

/// The session of the agent that is open; empty when none is.
fn open_session(directory: &Directory) -> String {
    let open = directory.focus.as_deref().and_then(|open| {
        let mut members = directory.members.iter();
        members.find(|member| member.record.agent_id == open)
    });
    open.map_or_else(String::new, |member| member.record.session.clone())
}

/// How the owner has set the herald.
fn herald_settings(directory: &Directory) -> herald::Settings {
    herald::Settings::stored(
        directory.setting(herald::DISABLED_KEY),
        directory.setting(herald::SHORT_KEY),
        directory.setting(herald::BYPASS_KEY),
    )
}

impl Mouth {
    /// Tells the apps of what the herald says is played now, in its order, and writes on
    /// each clip's line the event it was told with. `this` is a clip whose line is written
    /// by whoever asks (it is being begun): the event it was told with is answered, when it
    /// is among them. `herald_file` is the herald's own clip, when one was made.
    async fn tell(
        &self,
        directory: &Directory,
        plays: Vec<Play<HeldClip>>,
        this: Option<u64>,
        herald_file: Option<&str>,
    ) -> Option<u64> {
        let mut told_this = None;
        for play in plays {
            match play {
                Play::Herald { session } => {
                    if let Some(name) = herald_file {
                        self.hub
                            .publish(wire::herald(&session, name), self.now_ms());
                    }
                }
                Play::Clip {
                    clip: held, across, ..
                } => {
                    let mut members = directory.members.iter();
                    let Some(member) =
                        members.find(|member| member.record.agent_id == held.agent_id)
                    else {
                        continue;
                    };
                    let trace = held.trace_id.as_deref();
                    let event = if across {
                        wire::audio_across(&member.record, held.clip, &held.name, trace)
                    } else {
                        wire::audio(&member.record, held.clip, &held.name, trace)
                    };
                    let event_id = self.hub.publish(event, self.now_ms());
                    if this == Some(held.clip) {
                        told_this = Some(event_id);
                    } else {
                        let _ = member
                            .agent
                            .line_announced(held.clip, event_id, across)
                            .await;
                    }
                }
            }
        }
        told_this
    }

    /// An agent raises its hand: the herald's words in the agent's own voice, in a clip of
    /// their own; then what the herald says of it is played. When the user has turned to
    /// the agent meanwhile, the clip is nobody's.
    async fn raise(self: Arc<Self>, attempt: Attempt, member: Arc<Member>) {
        let made = self.herald_clip(&member.record).await;
        let mut heralding = self.herald.lock().await;
        let directory = self.fleet.directory();
        let open = open_session(&directory);
        let at = Moment {
            open: &open,
            now_ms: self.now_ms(),
        };
        let plays = heralding.made(&attempt, made.is_some(), at);
        let played = plays.iter().any(|play| matches!(play, Play::Herald { .. }));
        let name = made.as_ref().map(|(name, _)| name.as_str());
        self.tell(&directory, plays, None, name).await;
        drop(heralding);
        if let (Some((_, path)), false) = (made, played) {
            let _ = std::fs::remove_file(path);
        }
    }

    /// The herald's clip for an agent: its name and its file. `None`: nothing on this Host
    /// can say it now.
    async fn herald_clip(&self, record: &AgentRecord) -> Option<(String, PathBuf)> {
        let text = herald::words(&record.persona, &record.session);
        self.clip_of(record, text, HERALD_FILE).await
    }

    /// A line an agent says outside any turn, in its own voice, as a clip of its own: the
    /// name of its file, which `/audio/<name>` hands out. A ring's opening line is one: it
    /// is voiced when the ring is placed, so that answering plays it at once. `None`:
    /// nothing on this Host can say it now.
    pub async fn said_outside_a_turn(&self, record: &AgentRecord, text: &str) -> Option<String> {
        let made = self.clip_of(record, text.to_string(), "").await;
        made.map(|(name, _)| name)
    }

    /// Something said aloud outside every turn and told to the apps at once, as a clip
    /// that is played whichever agent is open: a question the router of spoken messages
    /// asks as an agent, or a word of its own (a speaker with no session). Where nothing on
    /// this Host can say it, nothing is said. Answers whether it was.
    pub(crate) async fn announce(&self, speaker: &AgentRecord, text: &str) -> bool {
        let Some((name, _)) = self.clip_of(speaker, text.to_string(), "").await else {
            return false;
        };
        let session = Some(speaker.session.as_str()).filter(|session| !session.is_empty());
        self.hub
            .publish(wire::announcement(session, &name), self.now_ms());
        true
    }

    /// Plays again what an agent last said aloud: its newest clip, told to the apps once
    /// more. Answers whether it has one.
    pub(crate) async fn again(&self, member: &Member) -> bool {
        let view = member.agent.view();
        let lines = view.speech.lines().iter().rev();
        let last = lines
            .filter_map(|line| wire::audio_kept(&member.record, line))
            .next();
        let Some(mut clip) = last else {
            return false;
        };
        clip["replay"] = json!(true);
        self.hub.publish(clip, self.now_ms());
        true
    }

    /// A clip of `text` in an agent's voice, under a name that begins with `prefix`: its
    /// name and its file.
    async fn clip_of(
        &self,
        record: &AgentRecord,
        text: String,
        prefix: &'static str,
    ) -> Option<(String, PathBuf)> {
        let owners = self.owners();
        let has_key = |provider: &str| self.has_key(provider, &owners);
        let Chosen::By(by) = chosen(&owners, record, &has_key) else {
            return None;
        };
        let engines: Vec<(Box<dyn Speaker>, String)> = by
            .iter()
            .filter_map(|(provider, voice)| {
                Some((
                    self.engine(&owners, provider, SpeechUse::Turn)?,
                    voice.clone(),
                ))
            })
            .collect();
        let (audio, session, now) = (self.audio.clone(), record.session.clone(), self.now_ms());
        let made = tokio::task::spawn_blocking(move || -> Option<(String, PathBuf)> {
            let listed: Vec<(&dyn Speaker, &str)> = engines
                .iter()
                .map(|(engine, voice)| (engine.as_ref(), voice.as_str()))
                .collect();
            let mut sound = Vec::new();
            let said = said_by_the_first_that_can(&listed, &text, &mut |piece| {
                sound.extend_from_slice(piece);
            });
            if said.is_err() || sound.is_empty() {
                return None;
            }
            std::fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&audio)
                .ok()?;
            // A name of its own: the moment it was made, and the next when that is taken.
            for later in 0..100 {
                let name = format!("{prefix}{}", said::clip_name(Some(&session), now + later));
                let path = audio.join(&name);
                let file = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&path);
                match file {
                    Ok(mut file) => {
                        return match file.write_all(&sound) {
                            Ok(()) => Some((name, path)),
                            Err(_) => {
                                let _ = std::fs::remove_file(&path);
                                None
                            }
                        };
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                    Err(_) => return None,
                }
            }
            None
        });
        made.await.ok().flatten()
    }

    /// The user opened an agent: what the herald held of it is played.
    pub(crate) async fn opened(&self, session: &str) {
        let mut heralding = self.herald.lock().await;
        let plays = heralding.opened(session);
        self.tell(&self.fleet.directory(), plays, None, None).await;
    }

    /// A message went to an agent: for a minute its answer is the one that is waited for,
    /// and what the herald held of it is played.
    pub(crate) async fn sent(&self, session: &str) {
        let mut heralding = self.herald.lock().await;
        let plays = heralding.sent(session, self.now_ms());
        self.tell(&self.fleet.directory(), plays, None, None).await;
    }

    /// The user said something. A yes with the name of an agent whose hand is up lets what
    /// it holds be played; a no leaves it held. Answers whether it was one of the two: what
    /// was said is then a word to the herald, and no message for an agent.
    pub(crate) async fn heard(&self, text: &str) -> bool {
        let mut heralding = self.herald.lock().await;
        let directory = self.fleet.directory();
        let name_of = |session: &str| -> String {
            let member = directory.by_session(session);
            member.map_or_else(String::new, |member| member.record.persona.clone())
        };
        let (granted, declined, plays) = heralding.said(text, &name_of);
        self.tell(&directory, plays, None, None).await;
        !(granted.is_empty() && declined.is_empty())
    }
}

// ---- finding a clip ---------------------------------------------------------------------------

/// The line that became the clip with this number, and whose it is, as the agents' logs
/// have it.
fn clip_of(directory: &Directory, clip: u64) -> Option<(Arc<Member>, Line)> {
    directory.members.iter().find_map(|member| {
        let view = member.agent.view();
        let line = view.speech.by_clip(clip)?;
        Some((member.clone(), line.clone()))
    })
}

/// The line whose clip's file has this name, and whose it is.
fn clip_named(directory: &Directory, name: &str) -> Option<(Arc<Member>, Line)> {
    directory.members.iter().find_map(|member| {
        let view = member.agent.view();
        let line = view.speech.by_file(name)?;
        Some((member.clone(), line.clone()))
    })
}

// ---- a clip's bytes ---------------------------------------------------------------------------

fn plain(status: StatusCode, text: &'static str) -> Response {
    ApiError::plain(status, text).into_response()
}

/// What a file is, by its name.
fn kind_of(path: &FilePath) -> &'static str {
    match path.extension().and_then(|end| end.to_str()) {
        Some("mp3") => "audio/mpeg",
        _ => "application/octet-stream",
    }
}

/// Up to `most` bytes of a file from `at` on.
fn read_at(path: &FilePath, at: u64, most: usize) -> std::io::Result<Vec<u8>> {
    let mut file = std::fs::File::open(path)?;
    file.seek(SeekFrom::Start(at))?;
    let mut read = Vec::with_capacity(most.min(CHUNK));
    file.take(most as u64).read_to_end(&mut read)?;
    Ok(read)
}

/// A file's bytes from `first` on, `length` of them, read and sent a part at a time.
fn file_body(path: PathBuf, first: u64, length: u64) -> Body {
    let parts = stream::unfold((first, length), move |(at, left)| {
        let path = path.clone();
        async move {
            if left == 0 {
                return None;
            }
            let most = usize::try_from(left).map_or(CHUNK, |left| left.min(CHUNK));
            let read = tokio::task::spawn_blocking(move || read_at(&path, at, most)).await;
            match read {
                Ok(Ok(part)) if !part.is_empty() => {
                    let sent = part.len() as u64;
                    let rest = (at + sent, left.saturating_sub(sent));
                    Some((Ok(Bytes::from(part)), rest))
                }
                // The file is shorter than it was, or cannot be read: the answer ends short
                // of the length it promised, and the connection ends with it.
                _ => {
                    let short = std::io::Error::other("a clip's file ended early");
                    Some((Err(short), (at, 0)))
                }
            }
        }
    });
    Body::from_stream(parts)
}

/// An answer with these headers. A value that is no header's is left out.
fn answer(status: StatusCode, said: Vec<(header::HeaderName, String)>, body: Body) -> Response {
    let mut response = Response::new(body);
    *response.status_mut() = status;
    for (name, value) in said {
        if let Ok(value) = HeaderValue::from_str(&value) {
            response.headers_mut().insert(name, value);
        }
    }
    response
}

/// Some bytes of a clip's file, or all of them, with what a player needs to know of them.
/// `of` is the whole clip's length as it is told: a number, or `*` while it still grows.
fn part_of_file(path: PathBuf, part: Part, there: u64, of: &str) -> Response {
    let (status, first, length) = match part {
        Part::Whole => (StatusCode::OK, 0, there),
        Part::Bytes { first, last } => (StatusCode::PARTIAL_CONTENT, first, last - first + 1),
        Part::Missing => return no_such_part(of, false),
    };
    let mut said = vec![
        (header::CONTENT_TYPE, kind_of(&path).to_string()),
        (header::CONTENT_LENGTH, length.to_string()),
        (header::ACCEPT_RANGES, "bytes".to_string()),
    ];
    if status == StatusCode::PARTIAL_CONTENT {
        let last = first + length - 1;
        said.push((header::CONTENT_RANGE, format!("bytes {first}-{last}/{of}")));
    }
    answer(status, said, file_body(path, first, length))
}

/// The part that was asked for begins where the clip has no byte. `as_a_file`: answered
/// as the Python Host answers for any file it sends, which says a little more.
fn no_such_part(of: &str, as_a_file: bool) -> Response {
    let mut said = vec![
        (header::CONTENT_RANGE, format!("bytes */{of}")),
        (header::CONTENT_LENGTH, "0".to_string()),
    ];
    if as_a_file {
        said.push((header::CONTENT_TYPE, "text/plain".to_string()));
        said.push((header::ACCEPT_RANGES, "bytes".to_string()));
    }
    answer(StatusCode::RANGE_NOT_SATISFIABLE, said, Body::empty())
}

/// A file as the Python Host sends any file (`_send_file`): all of it, or the part a
/// `Range` header asks for, read the way that Host reads one.
pub(crate) fn send_file(path: PathBuf, range: &str) -> Response {
    let total = match std::fs::symlink_metadata(&path) {
        Ok(found) if found.is_file() => found.len(),
        _ => return plain(StatusCode::NOT_FOUND, "not found"),
    };
    match media::byte_range(range, total) {
        Part::Missing => no_such_part(&total.to_string(), true),
        part => part_of_file(path, part, total, &total.to_string()),
    }
}

/// A clip that is whole, as the Python Host serves one by its number: all of it, or the
/// part a player asks for, read the way that Host reads it for a clip.
fn send_clip(path: PathBuf, range: Option<(u64, Option<u64>)>) -> Response {
    let Ok(found) = std::fs::metadata(&path) else {
        return plain(StatusCode::INTERNAL_SERVER_ERROR, "stat failed");
    };
    let total = found.len();
    part_of_file(
        path,
        said::clip_part(range, total),
        total,
        &total.to_string(),
    )
}

/// A clip that is being made, from its first byte on, as its bytes come: for a player that
/// takes sound as a stream. It ends when the clip is whole, when no engine will finish it,
/// or when nothing came for too long.
fn as_it_grows(growing: Arc<Growing>) -> Response {
    let parts = stream::unfold(0_u64, move |at| {
        let growing = growing.clone();
        async move {
            let mut heard = growing.progress.subscribe();
            let until = Instant::now() + WAITS;
            loop {
                let progress = *heard.borrow_and_update();
                if progress.bytes > at {
                    let left = progress.bytes - at;
                    let most = usize::try_from(left).map_or(CHUNK, |left| left.min(CHUNK));
                    let path = growing.path.clone();
                    let read = tokio::task::spawn_blocking(move || read_at(&path, at, most)).await;
                    return match read {
                        Ok(Ok(part)) if !part.is_empty() => {
                            let next = at + part.len() as u64;
                            Some((Ok::<Bytes, std::io::Error>(Bytes::from(part)), next))
                        }
                        _ => None,
                    };
                }
                if progress.made != Made::Making {
                    return None;
                }
                match tokio::time::timeout_at(until, heard.changed()).await {
                    Ok(Ok(())) => {}
                    _ => return None,
                }
            }
        }
    });
    let said = vec![
        (header::CONTENT_TYPE, "audio/mpeg".to_string()),
        (header::CONNECTION, "close".to_string()),
    ];
    answer(StatusCode::OK, said, Body::from_stream(parts))
}

/// Waits until a clip that is being made has the byte `last`, or is over, or the wait is.
/// Without a `last`, until it is over. Answers how far it has come.
async fn enough(growing: &Growing, last: Option<u64>) -> Progress {
    let mut heard = growing.progress.subscribe();
    let until = Instant::now() + WAITS;
    loop {
        let progress = *heard.borrow_and_update();
        let there = last.is_some_and(|last| progress.bytes > last);
        if there || progress.made != Made::Making {
            return progress;
        }
        match tokio::time::timeout_at(until, heard.changed()).await {
            Ok(Ok(())) => {}
            _ => return growing.now(),
        }
    }
}

/// A part of a clip that is being made, for a player that asks for parts: it is given as
/// far as the clip has come, and told that how long the clip will be is not known yet.
async fn part_as_it_grows(growing: Arc<Growing>, range: (u64, Option<u64>)) -> Response {
    let progress = enough(&growing, range.1).await;
    let of = match progress.made {
        Made::Making => "*".to_string(),
        _ => progress.bytes.to_string(),
    };
    match said::clip_part(Some(range), progress.bytes) {
        Part::Missing => no_such_part(&of, false),
        part => part_of_file(growing.path.clone(), part, progress.bytes, &of),
    }
}

// ---- the routes -------------------------------------------------------------------------------

pub fn routes() -> Router<App> {
    Router::new()
        .route("/audio/", get(nothing_named))
        .route("/audio/{*name}", get(audio))
        .route("/clips/ack", post(ack))
        .route("/clips/recoverable", get(recoverable))
        .route("/clips/message", get(of_message))
        .route("/clips/{clip}/stream", get(clip_stream))
        .route("/clips/{clip}/complete.mp3", get(clip_complete))
}

async fn nothing_named() -> Response {
    plain(StatusCode::FORBIDDEN, "forbidden")
}

/// `GET /audio/<name>`: a clip by the name of its file. One that is being made is sent as
/// it grows; one that is whole is sent as a file, in parts when a player asks for parts.
async fn audio(State(app): State<App>, Path(name): Path<String>, headers: HeaderMap) -> Response {
    // Nothing outside the place where clips are kept is anybody's to fetch.
    if matches!(name.as_str(), "" | ".") || name.split('/').any(|part| part == "..") {
        return plain(StatusCode::FORBIDDEN, "forbidden");
    }
    if name.contains(['/', '\0']) {
        return plain(StatusCode::NOT_FOUND, "not found");
    }
    if let Some(growing) = app.mouth.growing_named(&name) {
        return as_it_grows(growing);
    }
    send_file(app.mouth.audio.join(&name), &header_text(&headers, "range"))
}

/// `GET /clips/<number>/stream`: a clip by its number, in the shape the player asks for.
/// While it is being made: as a stream, or the part that was asked for as far as the clip
/// has come. Once it is over: its file.
async fn clip_stream(
    State(app): State<App>,
    Path(clip): Path<String>,
    headers: HeaderMap,
) -> Response {
    let Some(number) = python::int(&clip) else {
        return plain(StatusCode::BAD_REQUEST, "bad clip id");
    };
    // A number no clip has is no clip.
    let clip = u64::try_from(number).ok();
    let range = said::clip_range(&header_text(&headers, "range"));
    let growing = clip.and_then(|clip| app.mouth.growing(clip));
    if let Some(growing) = &growing
        && growing.now().made == Made::Making
    {
        return match range {
            Some(range) => part_as_it_grows(growing.clone(), range).await,
            None => as_it_grows(growing.clone()),
        };
    }
    let path = growing.map(|growing| growing.path.clone()).or_else(|| {
        let (_, line) = clip_of(&app.fleet.directory(), clip?)?;
        Some(app.mouth.audio.join(line.clip?))
    });
    match path.filter(|path| path.is_file()) {
        Some(path) => send_clip(path, range),
        None => {
            let said = vec![
                (header::CONTENT_TYPE, "text/plain".to_string()),
                (header::CONNECTION, "close".to_string()),
            ];
            answer(StatusCode::NOT_FOUND, said, Body::from("no such clip"))
        }
    }
}

/// `GET /clips/<number>/complete.mp3`: the whole clip, once it is whole. A player that
/// cannot play a stream asks here and waits.
async fn clip_complete(
    State(app): State<App>,
    Path(clip): Path<String>,
    headers: HeaderMap,
) -> Response {
    let Some(number) = python::int(&clip) else {
        return plain(StatusCode::BAD_REQUEST, "bad clip id");
    };
    let Ok(clip) = u64::try_from(number) else {
        return plain(StatusCode::NOT_FOUND, "not found");
    };
    let range = header_text(&headers, "range");
    let until = Instant::now() + WAITS;
    loop {
        if let Some(growing) = app.mouth.growing(clip) {
            let mut heard = growing.progress.subscribe();
            let over = heard.wait_for(|progress| progress.made != Made::Making);
            let made = match tokio::time::timeout_at(until, over).await {
                Ok(Ok(progress)) => progress.made,
                _ => Made::Making,
            };
            return match made {
                Made::Whole => send_file(growing.path.clone(), &range),
                Made::Failed => plain(StatusCode::GONE, "clip failed"),
                Made::Making => plain(StatusCode::GATEWAY_TIMEOUT, "clip not complete"),
            };
        }
        let Some((_, line)) = clip_of(&app.fleet.directory(), clip) else {
            return plain(StatusCode::NOT_FOUND, "not found");
        };
        let whole = match line.state {
            LineState::Failed => return plain(StatusCode::GONE, "clip failed"),
            LineState::Spoken => line.clip.map(|name| app.mouth.audio.join(name)),
            _ => None,
        };
        if let Some(path) = whole.filter(|path| path.is_file()) {
            return send_file(path, &range);
        }
        if Instant::now() >= until {
            return plain(StatusCode::GATEWAY_TIMEOUT, "clip not complete");
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

fn bad(why: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, why)
}

/// `POST /clips/ack` with `{clip_id, url, status, error}`: an app says what became of a
/// clip in its hands: it has it, it holds it back, it plays it, it played it, it could not.
async fn ack(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    // No body at all is an empty one, as the Python Host reads it.
    let said: Map<String, Value> = if body.is_empty() {
        Map::new()
    } else {
        match serde_json::from_slice(&body) {
            Ok(Value::Object(said)) => said,
            _ => return Err(bad("bad json")),
        }
    };
    let text = |name: &str| said.get(name).and_then(Value::as_str).map_or("", str::trim);
    let meant = spoken::clip_meant(said.get("clip_id")).map_err(|_| bad("bad clip_id"))?;
    let status = text("status");
    if !spoken::PLAYED.contains(&status) {
        return Err(bad("bad status"));
    }
    let error = Some(text("error")).filter(|error| !error.is_empty());
    let directory = app.fleet.directory();
    let found = match meant {
        Meant::Clip(clip) => clip_of(&directory, clip),
        // By the name at the end of its address.
        Meant::ByAddress => match text("url").rsplit('/').next() {
            Some(name) if !name.is_empty() => clip_named(&directory, name),
            _ => None,
        },
        Meant::NoClip => None,
    };
    let updated = match found.and_then(|(member, line)| Some((member, line.clip_id?, line.turn))) {
        Some((member, clip, turn)) => {
            let error = error.map(str::to_string);
            let played = member.agent.line_played(clip, status.to_string(), error);
            let updated = played.await?;
            // What an app says of a clip is seen as it says it, for the clip's books.
            if updated {
                crate::audio::said(&app, &member.record.agent_id, clip, turn, status).await;
            }
            updated
        }
        None => false,
    };
    Ok(Json(json!({ "ok": true, "updated": updated })))
}

/// The clips of the agents a request is about, with whose they are: every agent's, or one
/// session's.
fn lines_of(directory: &Directory, session: &str) -> Vec<(Arc<Member>, Arc<View>)> {
    let members = directory.members.iter();
    members
        .filter(|member| session.is_empty() || member.record.session == session)
        .map(|member| (member.clone(), member.agent.view()))
        .collect()
}

/// `GET /clips/recoverable?session=`: the clips an app may have missed, as they were
/// announced. An app that was not listening asks when it listens again.
async fn recoverable(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
) -> Json<Value> {
    let session = asked.get("session").map_or("", |session| session.trim());
    let directory = app.fleet.directory();
    let views = lines_of(&directory, session);
    let clips: Vec<(&AgentRecord, &Line)> = views
        .iter()
        .flat_map(|(member, view)| {
            let lines = view.speech.lines().iter();
            lines.map(move |line| (&member.record, line))
        })
        .filter(|(_, line)| line.clip_id.is_some())
        .collect();
    let lines: Vec<&Line> = clips.iter().map(|(_, line)| *line).collect();
    let events: Vec<Value> = spoken::offered_again(&lines, app.clock.now_ms())
        .into_iter()
        .filter_map(|place| {
            let (record, line) = clips[place];
            wire::audio_of(record, line)
        })
        .collect();
    Json(json!({ "events": events }))
}

/// `GET /clips/message?session=&message_id=`: the clips of one reply, to be played again.
/// All of what the reply said aloud, or nothing: half of it is never played.
async fn of_message(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let given = |name: &str| asked.get(name).map_or("", |value| value.trim());
    let (session, message) = (given("session"), given("message_id"));
    let too_long = session.chars().count() > 256 || message.chars().count() > 1024;
    if session.is_empty() || message.is_empty() || too_long {
        return Err(bad("session and message_id required"));
    }
    let unavailable = || {
        ApiError::new(
            StatusCode::NOT_FOUND,
            "Retained audio is unavailable for this message",
        )
    };
    let member = app.member(session).ok_or_else(unavailable)?;
    let view = member.agent.view();
    let lines: Vec<&Line> = view.speech.of_reply(message).collect();
    if lines.is_empty() || lines.len() > 128 {
        return Err(unavailable());
    }
    let mut events = Vec::with_capacity(lines.len());
    for line in lines {
        let kept = line.state == LineState::Spoken
            && line
                .clip
                .as_ref()
                .is_some_and(|name| app.mouth.audio.join(name).is_file());
        let event = kept.then(|| wire::audio_kept(&member.record, line));
        events.push(event.flatten().ok_or_else(unavailable)?);
    }
    Ok(Json(json!({ "events": events })))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn record(persona: &str, voice_id: &str) -> AgentRecord {
        AgentRecord {
            agent_id: "a1".to_string(),
            session: persona.to_lowercase(),
            persona: persona.to_string(),
            voice_id: voice_id.to_string(),
            ..AgentRecord::default()
        }
    }

    fn file(text: &str) -> Table {
        text.parse().unwrap()
    }

    fn by(engines: &[(&str, &str)]) -> Chosen {
        let engines = engines.iter();
        Chosen::By(
            engines
                .map(|(provider, voice)| ((*provider).to_string(), (*voice).to_string()))
                .collect(),
        )
    }

    #[test]
    fn the_owner_chooses_who_speaks_and_the_agent_has_its_voice() {
        let all = |_: &str| true;
        let none = |_: &str| false;
        // Gemini for everyone, Cartesia after it: both, each in the persona's voice.
        let owners = file(
            "[tts]\nprovider = \"gemini\"\nfallback = \"cartesia\"\n\
             [gemini_tts]\nbackend = \"vertex\"\n\
             [gemini_tts.voices]\nMaximus = \"voice_api\"\n\
             [gemini_tts.vertex_voices]\nMaximus = \"voice_vertex\"\n\
             [cartesia.voices]\nMaximus = \"c-maximus\"\n",
        );
        let maximus = record("Maximus", "");
        assert_eq!(
            chosen(&owners, &maximus, &all),
            by(&[("gemini", "voice_vertex"), ("cartesia", "c-maximus")]),
            "a voice designed at one of Google's doors is the same persona's at the other"
        );
        // The engine whose key is not there is passed over before anything is announced.
        let only_cartesia = |provider: &str| provider == "cartesia";
        assert_eq!(
            chosen(&owners, &maximus, &only_cartesia),
            by(&[("cartesia", "c-maximus")])
        );
        assert_eq!(
            chosen(&owners, &maximus, &none),
            Chosen::Nobody {
                why: "the gemini api_key is not configured".to_string(),
                told: true
            }
        );
        // An agent nobody gave a voice stays silent without a word.
        let nobody = record("Stranger", "");
        assert_eq!(
            chosen(&file("[tts]\nprovider = \"gemini\"\n"), &nobody, &all),
            Chosen::Nobody {
                why: "no gemini voice configured for 'Stranger'".to_string(),
                told: false
            }
        );
        // The voice an agent was given itself is the one it speaks in.
        let stored = record("Stranger", "{\"cartesia\": \"c-own\"}");
        assert_eq!(
            chosen(&file(""), &stored, &all),
            by(&[("cartesia", "c-own")]),
            "Cartesia unless the owner says otherwise"
        );
        // The owner turned speech off.
        assert_eq!(
            chosen(&file("[tts]\nprovider = \"none\"\n"), &maximus, &all),
            Chosen::Silent
        );
        // An engine this Host does not have: the next one speaks, or the apps are told.
        let elsewhere = file(
            "[tts]\nprovider = \"elevenlabs\"\nfallback = \"cartesia\"\n\
             [cartesia.voices]\nMaximus = \"c-maximus\"\n",
        );
        let with_both = record("Maximus", "{\"elevenlabs\": \"e-own\"}");
        assert_eq!(
            chosen(&elsewhere, &with_both, &all),
            by(&[("elevenlabs", "e-own"), ("cartesia", "c-maximus")])
        );
        let alone = file("[tts]\nprovider = \"elevenlabs\"\n");
        assert_eq!(
            chosen(&alone, &with_both, &all),
            by(&[("elevenlabs", "e-own")])
        );
        // One agent moved to another engine on purpose.
        let moved = file(
            "[tts]\nprovider = \"cartesia\"\n[tts.agents.maximus]\nprovider = \"gemini\"\n\
             gemini_voice = \"Puck\"\n",
        );
        assert_eq!(chosen(&moved, &maximus, &all), by(&[("gemini", "Puck")]));
    }

    #[test]
    fn clips_nobody_will_play_again_are_cleared_away() {
        let dir = std::env::temp_dir().join(format!("clarp-voice-sweep-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let now = 1_763_280_000_000_i64;
        let old = now - spoken::KEPT_FOR_MS - 1;
        let names = [
            said::clip_name(Some("maximus"), now - 5_000),
            said::clip_name(Some("maximus"), old),
            said::clip_name(None, now - 1_000),
        ];
        for name in &names {
            std::fs::write(dir.join(name), b"sound").unwrap();
        }
        std::fs::write(dir.join("notes.txt"), b"not a clip").unwrap();
        assert_eq!(newest_clip(&dir), u64::try_from(now - 1_000).unwrap());
        sweep(&dir, now);
        let mut left: Vec<String> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        left.sort();
        let mut kept = vec![names[0].clone(), names[2].clone(), "notes.txt".to_string()];
        kept.sort();
        assert_eq!(left, kept, "what is no clip is nobody's to clear away");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_part_of_a_file_is_read_where_it_is() {
        let path = std::env::temp_dir().join(format!("clarp-voice-read-{}", std::process::id()));
        std::fs::write(&path, b"0123456789").unwrap();
        assert_eq!(read_at(&path, 0, 4).unwrap(), b"0123");
        assert_eq!(read_at(&path, 8, 4).unwrap(), b"89");
        assert_eq!(read_at(&path, 10, 4).unwrap(), b"");
        assert_eq!(kind_of(&path), "application/octet-stream");
        assert_eq!(kind_of(FilePath::new("1__a.mp3")), "audio/mpeg");
        std::fs::remove_file(&path).unwrap();
    }
}
