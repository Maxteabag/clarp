//! The sound cues of a call of the Oracle, made on the Host.
//!
//! A cue is a short tone the Host makes itself and hands to the phone as sound, in the
//! format the voice streams and the phone already plays (24 kHz mono PCM16): a call is put
//! through to an agent (a bright chime, pitched by the agent's name), the user is back
//! with the Oracle, a handoff did not happen, work was handed on, a result is about to be
//! read. Every cue follows from something the Host did, never from what anybody said.
//!
//! These are the Python Host's rules (`server/lib/oracle_earcons.py`), sample for sample:
//! sine tones with a soft attack and release, or a decay after the attack. Held to that
//! Host's own module by `tests/fixtures/oracle-earcon-vectors.json`.

use std::f64::consts::PI;

use sha2::{Digest, Sha256};

use crate::python;

/// How many samples a second a cue has.
pub const RATE: u32 = 24_000;
/// The cues there are.
pub const CUES: [&str; 5] = [
    "connected",
    "back_to_oracle",
    "switch_failed",
    "handed_off",
    "result",
];
const FADE_SECONDS: f64 = 0.012;
/// Five notes of one scale: an agent's chime has one of them as its root.
const TINTS: [f64; 5] = [523.25, 587.33, 659.25, 783.99, 880.00];
/// The parts a tone is made of: each a multiple of its pitch, and how much of it.
type Partials = &'static [(f64, f64)];
const PLAIN: Partials = &[(1.0, 1.0)];
const SOFT: Partials = &[(1.0, 1.0), (2.0, 0.18)];
const BELL: Partials = &[(1.0, 1.0), (2.76, 0.35), (5.4, 0.12)];
const LOW: Partials = &[(1.0, 1.0), (3.0, 0.25)];

/// One tone: its pitch, how long it is, how loud, the time in which it dies away after its
/// attack (or a fade out at its end), and what it is made of.
struct Tone {
    freq: f64,
    seconds: f64,
    level: f64,
    decay: Option<f64>,
    partials: Partials,
}

fn rate() -> f64 {
    f64::from(RATE)
}

/// How many samples so many seconds are.
#[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
fn samples_in(seconds: f64) -> usize {
    (rate() * seconds) as usize
}

#[allow(clippy::cast_precision_loss)]
fn real(count: usize) -> f64 {
    count as f64
}

/// The samples of a tone, between -1 and 1.
fn tone(tone: &Tone) -> Vec<f64> {
    let count = samples_in(tone.seconds);
    let fade = samples_in(FADE_SECONDS).max(1);
    let norm = tone
        .partials
        .iter()
        .fold(0.0, |sum, (_, weight)| sum + weight);
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let t = real(i) / rate();
        let parts = tone.partials.iter();
        let wave = parts.fold(0.0, |sum, (multiple, weight)| {
            sum + weight * (2.0 * PI * tone.freq * multiple * t).sin()
        });
        let value = wave / norm;
        let mut envelope = 1.0;
        if i < fade {
            envelope = 0.5 - 0.5 * (PI * real(i) / real(fade)).cos();
        }
        if count - i <= fade {
            envelope *= 0.5 - 0.5 * (PI * real(count - i) / real(fade)).cos();
        }
        if let Some(decay) = tone.decay {
            envelope *= (-t / decay).exp();
        }
        out.push(value * envelope * tone.level);
    }
    out
}

fn silence(seconds: f64) -> Vec<f64> {
    vec![0.0; samples_in(seconds)]
}

/// Samples as the bytes of PCM16, the loudest of them near -18 dBFS.
#[allow(clippy::cast_possible_truncation)]
fn pcm16(samples: &[f64]) -> Vec<u8> {
    let scale = 32767.0 * 10f64.powf(-18.0 / 20.0);
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for sample in samples {
        let whole = (sample.clamp(-1.0, 1.0) * scale).round_ties_even() as i16;
        bytes.extend_from_slice(&whole.to_le_bytes());
    }
    bytes
}

/// The root of an agent's chime: one of five notes, by the agent's name whatever the case
/// of its letters; the third for nobody.
pub fn tint(persona: &str) -> f64 {
    let key = python::casefold(python::strip(persona));
    if key.is_empty() {
        return TINTS[2];
    }
    // The digest as one number, by what is left of it when it is told off in fives.
    let digest = Sha256::digest(key.as_bytes());
    let left = digest.iter().fold(0_usize, |left, byte| {
        (left * 256 + usize::from(*byte)) % TINTS.len()
    });
    TINTS[left]
}

/// The sound of a cue as PCM16. `persona` pitches the chime of a call put through to an
/// agent, and no other cue. `None`: there is no cue of the name.
pub fn pcm(name: &str, persona: &str) -> Option<Vec<u8>> {
    let plain = |freq: f64, seconds: f64, partials: Partials| Tone {
        freq,
        seconds,
        level: 1.0,
        decay: None,
        partials,
    };
    let samples = match name {
        "connected" => {
            let root = tint(persona);
            let rings = Tone {
                decay: Some(0.12),
                ..plain(root * 1.5, 0.22, BELL)
            };
            [tone(&plain(root, 0.09, SOFT)), tone(&rings)].concat()
        }
        "back_to_oracle" => [
            tone(&plain(880.00, 0.12, SOFT)),
            silence(0.03),
            tone(&plain(659.25, 0.15, SOFT)),
        ]
        .concat(),
        "switch_failed" => [
            tone(&plain(220.00, 0.12, LOW)),
            silence(0.06),
            tone(&plain(196.00, 0.14, LOW)),
        ]
        .concat(),
        "handed_off" => tone(&Tone {
            level: 0.35,
            decay: Some(0.025),
            ..plain(1318.5, 0.12, PLAIN)
        }),
        "result" => tone(&Tone {
            level: 0.7,
            decay: Some(0.14),
            ..plain(1046.5, 0.40, BELL)
        }),
        _ => return None,
    };
    Some(pcm16(&samples))
}
