//! Voice playback and dictation (C++ `AudioController`, `WavEncoder`,
//! `RecordingSession`). Qt-free: the player is a state machine that returns
//! effects (download this, play these bytes, acknowledge that clip) for the
//! Qt object to run, so the queue, source choice and acknowledgements are
//! testable without audio hardware.

use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::path::PathBuf;

use serde_json::Value;
use url::Url;

use crate::json::Object;
use crate::protocol::AudioClip;

/// A clip file shorter than this is a click, not speech.
pub const MIN_WAV_BYTES: usize = 1_068;
pub const PLAYBACK_RATE: f64 = 1.2;
/// How often queued clips are retried (C++ `m_pendingPlayback`).
pub const PENDING_PLAYBACK_MS: u64 = 250;

// ---- WAV --------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PcmFormat {
    pub sample_rate: u32,
    pub channels: u16,
    pub bytes_per_sample: u16,
    pub float: bool,
}

impl PcmFormat {
    pub const DICTATION: PcmFormat = PcmFormat { sample_rate: 16_000, channels: 1, bytes_per_sample: 2, float: false };
}

/// A RIFF/WAVE file around little-endian PCM.
pub fn encode_wav(pcm: &[u8], format: PcmFormat) -> Vec<u8> {
    if pcm.is_empty() || format.bytes_per_sample == 0 || format.channels == 0 || pcm.len() > (u32::MAX - 44) as usize {
        return Vec::new();
    }
    let block_align = format.channels * format.bytes_per_sample;
    let mut wav = Vec::with_capacity(pcm.len() + 44);
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(b"WAVEfmt ");
    wav.extend_from_slice(&16u32.to_le_bytes());
    wav.extend_from_slice(&(if format.float { 3u16 } else { 1u16 }).to_le_bytes());
    wav.extend_from_slice(&format.channels.to_le_bytes());
    wav.extend_from_slice(&format.sample_rate.to_le_bytes());
    wav.extend_from_slice(&(format.sample_rate * u32::from(block_align)).to_le_bytes());
    wav.extend_from_slice(&block_align.to_le_bytes());
    wav.extend_from_slice(&(format.bytes_per_sample * 8).to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&(pcm.len() as u32).to_le_bytes());
    wav.extend_from_slice(pcm);
    wav
}

// ---- microphone lease -----------------------------------------------------------

/// The microphone is exclusive across Hosts and windows (C++
/// `RecordingSession`): an advisory lock on a file in the runtime directory.
/// Its destination chat is fixed until release, whatever the UI focus does.
#[derive(Debug)]
pub struct RecordingLease {
    path: PathBuf,
    lock: Option<File>,
    target: String,
}

impl RecordingLease {
    pub fn new(path: Option<PathBuf>) -> Self {
        let path = path.unwrap_or_else(|| {
            std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from).unwrap_or_else(std::env::temp_dir).join("clarp-desktop-microphone.lock")
        });
        Self { path, lock: None, target: String::new() }
    }

    fn try_lock(&self) -> Option<File> {
        let file = File::options().create(true).truncate(false).write(true).open(&self.path).ok()?;
        file.try_lock().ok().map(|()| file)
    }

    pub fn acquire(&mut self, session: &str) -> bool {
        if self.lock.is_some() {
            return false;
        }
        let Some(file) = self.try_lock() else { return false };
        self.lock = Some(file);
        self.target = session.to_owned();
        true
    }

    /// Someone (this window or another) is recording.
    pub fn busy(&self) -> bool {
        self.lock.is_some() || self.try_lock().is_none()
    }

    pub fn active(&self) -> bool {
        self.lock.is_some()
    }

    /// Releases the microphone; returns the chat the recording was for.
    pub fn release(&mut self) -> String {
        self.lock = None; // dropping the file releases the lock
        std::mem::take(&mut self.target)
    }
}

// ---- transcriptions -------------------------------------------------------------

/// Dictations in flight, per chat: each result goes to the chat it was
/// recorded for, however focus moved meanwhile.
#[derive(Debug, Default)]
pub struct Transcriptions {
    /// request tag -> chat
    requests: HashMap<String, String>,
    by_session: HashMap<String, i32>,
}

