//! Where voice clips are played: the default output device through rodio
//! (mp3, mp4/aac, wav, flac, ogg and raw PCM). For tests,
//! `CLARP_AUDIO_OUTPUT=null` "plays" every clip silently for a moment and
//! `CLARP_AUDIO_OUTPUT=decode` really decodes each clip but never opens a
//! device, so probes never touch the user's speakers. When no output device
//! opens, every clip fails at once, like the C++ client without a Qt
//! Multimedia backend, instead of one stuck clip blocking the queue.

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

pub const MISSING_BACKEND: &str = "No audio output device is available";

pub fn select() -> Box<dyn AudioOutput> {
    match std::env::var("CLARP_AUDIO_OUTPUT").as_deref() {
        Ok("null") => Box::new(Silent::default()),
        Ok("none") => Box::new(Unavailable),
        Ok("decode") => Box::new(Speakers::start(false)),
        _ => Box::new(Speakers::start(true)),
    }
}

/// No output at all (`CLARP_AUDIO_OUTPUT=none`): every clip fails at once.
pub struct Unavailable;

impl AudioOutput for Unavailable {
    fn play(&mut self, _playback: Playback, _rate: f64, events: Sink) {
        events(OutputEvent::Ended { result: Err(MISSING_BACKEND.into()), missing_backend: true });
    }
    fn stop(&mut self) {}
    fn pause(&mut self) {}
    fn resume(&mut self) {}
}

fn source(playback: Playback) -> Result<Box<dyn rodio::Source + Send>, String> {
    use rodio::Source;
    match playback {
        // A buffered clip is seekable with a known length, which the mp4
        // demuxer needs to find its index.
        Playback::Media { bytes } => rodio::decoder::DecoderBuilder::new()
            .with_byte_len(bytes.len() as u64)
            .with_seekable(true)
            .with_data(std::io::Cursor::new(bytes))
            .build()
            .map(|decoder| Box::new(decoder) as Box<dyn Source + Send>)
            .map_err(|error| format!("the clip could not be decoded: {error}")),
        Playback::Pcm { bytes, sample_rate, channels, encoding } => {
            let samples: Vec<rodio::Sample> = match encoding.as_str() {
                "pcm_f32le" => bytes.as_chunks::<4>().0.iter().map(|b| f32::from_le_bytes(*b)).collect(),
                "pcm_s16le" => bytes.as_chunks::<2>().0.iter().map(|b| f32::from(i16::from_le_bytes(*b)) / 32_768.0).collect(),
                "pcm_u8" => bytes.iter().map(|b| (f32::from(*b) - 128.0) / 128.0).collect(),
                other => return Err(format!("unsupported raw PCM encoding: {other}")),
            };
            let (Some(channels), Some(rate)) = (std::num::NonZero::new(channels), std::num::NonZero::new(sample_rate)) else {
                return Err("the clip format has no channels or rate".into());
            };
            let buffer = rodio::buffer::SamplesBuffer::new(channels, rate, samples);
            Ok(Box::new(buffer) as Box<dyn Source + Send>)
        }
    }
}

enum Command {
    Play { playback: Playback, rate: f64, events: Sink },
    Stop,
    Pause,
    Resume,
}

/// The default output device, on a thread of its own (the device stream is
/// not `Send`). Opened on the first clip; `device: false` decodes only.
pub struct Speakers {
    commands: std::sync::mpsc::Sender<Command>,
}

impl Speakers {
    fn start(device: bool) -> Self {
        let (commands, inbox) = std::sync::mpsc::channel();
        let spawned = std::thread::Builder::new().name("clarp-audio".into()).spawn(move || speaker_thread(inbox, device));
        if let Err(error) = spawned {
            eprintln!("AudioController: could not start the audio thread: {error}");
        }
        Self { commands }
    }

    fn send(&self, command: Command) {
        if self.commands.send(command).is_err() {
            eprintln!("AudioController: the audio thread has stopped");
        }
    }
}

