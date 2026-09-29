//! AudioController (`app.audio`): plays announced voice clips and sends
//! dictations to `/transcribe` (C++ `AudioController`). The queue, sources
//! and acknowledgements are `clarp_core::audio::Player`; this object runs its
//! effects over its own Host client and an `AudioOutput`.

use std::collections::HashMap;
use std::pin::Pin;
use std::time::Duration;

use clarp_core::audio::{Effect, Output, PENDING_PLAYBACK_MS, PLAYBACK_RATE, Player, RecordingLease, Transcriptions};
use clarp_core::json::{self, Object};
use clarp_net::{ApiClient, ApiReply};
use cxx_qt::{CxxQtType, Threading};
use cxx_qt_lib::QString;
use serde_json::Value;
use url::Url;

use crate::audio_output::{self, AudioOutput, OutputEvent};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
    }

    extern "RustQt" {
        #[qobject]
        #[qproperty(bool, recording, READ = recording_value, NOTIFY = recording_changed)]
        #[qproperty(bool, transcribing, READ = transcribing_value, NOTIFY = transcribing_changed)]
        #[qproperty(i32, transcriptions_in_flight, cxx_name = "transcriptionsInFlight", READ = transcriptions_in_flight_value, NOTIFY = transcribing_changed)]
        #[qproperty(bool, playing, READ = playing_value, NOTIFY = playing_changed)]
        #[qproperty(bool, paused, READ = paused_value, NOTIFY = playing_changed)]
        type AudioController = super::AudioRust;
    }

    unsafe extern "RustQt" {
        fn recording_value(self: &AudioController) -> bool;
        fn transcribing_value(self: &AudioController) -> bool;
        fn transcriptions_in_flight_value(self: &AudioController) -> i32;
        fn playing_value(self: &AudioController) -> bool;
        fn paused_value(self: &AudioController) -> bool;

        #[qsignal]
        #[cxx_name = "mutedChanged"]
        fn muted_changed(self: Pin<&mut AudioController>, muted: bool);
        #[qsignal]
        #[cxx_name = "recordingChanged"]
        fn recording_changed(self: Pin<&mut AudioController>);
        #[qsignal]
        #[cxx_name = "transcribingChanged"]
        fn transcribing_changed(self: Pin<&mut AudioController>);
        #[qsignal]
        #[cxx_name = "playingChanged"]
        fn playing_changed(self: Pin<&mut AudioController>);
        #[qsignal]
        #[cxx_name = "transcriptionReady"]
        fn transcription_ready(
            self: Pin<&mut AudioController>,
            text: &QString,
            trace_id: &QString,
            transcription_id: &QString,
            hands_free: bool,
            target_session: &QString,
        );
        #[qsignal]
        #[cxx_name = "mediaError"]
        fn media_error(self: Pin<&mut AudioController>, message: &QString);

        #[qinvokable]
        #[cxx_name = "transcriptionsForSession"]
        fn transcriptions_for_session(self: &AudioController, session: &QString) -> i32;
        #[qinvokable]
        #[cxx_name = "toggleRecording"]
        fn toggle_recording(self: Pin<&mut AudioController>);
        #[qinvokable]
        #[cxx_name = "toggleRecordingForSession"]
        fn toggle_recording_for_session(self: Pin<&mut AudioController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "startRecording"]
        fn start_recording(self: Pin<&mut AudioController>);
        #[qinvokable]
        #[cxx_name = "stopRecording"]
        fn stop_recording(self: Pin<&mut AudioController>);
        #[qinvokable]
        #[cxx_name = "cancelRecording"]
        fn cancel_recording(self: Pin<&mut AudioController>);
        #[qinvokable]
        #[cxx_name = "cancelTranscriptionsForSession"]
        fn cancel_transcriptions_for_session(self: Pin<&mut AudioController>, session: &QString);
        #[qinvokable]
        fn silence(self: Pin<&mut AudioController>);
    }

    impl cxx_qt::Threading for AudioController {}
    impl cxx_qt::Initialize for AudioController {}

    #[namespace = "rust::cxxqtlib1"]
    unsafe extern "C++" {
        include!("cxx-qt-lib/common.h");
        /// Owned by the AppController.
        #[rust_name = "new_audio_controller"]
        fn make_unique() -> UniquePtr<AudioController>;
    }
}

