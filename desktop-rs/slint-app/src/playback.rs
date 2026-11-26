//! The in-app player for published audio and video: what `MediaPlayer`
//! (ui/playback.slint) shows and what its keys and buttons do.
//!
//! Video and audio play through libmpv (`platform::mpv`), video drawn into
//! the window, from the item's own Host (a peer chat's included). Without
//! libmpv, audio from the primary Host falls back to the app's own audio
//! (`platform::audio`, which plays a chat's audio cards). One item plays at a time: opening another,
//! `close`, or a change of Host ends it. Nothing starts making sound unless
//! the caller says the user asked to play.

use std::cell::RefCell;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use slint::{ComponentHandle, SharedString};

use crate::platform::{self, mpv};
use crate::{AppWindow, PlaybackBridge};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Video,
    Audio,
}

/// An item to play.
#[derive(Clone)]
pub struct Request {
    pub artifact_id: String,
    /// The chat it was published in, as the engine knows it (a peer
    /// Host's is prefixed): its media comes from that chat's Host.
    pub session: String,
    pub kind: Kind,
    /// The artifact's media on the Host (`/media/...`); only the Host's own
    /// media plays.
    pub path: String,
    pub title: String,
    pub poster: Option<slint::Image>,
    /// The length the artifact gives, until the media says its own.
    pub duration_ms: i64,
    /// Play at once (the user chose Play); otherwise open paused.
    pub autoplay: bool,
}

/// Seconds the arrow keys move; with Shift, and J/L.
const STEP: f64 = 5.0;
const LONG_STEP: f64 = 30.0;
const JUMP: f64 = 10.0;

struct Playing {
    artifact: String,
    kind: Kind,
    path: String,
    /// The length the artifact gave, in seconds.
    length: f64,
    video: Option<mpv::Player>,
    state: mpv::State,
    /// Tells this item's late updates from a newer one's.
    generation: u64,
    /// Follows the app's audio while it plays an audio item (no libmpv).
    ticker: Option<slint::Timer>,
    /// The window was fullscreen before the player made it so.
    was_fullscreen: bool,
}