impl Transcriptions {
    pub fn in_flight(&self) -> i32 {
        self.requests.len() as i32
    }
    pub fn for_session(&self, session: &str) -> i32 {
        self.by_session.get(session).copied().unwrap_or(0)
    }
    pub fn start(&mut self, tag: &str, session: &str) {
        self.requests.insert(tag.to_owned(), session.to_owned());
        *self.by_session.entry(session.to_owned()).or_default() += 1;
    }
    /// The chat a finished request was for; None when it was cancelled.
    pub fn finish(&mut self, tag: &str) -> Option<String> {
        let session = self.requests.remove(tag)?;
        if let Some(count) = self.by_session.get_mut(&session) {
            *count -= 1;
            if *count <= 0 {
                self.by_session.remove(&session);
            }
        }
        Some(session)
    }
    /// Cancels a chat's dictations; their results are dropped when they arrive.
    pub fn cancel_session(&mut self, session: &str) -> bool {
        let tags: Vec<String> = self.requests.iter().filter(|(_, s)| *s == session).map(|(t, _)| t.clone()).collect();
        for tag in &tags {
            self.finish(tag);
        }
        !tags.is_empty()
    }
    /// A Host or account switch cancels everything.
    pub fn cancel_all(&mut self) -> bool {
        let any = !self.requests.is_empty();
        self.requests.clear();
        self.by_session.clear();
        any
    }
}

// ---- playback ---------------------------------------------------------------------

/// The media playlist entries (init map and segments) that stay on the Host.
pub fn hls_artifacts(playlist: &str, playlist_url: &Url, base: &Url) -> Vec<Url> {
    playlist
        .lines()
        .map(str::trim)
        .filter_map(|line| {
            if let Some(rest) = line.strip_prefix("#EXT-X-MAP:") {
                let start = rest.find("URI=")? + 4;
                let quoted = &rest[start..];
                let quote = quoted.chars().next().filter(|c| *c == '"' || *c == '\'')?;
                let end = quoted[1..].find(quote)?;
                Some(quoted[1..=end].to_owned())
            } else if !line.is_empty() && !line.starts_with('#') {
                Some(line.to_owned())
            } else {
                None
            }
        })
        .filter_map(|artifact| playlist_url.join(&artifact).ok())
        .filter(|url| same_origin(url, base))
        .collect()
}

pub fn same_origin(url: &Url, base: &Url) -> bool {
    url.scheme() == base.scheme() && url.host_str() == base.host_str() && url.port_or_known_default() == base.port_or_known_default()
}

/// How to play downloaded bytes.
#[derive(Debug, Clone, PartialEq)]
pub enum Playback {
    /// A container (mp3, mp4, wav...) the decoder recognises.
    Media { bytes: Vec<u8> },
    /// Headerless PCM as the clip's `audio_format` says.
    Pcm { bytes: Vec<u8>, sample_rate: u32, channels: u16, encoding: String },
}