pub struct AudioRust {
    player: Player,
    output: Box<dyn AudioOutput>,
    /// Bumped whenever the output is stopped, so a stopped clip's late
    /// events are ignored.
    output_generation: u64,
    api: Option<ApiClient>,
    base: Option<Url>,
    token: String,
    /// The download the player waits for.
    download_tag: String,
    next_tag: u64,
    transcriptions: Transcriptions,
    /// request tag -> transcription id sent with it
    transcription_ids: HashMap<String, String>,
    lease: RecordingLease,
    capture: Option<crate::audio_input::Recording>,
    recording: bool,
    recording_session: String,
}

impl Default for AudioRust {
    fn default() -> Self {
        Self {
            player: Player::default(),
            output: audio_output::select(),
            output_generation: 0,
            api: None,
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
        }
    }
}

impl Drop for AudioRust {
    fn drop(&mut self) {
        self.output.stop();
        if let Some(capture) = self.capture.take() {
            capture.cancel();
        }
        self.lease.release();
    }
}

impl cxx_qt::Initialize for qobject::AudioController {
    fn initialize(mut self: Pin<&mut Self>) {
        let qt = self.qt_thread();
        let api = ApiClient::new(crate::runtime::handle(), move |reply| {
            if qt.queue(move |audio| audio.handle_reply(reply)).is_err() {
                eprintln!("AudioController: dropped a reply; the controller is gone");
            }
        });
        self.as_mut().rust_mut().api = Some(api);
        self.pending_playback_after();
    }
}

impl qobject::AudioController {
    fn recording_value(&self) -> bool {
        self.recording
    }
    fn transcribing_value(&self) -> bool {
        self.transcriptions.in_flight() > 0
    }
    fn transcriptions_in_flight_value(&self) -> i32 {
        self.transcriptions.in_flight()
    }
    fn playing_value(&self) -> bool {
        self.player.output() == Output::Playing
    }
    fn paused_value(&self) -> bool {
        self.player.output() == Output::Paused
    }
    pub fn recording_pub(&self) -> bool {
        self.recording
    }
    fn transcriptions_for_session(&self, session: &QString) -> i32 {
        self.transcriptions.for_session(&session.to_string())
    }

