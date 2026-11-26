//! `--check playback --out DIR`: the in-app player plays a synthetic clip
//! from the fake Host (check.sh makes it: 6 s of test picture and a 440 Hz
//! tone, H.264 and AAC). It opens paused on its first picture, plays,
//! pauses, seeks with the keys, goes fullscreen and back, and closes on
//! Escape with its sound stopped. mpv's sound goes to a WAV file
//! (`playback-sound.wav`) that must hold the tone. Then an audio item (the
//! fake Host's MP3) plays, pauses and closes. Needs libmpv and ffmpeg.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use slint::ComponentHandle;

use super::{Stage, check, run_stages, shot};
use crate::PlaybackBridge;
use crate::playback::{self, Kind, Request};

fn bridge(window: &crate::AppWindow) -> PlaybackBridge<'_> {
    window.global::<PlaybackBridge>()
}

/// A short fingerprint of the picture shown (0 when there is none).
fn picture(window: &crate::AppWindow) -> u64 {
    let Some(pixels) = bridge(window).get_frame().to_rgba8() else { return 0 };
    let bytes = pixels.as_bytes();
    let step = (bytes.len() / 4096).max(1);
    bytes.iter().step_by(step).fold(1469598103934665603u64, |h, b| (h ^ u64::from(*b)).wrapping_mul(1099511628211))
}

/// The picture's size in pixels.
fn picture_size(window: &crate::AppWindow) -> (u32, u32) {
    let size = bridge(window).get_frame().size();
    (size.width, size.height)
}

/// Whether the picture has colour in it (the test picture), not black.
fn colourful(window: &crate::AppWindow) -> bool {
    let Some(pixels) = bridge(window).get_frame().to_rgba8() else { return false };
    let lit = pixels.as_slice().iter().filter(|p| u16::from(p.r) + u16::from(p.g) + u16::from(p.b) > 120).count();
    lit * 4 > pixels.as_slice().len()
}

fn key(window: &crate::AppWindow, key: impl Into<slint::SharedString>) -> bool {
    bridge(window).invoke_key(key.into(), false)
}

/// The 440 Hz tone's loudness in the WAV mpv wrote (RMS of 16-bit or float
/// samples), and how many seconds it holds.
fn sound(path: &str) -> (f64, f64) {
    let Ok(data) = std::fs::read(path) else { return (0.0, 0.0) };
    // RIFF: find "fmt " and "data".
    let find = |tag: &[u8]| data.windows(4).position(|w| w == tag);
    let (Some(fmt), Some(at)) = (find(b"fmt "), find(b"data")) else { return (0.0, 0.0) };
    let u16_at = |i: usize| u16::from_le_bytes([data[i], data[i + 1]]);
    let u32_at = |i: usize| u32::from_le_bytes([data[i], data[i + 1], data[i + 2], data[i + 3]]);
    let (format, channels, rate, bits) = (u16_at(fmt + 8), u16_at(fmt + 10), u32_at(fmt + 12), u16_at(fmt + 22));
    let samples = &data[(at + 8).min(data.len())..];
    let values: Vec<f64> = match (format, bits) {
        (1, 16) => samples.chunks_exact(2).map(|c| f64::from(i16::from_le_bytes([c[0], c[1]])) / 32768.0).collect(),
        (3, 32) | (0xfffe, 32) => samples.chunks_exact(4).map(|c| f64::from(f32::from_le_bytes([c[0], c[1], c[2], c[3]]))).collect(),
        (1, 32) => samples.chunks_exact(4).map(|c| f64::from(i32::from_le_bytes([c[0], c[1], c[2], c[3]])) / 2147483648.0).collect(),
        _ => return (0.0, 0.0),
    };
    if values.is_empty() || channels == 0 || rate == 0 {
        return (0.0, 0.0);
    }
    let rms = (values.iter().map(|v| v * v).sum::<f64>() / values.len() as f64).sqrt();
    (rms, values.len() as f64 / f64::from(channels) / f64::from(rate))
}

fn video() -> Request {
    Request {
        key: "media:playback-video".into(),
        session: "rachel".into(),
        kind: Kind::Video,
        path: "/media/playback-video".into(),
        title: "Synthetic clip".into(),
        poster: None,
        duration_ms: 6000,
        autoplay: false,
    }
}

