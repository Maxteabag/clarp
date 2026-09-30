//! Voice replies and dictation (the Qt app's `AudioController`): plays
//! announced clips and sends recordings to `/transcribe`. The queue, sources
//! and acknowledgements are `clarp_core::audio::Player`; this runs its
//! effects over its own Host client and an `AudioOutput`, on the UI thread.
//! What the window must act on comes out as `Notice`s.

use std::cell::RefCell;
use std::collections::HashMap;
use std::time::Duration;

use clarp_core::audio::{Effect, Output, PENDING_PLAYBACK_MS, PLAYBACK_RATE, Player, RecordingLease, Transcriptions};
use clarp_core::json::{self, Object};
use clarp_net::{ApiClient, ApiReply};
use serde_json::Value;
use url::Url;

use super::audio_coordinator::{self, Coordinator};
use super::audio_output::{self, AudioOutput, OutputEvent};

/// What the audio asks of the window.
#[derive(Debug, Clone, PartialEq)]
pub enum Notice {
    /// The shared journal (another window, or MPRIS) changed mute.
    Muted(bool),
    Error(String),
    /// A dictation came back as text for `target` (empty: the selection).
    Transcribed { text: String, trace: String, transcription: String, hands_free: bool, target: String },
    /// Recording, transcribing or playback changed: redraw and publish.
    Changed,
}

pub struct Audio {
    player: Player,
    /// Elects one player across windows.
    coordinator: Option<Coordinator>,
    /// The owner's state, shown while another window plays.
    remote: clarp_core::audio_journal::State,
    output: Box<dyn AudioOutput>,
    /// Bumped whenever the output is stopped, so a stopped clip's late
    /// events are ignored.
    output_generation: u64,
    api: ApiClient,
    base: Option<Url>,
    token: String,
    /// The download the player waits for.
    download_tag: String,
    next_tag: u64,
    transcriptions: Transcriptions,
    /// request tag -> transcription id sent with it
    transcription_ids: HashMap<String, String>,
    lease: RecordingLease,
    capture: Option<super::audio_input::Recording>,
    recording: bool,
    recording_session: String,
    notices: Vec<Notice>,
}

thread_local! {
    static AUDIO: RefCell<Option<Audio>> = const { RefCell::new(None) };
}

/// Runs `act` on the audio, then hands its notices to the window.
pub fn with<R>(act: impl FnOnce(&mut Audio) -> R) -> Option<R> {
    let (result, notices) = AUDIO.with(|audio| {
        let mut audio = audio.borrow_mut();
        let audio = audio.as_mut()?;
        let result = act(audio);
        Some((result, std::mem::take(&mut audio.notices)))
    })?;
    if !notices.is_empty() {
        crate::audio_notices(notices);
    }
    Some(result)
}

/// Queues `act` onto the UI thread (from the runtime or a device thread).
fn later(act: impl FnOnce(&mut Audio) + Send + 'static) {
    if let Err(error) = slint::invoke_from_event_loop(move || {
        with(act);
    }) {
        eprintln!("clarp-slint audio: dropped an event: {error}");
    }
}

/// Creates the audio for this window (once, on the UI thread).
pub fn start(muted: bool) {
    let api = ApiClient::new(super::runtime::handle(), move |reply| later(move |audio| audio.handle_reply(reply)));
    let coordinator = Coordinator::new(std::sync::Arc::new(move |event| later(move |audio| audio.coordinator_event(event))));
    let mut audio = Audio {
        player: Player::default(),
        coordinator: Some(coordinator),
        remote: clarp_core::audio_journal::State::default(),
        output: audio_output::select(),
        output_generation: 0,
        api,
        base: None,
        token: String::new(),
        download_tag: String::new(),
        next_tag: 0,
        transcriptions: Transcriptions::default(),
        transcription_ids: HashMap::new(),
        lease: RecordingLease::new(None),
        capture: None,
        recording: false,
        recording_session: String::new(),
        notices: Vec::new(),
    };
    audio.set_muted(muted);
    AUDIO.with(|slot| *slot.borrow_mut() = Some(audio));
    pending_playback_after();
}

/// Stops everything as the window closes.
pub fn stop() {
    AUDIO.with(|slot| {
        if let Some(mut audio) = slot.borrow_mut().take() {
            if let Some(coordinator) = audio.coordinator.take() {
                coordinator.stop();
            }
            audio.output.stop();
            if let Some(capture) = audio.capture.take() {
                capture.cancel();
            }
            audio.lease.release();
        }
    });
}