#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// POST /clips/ack.
    Ack(Object),
    Download(Url),
    Play(Playback),
    /// Stop whatever the output is doing.
    StopOutput,
    PauseOutput,
    ResumeOutput,
    /// The clip left the shared journal (played, failed, muted, silenced).
    Finished(Object),
    /// `playing`/`paused`/availability may have changed.
    Changed,
    Error(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Output {
    #[default]
    Stopped,
    Playing,
    Paused,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Downloading {
    Source,
    Playlist,
    Artifact,
}

#[derive(Debug, Default)]
pub struct Player {
    base: Option<Url>,
    muted: bool,
    queue: VecDeque<Object>,
    current: Option<(Object, AudioClip)>,
    downloading: Option<Downloading>,
    current_source: Option<Url>,
    artifacts: VecDeque<Url>,
    media: Vec<u8>,
    play_started: bool,
    output: Output,
    /// "No audio backend" is reported once per run, not per clip.
    reported_missing_backend: bool,
}

fn ack(clip: &AudioClip, status: &str, error: &str) -> Effect {
    let mut body = Object::new();
    body.insert("clip_id".into(), Value::from(clip.clip_id));
    body.insert("url".into(), Value::from(clip.url.as_str()));
    body.insert("status".into(), Value::from(status));
    body.insert("trace_id".into(), Value::from(clip.trace_id.as_str()));
    if !error.is_empty() {
        body.insert("error".into(), Value::from(error));
    }
    Effect::Ack(body)
}

impl Player {
    pub fn muted(&self) -> bool {
        self.muted
    }
    pub fn output(&self) -> Output {
        self.output
    }
    pub fn has_current(&self) -> bool {
        self.current.is_some()
    }
    pub fn queued(&self) -> usize {
        self.queue.len()
    }

    /// The Host this plays from; `None` stops everything.
    pub fn set_base(&mut self, base: Option<Url>) -> Vec<Effect> {
        let base = base.map(|mut url| {
            if !url.path().ends_with('/') {
                let path = format!("{}/", url.path());
                url.set_path(&path);
            }
            url.set_query(None);
            url.set_fragment(None);
            url
        });
        let changed = base != self.base;
        self.base = base;
        if changed { self.reset() } else { Vec::new() }
    }

    /// Drops the queue and the clip in hand without acknowledging them:
    /// interrupted audio is never reported as played.
    pub fn reset(&mut self) -> Vec<Effect> {
        let had = self.current.is_some() || self.output != Output::Stopped;
        self.queue.clear();
        self.current = None;
        self.downloading = None;
        self.play_started = false;
        self.artifacts.clear();
        self.media.clear();
        self.output = Output::Stopped;
        if had { vec![Effect::StopOutput, Effect::Changed] } else { Vec::new() }
    }

    pub fn set_muted(&mut self, muted: bool) -> Vec<Effect> {
        if self.muted == muted {
            return Vec::new();
        }
        self.muted = muted;
        if muted { self.silence() } else { Vec::new() }
    }

    fn resolve(&self, path: &str) -> Option<Url> {
        let base = self.base.as_ref()?;
        match Url::parse(path) {
            Ok(url) => same_origin(&url, base).then_some(url),
            Err(_) => base.join(path.trim_start_matches('/')).ok(),
        }
    }

    /// A clip this window owns (C++ `enqueueOwned`).
    pub fn enqueue(&mut self, event: Object, recording: bool) -> Vec<Effect> {
        if self.muted {
            return vec![Effect::Finished(event)];
        }
        let mut effects = vec![ack(&AudioClip::from_json(&event), "queued", "")];
        self.queue.push_back(event);
        effects.extend(self.start_next(recording));
        effects
    }

    /// Starts the next clip unless one is playing, muted, or the microphone
    /// is in use (called again on a timer).
    pub fn start_next(&mut self, recording: bool) -> Vec<Effect> {
        if self.muted || self.downloading.is_some() || self.current.is_some() || recording {
            return Vec::new();
        }
        let Some(event) = self.queue.pop_front() else { return Vec::new() };
        let clip = AudioClip::from_json(&event);
        self.current = Some((event, clip.clone()));
        self.play_started = false;
        let mut effects = vec![Effect::Changed];
        if !clip.playlist_url.is_empty() && clip.complete_url.is_empty() {
            match self.resolve(&clip.playlist_url) {
                Some(url) => {
                    self.current_source = Some(url.clone());
                    self.downloading = Some(Downloading::Playlist);
                    effects.push(Effect::Download(url));
                }
                None => effects.extend(self.finish("play-fail", "playlist is outside the configured server")),
            }
            return effects;
        }
        let raw = clip.audio_format.get("container").and_then(Value::as_str) == Some("raw");
        let source = if !raw && !clip.complete_url.is_empty() {
            &clip.complete_url
        } else if !clip.stream_url.is_empty() {
            &clip.stream_url
        } else {
            &clip.url
        };
        match self.resolve(source) {
            Some(url) => {
                self.current_source = Some(url.clone());
                self.downloading = Some(Downloading::Source);
                effects.push(Effect::Download(url));
            }
            None => effects.extend(self.finish("play-fail", "audio source is outside the configured server")),
        }
        effects
    }

    /// A download for the current clip finished.
    pub fn downloaded(&mut self, result: Result<Vec<u8>, String>) -> Vec<Effect> {
        let Some(stage) = self.downloading.take() else { return Vec::new() };
        let bytes = match result {
            Ok(bytes) => bytes,
            Err(error) => {
                let mut effects = self.finish("play-fail", &error);
                effects.push(Effect::Error(error));
                return effects;
            }
        };
        match stage {
            Downloading::Playlist => {
                let (Some(source), Some(base)) = (self.current_source.clone(), self.base.clone()) else { return Vec::new() };
                self.artifacts = hls_artifacts(&String::from_utf8_lossy(&bytes), &source, &base).into();
                if self.artifacts.is_empty() {
                    return self.finish("play-fail", "playlist contained no playable media");
                }
                self.media.clear();
                self.next_artifact()
            }
            Downloading::Artifact => {
                self.media.extend_from_slice(&bytes);
                self.next_artifact()
            }
            Downloading::Source => {
                let Some((_, clip)) = &self.current else { return Vec::new() };
                if clip.audio_format.get("container").and_then(Value::as_str) == Some("raw") {
                    self.play_pcm(bytes)
                } else {
                    self.play_media(bytes)
                }
            }
        }
    }

    fn next_artifact(&mut self) -> Vec<Effect> {
        match self.artifacts.pop_front() {
            Some(url) => {
                self.downloading = Some(Downloading::Artifact);
                vec![Effect::Download(url)]
            }
            None => {
                let media = std::mem::take(&mut self.media);
                self.play_media(media)
            }
        }
    }

    fn play_media(&mut self, bytes: Vec<u8>) -> Vec<Effect> {
        if bytes.is_empty() {
            return self.finish("play-fail", "audio response was empty");
        }
        vec![Effect::Play(Playback::Media { bytes })]
    }

    fn play_pcm(&mut self, bytes: Vec<u8>) -> Vec<Effect> {
        let Some((_, clip)) = &self.current else { return Vec::new() };
        let format = &clip.audio_format;
        let encoding = format.get("encoding").and_then(Value::as_str).unwrap_or_default().to_owned();
        if !matches!(encoding.as_str(), "pcm_f32le" | "pcm_s16le" | "pcm_u8") {
            return self.finish("play-fail", &format!("unsupported raw PCM encoding: {encoding}"));
        }
        let sample_rate = format.get("sample_rate").and_then(Value::as_u64).unwrap_or(44_100) as u32;
        let channels = format.get("channels").and_then(Value::as_u64).unwrap_or(1) as u16;
        vec![Effect::Play(Playback::Pcm { bytes, sample_rate, channels, encoding })]
    }

    /// The output began playing (once per clip it is acknowledged).
    pub fn started(&mut self) -> Vec<Effect> {
        self.output = Output::Playing;
        let mut effects = vec![Effect::Changed];
        if !self.play_started
            && let Some((_, clip)) = &self.current
        {
            self.play_started = true;
            effects.push(ack(clip, "play-start", ""));
        }
        effects
    }

    /// The output finished the clip, or could not play it. `missing_backend`
    /// marks "there is no audio output at all", reported once per run.
    pub fn ended(&mut self, result: Result<(), String>, missing_backend: bool, recording: bool) -> Vec<Effect> {
        let mut effects = match &result {
            Ok(()) => self.finish("play-ok", ""),
            Err(error) => self.finish("play-fail", error),
        };
        if let Err(error) = result {
            if !missing_backend || !std::mem::replace(&mut self.reported_missing_backend, true) {
                effects.push(Effect::Error(error));
            }
        }
        effects.extend(self.start_next(recording));
        effects
    }

    fn finish(&mut self, status: &str, error: &str) -> Vec<Effect> {
        let Some((event, clip)) = self.current.take() else { return Vec::new() };
        self.downloading = None;
        self.play_started = false;
        self.artifacts.clear();
        self.media.clear();
        self.output = Output::Stopped;
        vec![Effect::Finished(event), Effect::StopOutput, ack(&clip, status, error), Effect::Changed]
    }

    /// Stop talking: the queue leaves the journal and the clip in hand is
    /// reported as interrupted.
    pub fn silence(&mut self) -> Vec<Effect> {
        let mut effects: Vec<Effect> = self.queue.drain(..).map(Effect::Finished).collect();
        effects.extend(self.finish("play-fail", "interrupted by user"));
        effects
    }

    pub fn pause(&mut self) -> Vec<Effect> {
        if self.output != Output::Playing {
            return Vec::new();
        }
        self.output = Output::Paused;
        vec![Effect::PauseOutput, Effect::Changed]
    }

    pub fn resume(&mut self) -> Vec<Effect> {
        if self.output != Output::Paused {
            return Vec::new();
        }
        self.output = Output::Playing;
        vec![Effect::ResumeOutput, Effect::Changed]
    }

    pub fn toggle(&mut self) -> Vec<Effect> {
        if self.output == Output::Paused { self.resume() } else { self.pause() }
    }
}
