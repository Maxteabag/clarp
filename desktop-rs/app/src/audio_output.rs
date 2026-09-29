//! Where voice clips are played. `CLARP_AUDIO_OUTPUT=null` selects a silent
//! sink that "plays" every clip for a moment, so probes exercise the whole
//! pipeline without touching the user's speakers. Without a playback backend
//! every clip fails at once, like the C++ client without a Qt Multimedia
//! backend, instead of one stuck clip blocking the queue.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use clarp_core::audio::Playback;

/// What the output reports back, in order.
#[derive(Debug, Clone, PartialEq)]
pub enum OutputEvent {
    Started,
    /// `missing_backend` marks "nothing can play here", reported once.
    Ended { result: Result<(), String>, missing_backend: bool },
}

pub type Sink = Arc<dyn Fn(OutputEvent) + Send + Sync>;

pub trait AudioOutput: Send {
    fn play(&mut self, playback: Playback, rate: f64, events: Sink);
    fn stop(&mut self);
    fn pause(&mut self);
    fn resume(&mut self);
}

pub const MISSING_BACKEND: &str = "No audio backend is available; this build cannot play voice clips yet";

pub fn select() -> Box<dyn AudioOutput> {
    match std::env::var("CLARP_AUDIO_OUTPUT").as_deref() {
        Ok("null") => Box::new(Silent::default()),
        _ => Box::new(Unavailable),
    }
}

pub struct Unavailable;

impl AudioOutput for Unavailable {
    fn play(&mut self, _playback: Playback, _rate: f64, events: Sink) {
        events(OutputEvent::Ended { result: Err(MISSING_BACKEND.into()), missing_backend: true });
    }
    fn stop(&mut self) {}
    fn pause(&mut self) {}
    fn resume(&mut self) {}
}

/// Plays nothing, for tests: starts at once and ends after a short while
/// unless stopped; pausing holds the end.
#[derive(Default)]
pub struct Silent {
    generation: Arc<AtomicU64>,
    paused: Arc<std::sync::atomic::AtomicBool>,
}

pub const SILENT_CLIP_MS: u64 = 150;

impl AudioOutput for Silent {
    fn play(&mut self, _playback: Playback, _rate: f64, events: Sink) {
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        self.paused.store(false, Ordering::SeqCst);
        events(OutputEvent::Started);
        let (current, paused) = (self.generation.clone(), self.paused.clone());
        fn wait(current: Arc<AtomicU64>, paused: Arc<std::sync::atomic::AtomicBool>, generation: u64, events: Sink) {
            crate::runtime::after(Duration::from_millis(SILENT_CLIP_MS), move || {
                if current.load(Ordering::SeqCst) != generation {
                    return;
                }
                if paused.load(Ordering::SeqCst) {
                    wait(current, paused, generation, events);
                } else {
                    events(OutputEvent::Ended { result: Ok(()), missing_backend: false });
                }
            });
        }
        wait(current, paused, generation, events);
    }
    fn stop(&mut self) {
        self.generation.fetch_add(1, Ordering::SeqCst);
    }
    fn pause(&mut self) {
        self.paused.store(true, Ordering::SeqCst);
    }
    fn resume(&mut self) {
        self.paused.store(false, Ordering::SeqCst);
    }
}