impl AudioOutput for Speakers {
    fn play(&mut self, playback: Playback, rate: f64, events: Sink) {
        self.send(Command::Play { playback, rate, events });
    }
    fn stop(&mut self) {
        self.send(Command::Stop);
    }
    fn pause(&mut self) {
        self.send(Command::Pause);
    }
    fn resume(&mut self) {
        self.send(Command::Resume);
    }
}

fn speaker_thread(inbox: std::sync::mpsc::Receiver<Command>, device: bool) {
    use std::sync::mpsc::RecvTimeoutError;
    let mut output: Option<(rodio::MixerDeviceSink, rodio::Player)> = None;
    let mut playing: Option<Sink> = None;
    loop {
        match inbox.recv_timeout(Duration::from_millis(50)) {
            Ok(Command::Play { playback, rate, events }) => {
                if let Some((_, player)) = &output {
                    player.clear();
                }
                let source = match source(playback) {
                    Ok(source) => source,
                    Err(error) => {
                        events(OutputEvent::Ended { result: Err(error), missing_backend: false });
                        continue;
                    }
                };
                if !device {
                    // Decode-only: the whole clip decodes, nothing is heard.
                    let _ = source.count();
                    events(OutputEvent::Started);
                    events(OutputEvent::Ended { result: Ok(()), missing_backend: false });
                    continue;
                }
                if output.is_none() {
                    match rodio::DeviceSinkBuilder::open_default_sink() {
                        Ok(mut sink) => {
                            sink.log_on_drop(false);
                            let player = rodio::Player::connect_new(sink.mixer());
                            output = Some((sink, player));
                        }
                        Err(error) => {
                            eprintln!("AudioController: no output device: {error}");
                            events(OutputEvent::Ended { result: Err(MISSING_BACKEND.into()), missing_backend: true });
                            continue;
                        }
                    }
                }
                if let Some((_, player)) = &output {
                    player.append(source);
                    player.set_speed(rate as f32);
                    player.play();
                    events(OutputEvent::Started);
                    playing = Some(events);
                }
            }
            Ok(Command::Stop) => {
                playing = None;
                if let Some((_, player)) = &output {
                    player.clear();
                }
            }
            Ok(Command::Pause) => {
                if let Some((_, player)) = &output {
                    player.pause();
                }
            }
            Ok(Command::Resume) => {
                if let Some((_, player)) = &output {
                    player.play();
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return,
        }
        let finished = output.as_ref().is_some_and(|(_, player)| player.empty());
        if finished && let Some(events) = playing.take() {
            events(OutputEvent::Ended { result: Ok(()), missing_backend: false });
        }
    }
}

/// Plays nothing, for tests: starts at once and ends after a short while
/// unless stopped; pausing holds the end.
#[derive(Default)]
pub struct Silent {
    generation: Arc<AtomicU64>,
    paused: Arc<std::sync::atomic::AtomicBool>,
}

pub const SILENT_CLIP_MS: u64 = 150;

/// How long a silent clip lasts; `CLARP_TEST_SILENT_CLIP_MS` lengthens it
/// for checks that control playback from outside (MPRIS).
fn silent_clip_ms() -> u64 {
    std::env::var("CLARP_TEST_SILENT_CLIP_MS").ok().and_then(|ms| ms.parse().ok()).unwrap_or(SILENT_CLIP_MS)
}

impl AudioOutput for Silent {
    fn play(&mut self, _playback: Playback, _rate: f64, events: Sink) {
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        self.paused.store(false, Ordering::SeqCst);
        events(OutputEvent::Started);
        let (current, paused) = (self.generation.clone(), self.paused.clone());
        fn wait(current: Arc<AtomicU64>, paused: Arc<std::sync::atomic::AtomicBool>, generation: u64, events: Sink) {
            crate::platform::runtime::after(Duration::from_millis(silent_clip_ms()), move || {
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