thread_local! {
    static NOW: RefCell<Option<Playing>> = const { RefCell::new(None) };
    static GENERATION: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

/// Wires the bridge's buttons and keys (once, with the window).
pub fn install(window: &AppWindow) {
    let bridge = window.global::<PlaybackBridge>();
    bridge.on_toggle(toggle);
    bridge.on_seek_to(|seconds| seek_to(f64::from(seconds)));
    bridge.on_seek_by(|seconds| seek_by(f64::from(seconds)));
    bridge.on_toggle_mute(toggle_mute);
    bridge.on_set_volume(|volume| set_volume(f64::from(volume)));
    bridge.on_toggle_fullscreen(toggle_fullscreen);
    bridge.on_surface_resized(|w, h| {
        NOW.with(|now| {
            if let Some(video) = now.borrow().as_ref().and_then(|p| p.video.as_ref()) {
                video.resize(w.max(0) as u32, h.max(0) as u32);
            }
        });
    });
    bridge.on_key(|key, shift| key_pressed(&key, shift));
}

/// Whether an item is open in the player.
pub fn active() -> bool {
    NOW.with(|now| now.borrow().is_some())
}

/// The artifact open in the player.
pub fn active_artifact() -> Option<String> {
    NOW.with(|now| now.borrow().as_ref().map(|p| p.artifact.clone()))
}

/// Opens `request` in the player, ending whatever played before.
pub fn open(app: &crate::App, request: Request) {
    close();
    let Some(window) = crate::window() else { return };
    let generation = GENERATION.with(|g| {
        g.set(g.get() + 1);
        g.get()
    });
    let bridge = window.global::<PlaybackBridge>();
    bridge.set_kind(match request.kind {
        Kind::Video => "video",
        Kind::Audio => "audio",
    }.into());
    bridge.set_title(request.title.as_str().into());
    bridge.set_has_poster(request.poster.is_some());
    bridge.set_poster(request.poster.clone().unwrap_or_default());
    bridge.set_frame(slint::Image::default());
    bridge.set_has_frame(false);
    bridge.set_playing(false);
    bridge.set_position(0.0);
    let length = request.duration_ms.max(0) as f64 / 1000.0;
    bridge.set_duration(length as f32);
    bridge.set_message(SharedString::new());
    bridge.set_has_volume(false);
    bridge.set_fullscreen(false);
    let path = request.path.trim().to_owned();
    if !path.starts_with('/') || path.starts_with("//") {
        bridge.set_message(format!("{} unavailable", if request.kind == Kind::Video { "Video" } else { "Audio" }).into());
        return;
    }
    let mut playing = Playing {
        artifact: request.artifact_id.clone(),
        kind: request.kind,
        path: path.clone(),
        length,
        video: None,
        state: mpv::State { paused: true, volume: 100.0, ..mpv::State::default() },
        generation,
        ticker: None,
        was_fullscreen: window.window().is_fullscreen(),
    };
    let endpoint = app.engine.borrow().endpoint_for(&request.session);
    let primary = endpoint.is_some() && endpoint == app.engine.borrow().endpoint();
    let missing = mpv::unavailable();
    match (request.kind, missing) {
        (Kind::Audio, Some(_)) if primary => {
            if request.autoplay {
                let id = request.artifact_id.clone();
                platform::audio::with(|audio| audio.toggle_media(&id, &path));
            }
            let ticker = slint::Timer::default();
            ticker.start(slint::TimerMode::Repeated, Duration::from_millis(200), follow_audio);
            playing.ticker = Some(ticker);
        }
        (_, Some(why)) => bridge.set_message(why.into()),
        (_, None) => {
            if let Some((base, token)) = endpoint {
                let api = clarp_net::ApiClient::new(platform::runtime::handle(), |_| {});
                api.set_endpoint(base, &token);
                if request.autoplay {
                    quiet_other_sound();
                }
                bridge.set_preparing(true);
                match mpv::Player::open(api, platform::runtime::handle(), &path, request.autoplay, deliverer(generation)) {
                    Ok(player) => {
                        playing.video = Some(player);
                        bridge.set_has_volume(true);
                    }
                    Err(error) => {
                        eprintln!("clarp-slint video: {error}");
                        bridge.set_preparing(false);
                        bridge.set_message(error.into());
                    }
                }
            } else {
                bridge.set_message("Not connected to its Host".into());
            }
        }
    }
    let rodio = playing.ticker.is_some();
    NOW.with(|now| *now.borrow_mut() = Some(playing));
    if rodio {
        follow_audio();
    }
}

/// Ends the open item: its sound stops at once, the player is torn down,
/// and a fullscreen it made is left.
pub fn close() {
    let Some(playing) = NOW.with(|now| now.borrow_mut().take()) else { return };
    if playing.ticker.is_some() {
        platform::audio::with(|audio| audio.stop_media(&playing.artifact));
    }
    drop(playing.video);
    if let Some(window) = crate::window() {
        let bridge = window.global::<PlaybackBridge>();
        if bridge.get_fullscreen() && !playing.was_fullscreen {
            window.window().set_fullscreen(false);
        }
        bridge.set_fullscreen(false);
        bridge.set_kind(SharedString::new());
        bridge.set_frame(slint::Image::default());
        bridge.set_has_frame(false);
        bridge.set_playing(false);
        bridge.set_preparing(false);
        bridge.set_message(SharedString::new());
    }
}

/// A key in the player. Escape is taken only to leave fullscreen; otherwise
/// it is left to the viewer, which closes (and calls `close`).
pub fn key_pressed(key: &str, shift: bool) -> bool {
    use slint::platform::Key;
    if !active() {
        return false;
    }
    let is = |k: Key| key == SharedString::from(k).as_str();
    let (video, sound) = NOW.with(|now| now.borrow().as_ref().map_or((false, false), |p| (p.kind == Kind::Video, p.video.is_some())));
    if is(Key::Escape) {
        let full = crate::window().is_some_and(|w| w.global::<PlaybackBridge>().get_fullscreen());
        if full {
            toggle_fullscreen();
        }
        return full;
    }
    match key {
        " " | "k" | "K" => toggle(),
        "j" | "J" => seek_by(-JUMP),
        "l" | "L" => seek_by(JUMP),
        "m" | "M" if sound => toggle_mute(),
        "f" | "F" if video => toggle_fullscreen(),
        _ if is(Key::LeftArrow) => seek_by(if shift { -LONG_STEP } else { -STEP }),
        _ if is(Key::RightArrow) => seek_by(if shift { LONG_STEP } else { STEP }),
        _ if is(Key::Home) => seek_to(0.0),
        _ if is(Key::End) => seek_to(f64::MAX),
        _ if is(Key::UpArrow) && sound => nudge_volume(5.0),
        _ if is(Key::DownArrow) && sound => nudge_volume(-5.0),
        _ => return false,
    }
    true
}

/// Play or pause.
pub fn toggle() {
    let Some((rodio, artifact, path)) = NOW.with(|now| now.borrow().as_ref().map(|p| (p.ticker.is_some(), p.artifact.clone(), p.path.clone()))) else { return };
    match rodio {
        false => {
            let resume = NOW.with(|now| {
                let now = now.borrow();
                let Some(playing) = now.as_ref() else { return None };
                let video = playing.video.as_ref()?;
                let resume = playing.state.paused || playing.state.ended;
                if resume && playing.state.ended {
                    video.seek_to(0.0);
                }
                video.set_paused(!resume);
                Some(resume)
            });
            if resume == Some(true) {
                quiet_other_sound();
            }
        }
        true => {
            platform::audio::with(|audio| audio.toggle_media(&artifact, &path));
            follow_audio();
        }
    }
}

/// Moves to `seconds` from the start (the end for anything past it).
pub fn seek_to(seconds: f64) {
    NOW.with(|now| {
        let now = now.borrow();
        let Some(playing) = now.as_ref() else { return };
        let length = if playing.state.duration > 0.0 { playing.state.duration } else { playing.length };
        let to = if length > 0.0 { seconds.clamp(0.0, length) } else { seconds.max(0.0) };
        match &playing.video {
            Some(video) => video.seek_to(to),
            None if playing.ticker.is_none() => {}
            None => {
                let at = audio_position(&playing.artifact).unwrap_or(0.0);
                let delta = (to - at).round() as i64;
                let length = (length > 0.0).then(|| Duration::from_secs_f64(length));
                platform::audio::with(|audio| audio.seek_media(&playing.artifact, delta, length));
            }
        }
    });
    follow_audio();
}

/// Moves `seconds` forward (back when negative).
pub fn seek_by(seconds: f64) {
    let at = NOW.with(|now| {
        now.borrow().as_ref().map(|p| if p.ticker.is_some() { audio_position(&p.artifact).unwrap_or(0.0) } else { p.state.position })
    });
    if let Some(at) = at {
        seek_to(at + seconds);
    }
}

pub fn toggle_mute() {
    NOW.with(|now| {
        let now = now.borrow();
        if let Some(playing) = now.as_ref()
            && let Some(video) = &playing.video
        {
            video.set_muted(!playing.state.muted);
        }
    });
}

pub fn set_volume(volume: f64) {
    NOW.with(|now| {
        let now = now.borrow();
        if let Some(video) = now.as_ref().and_then(|p| p.video.as_ref()) {
            video.set_volume(volume);
            video.set_muted(false);
        }
    });
}

fn nudge_volume(delta: f64) {
    let volume = NOW.with(|now| now.borrow().as_ref().map(|p| p.state.volume));
    if let Some(volume) = volume {
        set_volume((volume + delta).clamp(0.0, 100.0));
    }
}

/// The picture fills the whole window, or goes back into the viewer.
pub fn toggle_fullscreen() {
    let Some(window) = crate::window() else { return };
    let Some(was) = NOW.with(|now| now.borrow().as_ref().filter(|p| p.kind == Kind::Video).map(|p| p.was_fullscreen)) else { return };
    let bridge = window.global::<PlaybackBridge>();
    let full = !bridge.get_fullscreen();
    bridge.set_fullscreen(full);
    if full || !was {
        window.window().set_fullscreen(full);
    }
}

/// A video about to sound: speech and a chat's audio clip stop.
fn quiet_other_sound() {
    platform::audio::with(|audio| {
        audio.silence();
        if let Some(artifact) = audio.media().map(|m| m.artifact.clone()) {
            audio.stop_media(&artifact);
        }
    });
}

/// Where the audio item is, when it is the one the audio plays.
fn audio_position(artifact: &str) -> Option<f64> {
    platform::audio::with(|audio| audio.media().filter(|m| m.artifact == artifact).map(|m| m.position().as_secs_f64())).flatten()
}

/// The audio item's state into the bridge.
fn follow_audio() {
    let Some(window) = crate::window() else { return };
    let Some((artifact, length)) = NOW.with(|now| now.borrow().as_ref().filter(|p| p.ticker.is_some()).map(|p| (p.artifact.clone(), p.length))) else { return };
    use platform::audio::MediaState;
    let media = platform::audio::with(|audio| audio.media().filter(|m| m.artifact == artifact).map(|m| (m.state.clone(), m.position().as_secs_f64()))).flatten();
    let bridge = window.global::<PlaybackBridge>();
    let (playing, preparing, message, at) = match media {
        Some((MediaState::Playing, at)) => (true, false, String::new(), at),
        Some((MediaState::Paused, at)) => (false, false, String::new(), at),
        Some((MediaState::Preparing, _)) => (false, true, String::new(), 0.0),
        Some((MediaState::Failed(why), _)) => (false, false, why, 0.0),
        None => (false, false, String::new(), 0.0),
    };
    let at = if length > 0.0 { at.min(length) } else { at };
    bridge.set_playing(playing);
    bridge.set_preparing(preparing);
    bridge.set_message(message.into());
    bridge.set_position(at as f32);
}

/// Hands the player's updates for item `generation` to the UI thread: the
/// state as it comes, and of the pictures only the newest not yet shown.
fn deliverer(generation: u64) -> impl Fn(mpv::Update) + Send + Sync + 'static {
    let waiting: Arc<Mutex<Option<slint::SharedPixelBuffer<slint::Rgba8Pixel>>>> = Arc::default();
    move |update| match update {
        mpv::Update::State(state) => {
            let _ = slint::invoke_from_event_loop(move || take_state(generation, state));
        }
        mpv::Update::Frame(buffer) => {
            let first = waiting.lock().expect("frame slot").replace(buffer).is_none();
            if first {
                let waiting = waiting.clone();
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(buffer) = waiting.lock().expect("frame slot").take() {
                        take_frame(generation, buffer);
                    }
                });
            }
        }
    }
}