pub fn playback_check(out: String) {
    let o = |n: usize| (0..n).map(|_| out.clone()).collect::<Vec<_>>();
    let [o1, o2, o3, o4, o5] = <[String; 5]>::try_from(o(5)).expect("five");
    let first: Rc<Cell<u64>> = Rc::default();
    let at: Rc<Cell<f32>> = Rc::default();
    let (first2, at2, at3, at4) = (first.clone(), at.clone(), at.clone(), at.clone());
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(crate::platform::mpv::unavailable().is_none(), &format!("libmpv loads here: {:?}", crate::platform::mpv::unavailable()));
            playback::open(app, video());
            check(bridge(window).get_kind() == "video" && playback::active(), "the video opens in the player");
            true
        })),
        ("first picture, paused", Box::new(move |_, window, elapsed| {
            let b = bridge(window);
            if !b.get_has_frame() || b.get_preparing() {
                return elapsed > Duration::from_secs(10) && { check(false, &format!("its first picture shows: {:?}", b.get_message())); true };
            }
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            check(!b.get_playing() && b.get_position() < 0.2, &format!("it waits, paused at the start, until asked to play: playing={} at {}", b.get_playing(), b.get_position()));
            check((b.get_duration() - 6.0).abs() < 0.3, &format!("it knows its length: {}", b.get_duration()));
            check(colourful(window), "the picture is the clip's, not black");
            let ranged = super::requests("GET", "/media/playback-video");
            check(!ranged.is_empty() && ranged.iter().all(|r| r["authorization"] == "Bearer probe-token"), "the clip comes from the Host with the app's token");
            check(ranged.iter().any(|r| r["range"].as_str().is_some_and(|v| v.starts_with("bytes="))), "in parts, as a player reads it");
            first2.set(picture(window));
            shot(&o1, "playback-1-paused");
            check(key(window, " "), "Space is the player's");
            true
        })),
        ("plays", Box::new(move |_, window, elapsed| {
            let b = bridge(window);
            if !b.get_playing() || b.get_position() < 1.5 {
                return elapsed > Duration::from_secs(6) && { check(false, &format!("Space plays it: playing={} at {}", b.get_playing(), b.get_position())); true };
            }
            check(picture(window) != first.get(), "the picture moves while it plays");
            shot(&o2, "playback-2-playing");
            at2.set(b.get_position());
            check(key(window, slint::platform::Key::RightArrow), "→ is the player's");
            true
        })),
        ("seeks", Box::new(move |_, window, elapsed| {
            let b = bridge(window);
            if b.get_position() < at3.get() + 4.0 {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("→ moves 5 s on: {} then {}", at3.get(), b.get_position())); true };
            }
            check(key(window, slint::platform::Key::LeftArrow), "← is the player's");
            check(key(window, " "), "Space again");
            true
        })),
        ("pauses", Box::new(move |_, window, elapsed| {
            let b = bridge(window);
            if b.get_playing() {
                return elapsed > Duration::from_secs(3) && { check(false, "Space pauses it"); true };
            }
            if elapsed < Duration::from_millis(600) {
                at4.set(b.get_position());
                return false;
            }
            check((b.get_position() - at4.get()).abs() < 0.05, &format!("and it stays where it paused: {} then {}", at4.get(), b.get_position()));
            check(b.get_position() > 1.0 && b.get_position() < 5.0, &format!("← went back 5 s: at {}", b.get_position()));
            check(key(window, "m") && bridge(window).get_has_volume(), "M mutes");
            true
        })),
        ("mute and volume", Box::new(|_, window, elapsed| {
            let b = bridge(window);
            if !b.get_muted() {
                return elapsed > Duration::from_secs(2) && { check(false, "M mutes the sound"); true };
            }
            key(window, "m");
            key(window, slint::platform::Key::DownArrow);
            true
        })),
        ("unmuted, quieter", Box::new(|_, window, elapsed| {
            let b = bridge(window);
            if b.get_muted() || b.get_volume() > 99.0 {
                return elapsed > Duration::from_secs(2) && { check(false, &format!("M again unmutes and ↓ lowers it: muted={} volume={}", b.get_muted(), b.get_volume())); true };
            }
            check((b.get_volume() - 95.0).abs() < 0.5, &format!("↓ takes 5 off: {}", b.get_volume()));
            key(window, slint::platform::Key::UpArrow);
            check(key(window, "f"), "F is the player's");
            true
        })),
        ("fullscreen", Box::new(move |_, window, elapsed| {
            let b = bridge(window);
            let scale = window.window().scale_factor();
            let size = window.window().size();
            let (w, h) = picture_size(window);
            // The picture is redrawn for the whole window (less the controls).
            if !b.get_fullscreen() || w + 2 < size.width || (h as f32) < size.height as f32 - 60.0 * scale {
                return elapsed > Duration::from_secs(4) && { check(false, &format!("F fills the window: fullscreen={} picture {w}x{h} window {}x{}", b.get_fullscreen(), size.width, size.height)); true };
            }
            shot(&o3, "playback-3-fullscreen");
            check(key(window, slint::platform::Key::Escape), "Escape leaves fullscreen first");
            true
        })),
        ("escape closes", Box::new(|_, window, elapsed| {
            let b = bridge(window);
            if b.get_fullscreen() {
                return elapsed > Duration::from_secs(2) && { check(false, "Escape left fullscreen"); true };
            }
            check(!key(window, slint::platform::Key::Escape), "out of fullscreen, Escape is the viewer's (it closes)");
            let started = std::time::Instant::now();
            playback::close();
            let took = started.elapsed();
            check(took < Duration::from_millis(50), &format!("closing never waits for the player: {took:?}"));
            check(!playback::active() && b.get_kind().is_empty() && !b.get_has_frame() && !b.get_playing(), "closing ends it: no item, no picture");
            true
        })),
        ("its sound", Box::new(move |_, _, elapsed| {
            // mpv writes the file's header as it closes, on its own thread.
            if elapsed < Duration::from_millis(1500) {
                return false;
            }
            let file = format!("{o4}/playback-sound.wav");
            let (rms, seconds) = sound(&file);
            check(rms > 0.05 && seconds > 1.0, &format!("the clip's tone was played (written to {file}): RMS {rms:.3} over {seconds:.1} s"));
            true
        })),
        ("redirect elsewhere", Box::new(|app, window, _| {
            playback::open(app, Request { key: "media:moved".into(), path: "/media/playback-moved".into(), ..video() });
            check(bridge(window).get_kind() == "video", "a video that moved opens");
            true
        })),
        ("refused, token kept home", Box::new(|_, window, elapsed| {
            let b = bridge(window);
            if b.get_message().is_empty() {
                return elapsed > Duration::from_secs(8) && { check(false, "a video moved to another origin fails"); true };
            }
            check(!b.get_has_frame() && !b.get_playing(), &format!("it doesn't play: {:?}", b.get_message()));
            check(super::requests("GET", "/media/playback-stolen").is_empty(), "nothing went to the other origin (no token crossed)");
            true
        })),
        ("redirect at home", Box::new(|app, window, _| {
            playback::open(app, Request { key: "media:here".into(), path: "/media/playback-here".into(), ..video() });
            check(bridge(window).get_message().is_empty(), "the failed one closed with it");
            true
        })),
        ("plays from where it moved", Box::new(|_, window, elapsed| {
            let b = bridge(window);
            if !b.get_has_frame() {
                return elapsed > Duration::from_secs(10) && { check(false, &format!("a video moved on its own Host plays: {:?}", b.get_message())); true };
            }
            check(colourful(window), "its picture shows");
            playback::close();
            true
        })),
        ("audio", Box::new(|app, window, _| {
            playback::open(app, Request {
                key: "media:aud1".into(),
                session: "rachel".into(),
                kind: Kind::Audio,
                path: "/media/aud1".into(),
                title: "Standup summary".into(),
                poster: None,
                duration_ms: 72000,
                autoplay: false,
            });
            let b = bridge(window);
            check(b.get_kind() == "audio" && !b.get_playing(), "an audio item opens, quiet until asked");
            check(key(window, " "), "Space plays it");
            true
        })),
        ("audio plays", Box::new(move |_, window, elapsed| {
            let b = bridge(window);
            if !b.get_playing() {
                return elapsed > Duration::from_secs(5) && { check(false, &format!("the audio plays: {:?}", b.get_message())); true };
            }
            if b.get_position() < 1.0 {
                return elapsed > Duration::from_secs(6) && { check(false, &format!("the audio moves on: at {}", b.get_position())); true };
            }
            shot(&o5, "playback-4-audio");
            check(!key(window, "f"), "audio has no fullscreen");
            key(window, " ");
            true
        })),
        ("audio pauses and closes", Box::new(|_, window, elapsed| {
            let b = bridge(window);
            if b.get_playing() {
                return elapsed > Duration::from_secs(3) && { check(false, "Space pauses the audio"); true };
            }
            playback::close();
            let left = crate::platform::audio::with(|audio| audio.media().is_some()).unwrap_or(false);
            check(!left && !playback::active(), "closing stops the audio");
            true
        })),
    ];
    run_stages(stages);
}