/// Queued clips retry on a timer: a recording may have ended meanwhile.
fn pending_playback_after() {
    super::runtime::after(Duration::from_millis(PENDING_PLAYBACK_MS), || {
        later(|audio| {
            let hold = audio.hold();
            let coordinator = audio.coordinator.clone();
            let mut begin = |event: &Object| coordinator.as_ref().is_some_and(|c| c.begin(event));
            let effects = audio.player.start_next(hold, &mut begin);
            audio.apply(effects);
            pending_playback_after();
        });
    });
}

impl Audio {
    pub fn recording(&self) -> bool {
        self.recording
    }
    /// Dictations still being transcribed, for any chat.
    pub fn transcriptions_in_flight(&self) -> i32 {
        self.transcriptions.in_flight()
    }

    /// The chat the recording in progress is for.
    pub fn recording_target(&self) -> String {
        self.recording_session.clone()
    }
    pub fn transcriptions_for_session(&self, session: &str) -> i32 {
        self.transcriptions.for_session(session)
    }
    /// Another window owns playback: show what it plays.
    fn remote_view(&self) -> bool {
        self.coordinator.as_ref().is_some_and(|c| c.configured() && !c.owner())
    }
    pub fn playing(&self) -> bool {
        if self.remote_view() { self.remote.playing } else { self.player.output() == Output::Playing }
    }
    pub fn paused(&self) -> bool {
        if self.remote_view() { self.remote.paused } else { self.player.output() == Output::Paused }
    }
    /// Playing, paused, and whether there is a clip to control, as MPRIS
    /// shows them.
    pub fn playback_state(&self) -> super::mpris::Status {
        let available = if self.remote_view() { self.remote.available } else { self.player.has_current() };
        super::mpris::Status { playing: self.playing(), paused: self.paused(), available }
    }

    /// Clips wait while the microphone is in use or another window plays.
    fn hold(&self) -> bool {
        self.lease.busy() || !self.coordinator.as_ref().is_some_and(Coordinator::owner)
    }

    fn tag(&mut self, kind: &str) -> String {
        self.next_tag += 1;
        format!("{kind}:{}", self.next_tag)
    }

    /// The Host this plays from and transcribes with; None stops everything.
    /// A different Host or account drops pending dictations.
    pub fn set_endpoint(&mut self, base: Option<Url>, token: &str) {
        if self.base != base || self.token != token {
            self.cancel_recording();
            if self.transcriptions.cancel_all() {
                self.transcription_ids.clear();
                self.notices.push(Notice::Changed);
            }
            let effects = self.player.reset();
            self.apply(effects);
        }
        let effects = self.player.set_base(base.clone());
        self.apply(effects);
        if let Some(url) = base.clone() {
            self.api.set_endpoint(url, token);
        }
        let scope = base.as_ref().filter(|_| !token.is_empty()).map(|url| {
            let mut url = url.clone();
            if !url.path().ends_with('/') {
                let path = format!("{}/", url.path());
                url.set_path(&path);
            }
            url.set_query(None);
            url.set_fragment(None);
            format!("{url}\0{token}")
        });
        let muted = self.player.muted();
        if let Some(coordinator) = self.coordinator.as_ref() {
            match scope {
                Some(scope) => coordinator.configure(&scope, muted),
                None => coordinator.stop(),
            }
        }
        self.base = base;
        self.token = token.to_owned();
    }

    /// Mute belongs to the shared journal once coordinated.
    pub fn set_muted(&mut self, muted: bool) {
        if let Some(coordinator) = self.coordinator.as_ref().filter(|c| c.configured()) {
            coordinator.command("mute", muted);
            return;
        }
        let changed = self.player.muted() != muted;
        let effects = self.player.set_muted(muted);
        self.apply(effects);
        if changed {
            self.notices.push(Notice::Muted(muted));
        }
    }

    pub fn enqueue_clip(&mut self, event: Object) {
        let clip = clarp_core::protocol::AudioClip::from_json(&event);
        if clip.preferred_source().is_empty() {
            return;
        }
        if let Some(coordinator) = self.coordinator.as_ref() {
            coordinator.submit(event);
        }
    }

    /// A clip the journal gave this window, as owner.
    fn enqueue_owned(&mut self, event: Object) {
        if !self.coordinator.as_ref().is_some_and(Coordinator::owner) {
            return;
        }
        let hold = self.hold();
        let coordinator = self.coordinator.clone();
        let mut begin = |event: &Object| coordinator.as_ref().is_some_and(|c| c.begin(event));
        let effects = self.player.enqueue(event, hold, &mut begin);
        self.apply(effects);
    }