fn current(generation: u64) -> bool {
    NOW.with(|now| now.borrow().as_ref().is_some_and(|p| p.generation == generation))
}

fn take_frame(generation: u64, buffer: slint::SharedPixelBuffer<slint::Rgba8Pixel>) {
    if !current(generation) {
        return;
    }
    let Some(window) = crate::window() else { return };
    let bridge = window.global::<PlaybackBridge>();
    bridge.set_frame(slint::Image::from_rgba8(buffer));
    bridge.set_has_frame(true);
}

fn take_state(generation: u64, state: mpv::State) {
    let length = NOW.with(|now| {
        let mut now = now.borrow_mut();
        let playing = now.as_mut().filter(|p| p.generation == generation)?;
        playing.state = state.clone();
        Some(playing.length)
    });
    let Some(length) = length else { return };
    let Some(window) = crate::window() else { return };
    let bridge = window.global::<PlaybackBridge>();
    bridge.set_playing(!state.paused && !state.ended && state.failed.is_none());
    bridge.set_preparing(state.failed.is_none() && (!state.loaded || state.buffering));
    bridge.set_position(state.position as f32);
    bridge.set_duration((if state.duration > 0.0 { state.duration } else { length }) as f32);
    bridge.set_volume(state.volume as f32);
    bridge.set_muted(state.muted);
    bridge.set_message(state.failed.unwrap_or_default().into());
}
