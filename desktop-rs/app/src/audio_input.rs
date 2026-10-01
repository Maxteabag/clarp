//! Where dictation is recorded from: the default microphone through rodio,
//! 16 kHz mono when the device offers it (C++ `AudioController`). For tests,
//! `CLARP_AUDIO_INPUT=file:<wav>` "records" a fixture and `none` has no
//! microphone, so probes never open the user's microphone.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

use clarp_core::audio::PcmFormat;

pub const NO_MICROPHONE: &str = "No microphone is available";
pub const NOT_STARTED: &str = "The microphone could not be started";

/// A running recording; `finish` stops it and returns 16-bit PCM.
pub struct Recording {
    stop: Arc<AtomicBool>,
    result: mpsc::Receiver<(Vec<u8>, PcmFormat)>,
}

impl Recording {
    pub fn finish(self) -> (Vec<u8>, PcmFormat) {
        self.stop.store(true, Ordering::SeqCst);
        self.result.recv().unwrap_or_else(|_| (Vec::new(), PcmFormat::DICTATION))
    }

    /// Stops and throws the audio away.
    pub fn cancel(self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

fn to_pcm16(samples: impl Iterator<Item = f32>) -> Vec<u8> {
    samples.flat_map(|s| ((s.clamp(-1.0, 1.0) * 32_767.0).round() as i16).to_le_bytes()).collect()
}

pub fn start() -> Result<Recording, String> {
    match std::env::var("CLARP_AUDIO_INPUT") {
        Ok(input) if input == "none" => Err(NO_MICROPHONE.into()),
        Ok(input) if input.starts_with("file:") => from_file(input.trim_start_matches("file:")),
        _ => from_microphone(),
    }
}

fn from_file(path: &str) -> Result<Recording, String> {
    use rodio::Source;
    let bytes = std::fs::read(path).map_err(|error| format!("{NO_MICROPHONE}: {path}: {error}"))?;
    let decoder = rodio::Decoder::new(std::io::Cursor::new(bytes)).map_err(|error| format!("{NOT_STARTED}: {error}"))?;
    let format = PcmFormat { sample_rate: decoder.sample_rate().get(), channels: decoder.channels().get(), bytes_per_sample: 2, float: false };
    let pcm = to_pcm16(decoder);
    let (sender, result) = mpsc::channel();
    // The whole fixture is "recorded" by the time recording stops.
    if sender.send((pcm, format)).is_err() {
        eprintln!("AudioController: the fixture recording was dropped");
    }
    Ok(Recording { stop: Arc::new(AtomicBool::new(false)), result })
}

fn from_microphone() -> Result<Recording, String> {
    let stop = Arc::new(AtomicBool::new(false));
    let (sender, result) = mpsc::channel();
    let (started, opened) = mpsc::channel::<Result<(), String>>();
    let stopping = stop.clone();
    // The input stream is not Send: open it and read it on its own thread.
    let spawned = std::thread::Builder::new().name("clarp-microphone".into()).spawn(move || {
        use rodio::Source;
        let builder = match rodio::microphone::MicrophoneBuilder::new().default_device().and_then(|b| b.default_config()) {
            Ok(builder) => builder,
            Err(error) => {
                eprintln!("AudioController: no microphone: {error}");
                // from_microphone waits on this; it cannot be gone.
                let _ = started.send(Err(NO_MICROPHONE.into()));
                return;
            }
        };
        let preferred = std::num::NonZero::new(16_000).and_then(|rate| builder.try_sample_rate(rate).ok());
        let builder = preferred.unwrap_or(builder);
        let builder = std::num::NonZero::new(1).and_then(|mono| builder.try_channels(mono).ok()).unwrap_or(builder);
        let microphone = match builder.open_stream() {
            Ok(microphone) => microphone,
            Err(error) => {
                eprintln!("AudioController: the microphone did not open: {error}");
                let _ = started.send(Err(NOT_STARTED.into()));
                return;
            }
        };
        let format = PcmFormat {
            sample_rate: microphone.sample_rate().get(),
            channels: microphone.channels().get(),
            bytes_per_sample: 2,
            float: false,
        };
        let _ = started.send(Ok(()));
        let mut samples = Vec::new();
        for sample in microphone {
            if stopping.load(Ordering::SeqCst) {
                break;
            }
            samples.push(sample);
        }
        if sender.send((to_pcm16(samples.into_iter()), format)).is_err() {
            // Cancelled: nobody waits for the audio.
        }
    });
    if let Err(error) = spawned {
        eprintln!("AudioController: could not start the microphone thread: {error}");
        return Err(NOT_STARTED.into());
    }
    match opened.recv() {
        Ok(Ok(())) => Ok(Recording { stop, result }),
        Ok(Err(message)) => Err(message),
        Err(_) => Err(NOT_STARTED.into()),
    }
}