    fn coordinator_event(&mut self, event: audio_coordinator::Event) {
        use audio_coordinator::Event;
        match event {
            Event::ClipReady(event) => self.enqueue_owned(event),
            Event::Command(action) => {
                let effects = self.player_command(&action);
                self.apply(effects);
            }
            Event::State(state) => {
                if self.player.muted() != state.muted {
                    let owner = self.coordinator.as_ref().is_some_and(Coordinator::owner);
                    let effects = self.player.set_muted(state.muted);
                    if owner {
                        self.apply(effects);
                    }
                    self.notices.push(Notice::Muted(state.muted));
                }
                let owner = self.coordinator.as_ref().is_some_and(Coordinator::owner);
                let shown = clarp_core::audio_journal::State { muted: false, ..state };
                if !owner && self.remote != shown {
                    self.remote = shown;
                    self.notices.push(Notice::Changed);
                }
            }
            Event::Ownership(owner) => {
                if !owner {
                    let effects = self.player.reset();
                    self.apply(effects);
                    self.remote = clarp_core::audio_journal::State::default();
                }
                self.notices.push(Notice::Changed);
            }
            Event::Error(message) => self.notices.push(Notice::Error(message)),
        }
    }

    fn player_command(&mut self, action: &str) -> Vec<Effect> {
        match action {
            "stop" => self.player.silence(),
            "pause" => self.player.pause(),
            "resume" => self.player.resume(),
            "toggle" => self.player.toggle(),
            other => {
                eprintln!("clarp-slint audio: unknown playback command {other}");
                Vec::new()
            }
        }
    }

    fn apply(&mut self, effects: Vec<Effect>) {
        for effect in effects {
            match effect {
                Effect::Ack(body) => {
                    let tag = self.tag("clip-ack");
                    self.api.post_json(&tag, "/clips/ack", Value::Object(body), None);
                }
                Effect::Download(url) => {
                    let tag = self.tag("clip-download");
                    self.download_tag = tag.clone();
                    self.api.get_bytes(&tag, url.as_str());
                }
                Effect::Play(playback) => {
                    let generation = self.output_generation;
                    let events = std::sync::Arc::new(move |event: OutputEvent| later(move |audio| audio.output_event(generation, event)));
                    self.output.play(playback, PLAYBACK_RATE, events);
                }
                Effect::StopOutput => {
                    self.output_generation += 1;
                    self.download_tag.clear();
                    self.output.stop();
                }
                Effect::PauseOutput => self.output.pause(),
                Effect::ResumeOutput => self.output.resume(),
                Effect::Finished(event) => {
                    if let Some(coordinator) = self.coordinator.as_ref() {
                        coordinator.finish(&event);
                    }
                }
                Effect::Changed => {
                    let (playing, paused, available) = (self.playing(), self.paused(), self.player.has_current());
                    if let Some(coordinator) = self.coordinator.as_ref() {
                        coordinator.publish(playing, paused, available);
                    }
                    self.notices.push(Notice::Changed);
                }
                Effect::Error(message) => self.notices.push(Notice::Error(message)),
            }
        }
    }

    fn output_event(&mut self, generation: u64, event: OutputEvent) {
        if generation != self.output_generation {
            return;
        }
        let hold = self.hold();
        let coordinator = self.coordinator.clone();
        let mut begin = |event: &Object| coordinator.as_ref().is_some_and(|c| c.begin(event));
        let effects = match event {
            OutputEvent::Started => self.player.started(),
            OutputEvent::Ended { result, missing_backend } => {
                self.output_generation += 1;
                self.player.ended(result, missing_backend, hold, &mut begin)
            }
        };
        self.apply(effects);
    }

    fn handle_reply(&mut self, reply: ApiReply) {
        match reply {
            ApiReply::Bytes { tag, bytes, .. } if tag == self.download_tag => {
                self.download_tag.clear();
                let effects = self.player.downloaded(Ok(bytes));
                self.apply(effects);
            }
            ApiReply::Failed { tag, message, status } if tag == self.download_tag => {
                self.download_tag.clear();
                let error = if status > 0 { format!("{message} (HTTP {status})") } else { message };
                let effects = self.player.downloaded(Err(error));
                self.apply(effects);
            }
            ApiReply::Json { tag, object } if tag.starts_with("transcribe:") => self.transcribed(&tag, Ok(object)),
            ApiReply::Failed { tag, message, status } if tag.starts_with("transcribe:") => {
                let error = if status > 0 { format!("{message} (HTTP {status})") } else { message };
                self.transcribed(&tag, Err(error));
            }
            ApiReply::Failed { tag, message, .. } if tag.starts_with("clip-ack:") => {
                eprintln!("clarp-slint audio: {tag} failed: {message}");
            }
            // Acks, and downloads of clips that were stopped meanwhile.
            _ => {}
        }
    }