    /// Queued clips retry on a timer: a recording may have ended meanwhile.
    fn pending_playback_after(self: Pin<&mut Self>) {
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_millis(PENDING_PLAYBACK_MS), move || {
            let queued = qt.queue(|mut audio| {
                let busy = audio.lease.busy();
                let effects = audio.as_mut().rust_mut().player.start_next(busy);
                audio.as_mut().apply(effects);
                audio.pending_playback_after();
            });
            if queued.is_err() {
                eprintln!("AudioController: playback timer stopped; the controller is gone");
            }
        });
    }

    fn tag(mut self: Pin<&mut Self>, kind: &str) -> String {
        let mut rust = self.as_mut().rust_mut();
        rust.next_tag += 1;
        format!("{kind}:{}", rust.next_tag)
    }

    /// The Host this plays from and transcribes with; an empty URL stops
    /// everything. A different Host or account drops pending dictations.
    pub fn set_endpoint(mut self: Pin<&mut Self>, base: Option<Url>, token: &str) {
        if self.base != base || self.token != token {
            self.as_mut().cancel_recording();
            if self.as_mut().rust_mut().transcriptions.cancel_all() {
                self.as_mut().rust_mut().transcription_ids.clear();
                self.as_mut().transcribing_changed();
            }
            let effects = self.as_mut().rust_mut().player.reset();
            self.as_mut().apply(effects);
        }
        let effects = self.as_mut().rust_mut().player.set_base(base.clone());
        self.as_mut().apply(effects);
        if let (Some(api), Some(url)) = (self.api.as_ref(), base.clone()) {
            api.set_endpoint(url, token);
        }
        let mut rust = self.as_mut().rust_mut();
        rust.base = base;
        rust.token = token.to_owned();
    }

    /// Without a coordinator (the D-Bus one lands later), mute is local.
    pub fn set_muted(mut self: Pin<&mut Self>, muted: bool) {
        let changed = self.player.muted() != muted;
        let effects = self.as_mut().rust_mut().player.set_muted(muted);
        self.as_mut().apply(effects);
        if changed {
            self.muted_changed(muted);
        }
    }

    pub fn enqueue_clip(mut self: Pin<&mut Self>, event: Object) {
        let clip = clarp_core::protocol::AudioClip::from_json(&event);
        if clip.preferred_source().is_empty() {
            return;
        }
        let busy = self.lease.busy();
        let effects = self.as_mut().rust_mut().player.enqueue(event, busy);
        self.apply(effects);
    }

    fn apply(mut self: Pin<&mut Self>, effects: Vec<Effect>) {
        for effect in effects {
            match effect {
                Effect::Ack(body) => {
                    let tag = self.as_mut().tag("clip-ack");
                    if let Some(api) = self.api.as_ref() {
                        api.post_json(&tag, "/clips/ack", Value::Object(body), None);
                    }
                }
                Effect::Download(url) => {
                    let tag = self.as_mut().tag("clip-download");
                    self.as_mut().rust_mut().download_tag = tag.clone();
                    if let Some(api) = self.api.as_ref() {
                        api.get_bytes(&tag, url.as_str());
                    }
                }
                Effect::Play(playback) => {
                    let generation = self.output_generation;
                    let qt = self.qt_thread();
                    let events = std::sync::Arc::new(move |event: OutputEvent| {
                        let queued = qt.queue(move |audio| audio.output_event(generation, event));
                        if queued.is_err() {
                            eprintln!("AudioController: dropped a playback event; the controller is gone");
                        }
                    });
                    self.as_mut().rust_mut().output.play(playback, PLAYBACK_RATE, events);
                }
                Effect::StopOutput => {
                    let mut rust = self.as_mut().rust_mut();
                    rust.output_generation += 1;
                    rust.download_tag.clear();
                    rust.output.stop();
                }
                Effect::PauseOutput => self.as_mut().rust_mut().output.pause(),
                Effect::ResumeOutput => self.as_mut().rust_mut().output.resume(),
                // The shared journal arrives with the coordinator.
                Effect::Finished(_) => {}
                Effect::Changed => self.as_mut().playing_changed(),
                Effect::Error(message) => self.as_mut().media_error(&QString::from(message.as_str())),
            }
        }
    }

    fn output_event(mut self: Pin<&mut Self>, generation: u64, event: OutputEvent) {
        if generation != self.output_generation {
            return;
        }
        let busy = self.lease.busy();
        let effects = match event {
            OutputEvent::Started => self.as_mut().rust_mut().player.started(),
            OutputEvent::Ended { result, missing_backend } => {
                self.as_mut().rust_mut().output_generation += 1;
                self.as_mut().rust_mut().player.ended(result, missing_backend, busy)
            }
        };
        self.apply(effects);
    }

    fn handle_reply(mut self: Pin<&mut Self>, reply: ApiReply) {
        match reply {
            ApiReply::Bytes { tag, bytes, .. } if tag == self.download_tag => {
                self.as_mut().rust_mut().download_tag.clear();
                let effects = self.as_mut().rust_mut().player.downloaded(Ok(bytes));
                self.apply(effects);
            }
            ApiReply::Failed { tag, message, status } if tag == self.download_tag => {
                self.as_mut().rust_mut().download_tag.clear();
                let error = if status > 0 { format!("{message} (HTTP {status})") } else { message };
                let effects = self.as_mut().rust_mut().player.downloaded(Err(error));
                self.apply(effects);
            }
            ApiReply::Json { tag, object } if tag.starts_with("transcribe:") => self.transcribed(&tag, Ok(object)),
            ApiReply::Failed { tag, message, status } if tag.starts_with("transcribe:") => {
                let error = if status > 0 { format!("{message} (HTTP {status})") } else { message };
                self.transcribed(&tag, Err(error));
            }
            ApiReply::Failed { tag, message, .. } if tag.starts_with("clip-ack:") => {
                eprintln!("AudioController: {tag} failed: {message}");
            }
            // Acks, and downloads of clips that were stopped meanwhile.
            _ => {}
        }
    }

    // ---- dictation -----------------------------------------------------------

    pub fn transcribe_recording(mut self: Pin<&mut Self>, wav: Vec<u8>, target_session: &str) {
        let tag = format!("transcribe:{}", uuid::Uuid::new_v4());
        let id = uuid::Uuid::new_v4().to_string();
        {
            let mut rust = self.as_mut().rust_mut();
            rust.transcriptions.start(&tag, target_session);
            rust.transcription_ids.insert(tag.clone(), id.clone());
        }
        self.as_mut().transcribing_changed();
        if let Some(api) = self.api.as_ref() {
            api.post_bytes(&tag, "/transcribe", wav, "audio/wav", &[("X-Hands-Free", "0"), ("X-Transcription-ID", &id)]);
        }
    }

    fn transcribed(mut self: Pin<&mut Self>, tag: &str, result: Result<Object, String>) {
        let id = self.as_mut().rust_mut().transcription_ids.remove(tag).unwrap_or_default();
        // None: cancelled (for its chat, or by a Host switch); drop it.
        let Some(session) = self.as_mut().rust_mut().transcriptions.finish(tag) else { return };
        self.as_mut().transcribing_changed();
        match result {
            Err(error) => self.media_error(&QString::from(error.as_str())),
            Ok(object) => {
                let text = json::string(&object, "text").trim().to_owned();
                if text.is_empty() {
                    return;
                }
                let returned = json::string(&object, "transcription_id");
                let transcription_id = if returned.is_empty() { id } else { returned };
                let hands_free = object.get("hands_free").and_then(Value::as_bool).unwrap_or(false);
                self.transcription_ready(
                    &QString::from(text.as_str()),
                    &QString::from(json::string(&object, "trace_id").as_str()),
                    &QString::from(transcription_id.as_str()),
                    hands_free,
                    &QString::from(session.as_str()),
                );
            }
        }
    }

    fn cancel_transcriptions_for_session(mut self: Pin<&mut Self>, session: &QString) {
        if self.as_mut().rust_mut().transcriptions.cancel_session(&session.to_string()) {
            self.transcribing_changed();
        }
    }

    // ---- recording -----------------------------------------------------------

    fn toggle_recording(self: Pin<&mut Self>) {
        if self.recording { self.stop_recording() } else { self.start_recording() }
    }

    pub fn toggle_recording_for_session(mut self: Pin<&mut Self>, session: &QString) {
        if self.recording {
            self.stop_recording();
            return;
        }
        self.as_mut().rust_mut().recording_session = session.to_string();
        self.start_recording();
    }

    fn start_recording(mut self: Pin<&mut Self>) {
        if self.recording || std::env::var_os("CLARP_SCREENSHOT_PATH").is_some() {
            return;
        }
        let session = self.recording_session.clone();
        if !self.as_mut().rust_mut().lease.acquire(&session) {
            self.as_mut().rust_mut().recording_session.clear();
            self.media_error(&QString::from("Another Clarp window is recording. Stop that recording before starting here."));
            return;
        }
        self.as_mut().silence();
        match crate::audio_input::start() {
            Ok(capture) => {
                let mut rust = self.as_mut().rust_mut();
                rust.capture = Some(capture);
                rust.recording = true;
                self.recording_changed();
            }
            Err(message) => {
                self.as_mut().cancel_recording();
                self.media_error(&QString::from(message.as_str()));
            }
        }
    }

    /// Stops recording and sends the audio to the chat it was recorded for.
    fn stop_recording(mut self: Pin<&mut Self>) {
        let Some(capture) = self.as_mut().rust_mut().capture.take().filter(|_| self.recording) else { return };
        let (pcm, format) = capture.finish();
        let target = {
            let mut rust = self.as_mut().rust_mut();
            rust.recording = false;
            rust.recording_session.clear();
            rust.lease.release()
        };
        self.as_mut().recording_changed();
        let wav = clarp_core::audio::encode_wav(&pcm, format);
        if wav.len() <= clarp_core::audio::MIN_WAV_BYTES {
            self.media_error(&QString::from("Recording was too short"));
            return;
        }
        self.transcribe_recording(wav, &target);
    }

    fn cancel_recording(mut self: Pin<&mut Self>) {
        let was = self.recording;
        {
            let mut rust = self.as_mut().rust_mut();
            if let Some(capture) = rust.capture.take() {
                capture.cancel();
            }
            rust.lease.release();
            rust.recording_session.clear();
            rust.recording = false;
        }
        if was {
            self.recording_changed();
        }
    }

    pub fn silence(mut self: Pin<&mut Self>) {
        let effects = self.as_mut().rust_mut().player.silence();
        self.apply(effects);
    }
}