    // ---- dictation -----------------------------------------------------------

    fn transcribe_recording(&mut self, wav: Vec<u8>, target_session: &str) {
        let tag = format!("transcribe:{}", uuid::Uuid::new_v4());
        let id = uuid::Uuid::new_v4().to_string();
        self.transcriptions.start(&tag, target_session);
        self.transcription_ids.insert(tag.clone(), id.clone());
        self.notices.push(Notice::Changed);
        self.api.post_bytes(&tag, "/transcribe", wav, "audio/wav", &[("X-Hands-Free", "0"), ("X-Transcription-ID", &id)]);
    }

    fn transcribed(&mut self, tag: &str, result: Result<Object, String>) {
        let id = self.transcription_ids.remove(tag).unwrap_or_default();
        // None: cancelled (for its chat, or by a Host switch); drop it.
        let Some(session) = self.transcriptions.finish(tag) else { return };
        self.notices.push(Notice::Changed);
        match result {
            Err(error) => self.notices.push(Notice::Error(error)),
            Ok(object) => {
                let text = json::string(&object, "text").trim().to_owned();
                if text.is_empty() {
                    return;
                }
                let returned = json::string(&object, "transcription_id");
                self.notices.push(Notice::Transcribed {
                    text,
                    trace: json::string(&object, "trace_id"),
                    transcription: if returned.is_empty() { id } else { returned },
                    hands_free: object.get("hands_free").and_then(Value::as_bool).unwrap_or(false),
                    target: session,
                });
            }
        }
    }

    pub fn cancel_transcriptions_for_session(&mut self, session: &str) {
        if self.transcriptions.cancel_session(session) {
            self.notices.push(Notice::Changed);
        }
    }

    // ---- recording -----------------------------------------------------------

    /// Talk (Ctrl+Shift+Space): starts recording for `session`, or stops
    /// and sends a recording in progress.
    pub fn toggle_recording_for_session(&mut self, session: &str) {
        if self.recording {
            self.stop_recording();
            return;
        }
        if session.is_empty() {
            return;
        }
        self.recording_session = session.to_owned();
        self.start_recording();
    }

    fn start_recording(&mut self) {
        if self.recording {
            return;
        }
        let session = self.recording_session.clone();
        if !self.lease.acquire(&session) {
            self.recording_session.clear();
            self.notices.push(Notice::Error("Another Clarp window is recording. Stop that recording before starting here.".into()));
            return;
        }
        self.silence();
        match super::audio_input::start() {
            Ok(capture) => {
                self.capture = Some(capture);
                self.recording = true;
                self.notices.push(Notice::Changed);
            }
            Err(message) => {
                self.cancel_recording();
                self.notices.push(Notice::Error(message));
            }
        }
    }

    /// Stops recording and sends the audio to the chat it was recorded for.
    fn stop_recording(&mut self) {
        if !self.recording {
            return;
        }
        let Some(capture) = self.capture.take() else { return };
        let (pcm, format) = capture.finish();
        self.recording = false;
        self.recording_session.clear();
        let target = self.lease.release();
        self.notices.push(Notice::Changed);
        let wav = clarp_core::audio::encode_wav(&pcm, format);
        if wav.len() <= clarp_core::audio::MIN_WAV_BYTES {
            self.notices.push(Notice::Error("Recording was too short".into()));
            return;
        }
        self.transcribe_recording(wav, &target);
    }

    pub fn cancel_recording(&mut self) {
        let was = self.recording;
        if let Some(capture) = self.capture.take() {
            capture.cancel();
        }
        self.lease.release();
        self.recording_session.clear();
        self.recording = false;
        if was {
            self.notices.push(Notice::Changed);
        }
    }

    /// Stops speech in whichever window plays it.
    pub fn silence(&mut self) {
        self.playback_command("stop");
    }

    /// "stop", "pause", "resume" or "toggle", in whichever window plays.
    pub fn playback_command(&mut self, action: &str) {
        if let Some(coordinator) = self.coordinator.as_ref().filter(|c| c.configured()) {
            coordinator.command(action, false);
            return;
        }
        let effects = self.player_command(action);
        self.apply(effects);
    }
}
