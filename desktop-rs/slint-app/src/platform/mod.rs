//! What the window offers the desktop, apart from drawing: voice replies
//! and dictation, the media-player controls (MPRIS), the tray and
//! notifications, presence, one process per desktop, and diagnostics.
//! `audio_output`, `audio_input`, `audio_coordinator` and `mpris` are the
//! Qt Rust app's (desktop-rs/app/src), unchanged but for the runtime.

pub mod audio;
pub mod audio_coordinator;
pub mod audio_input;
pub mod audio_output;
pub mod desktop;
pub mod diagnostics;
pub mod stall_monitor;
pub mod mpris;
pub mod runtime;

use std::cell::RefCell;

thread_local! {
    /// The media player on the session bus, once it is served.
    static MPRIS: RefCell<Option<mpris::Mpris>> = const { RefCell::new(None) };
}

/// Serves the MPRIS player: media keys and the panel's controls pause,
/// resume and stop speech; Raise shows the window; Quit closes it.
pub fn serve_mpris() {
    let status = audio::with(|audio| audio.playback_state()).unwrap_or_default();
    runtime::handle().spawn(async move {
        let served = mpris::Mpris::serve(status, |request| {
            if let Err(error) = slint::invoke_from_event_loop(move || mpris_request(request)) {
                eprintln!("clarp-slint: dropped an MPRIS request: {error}");
            }
        })
        .await;
        match served {
            Ok(player) => {
                if let Err(error) = slint::invoke_from_event_loop(move || MPRIS.with(|slot| *slot.borrow_mut() = Some(player))) {
                    eprintln!("clarp-slint: MPRIS started after the window closed: {error}");
                }
            }
            Err(error) => eprintln!("clarp-slint: MPRIS is not available: {error}"),
        }
    });
}

fn mpris_request(request: mpris::Request) {
    use slint::ComponentHandle;
    match request {
        mpris::Request::Raise => {
            if let Some(window) = crate::window()
                && let Err(error) = window.show()
            {
                eprintln!("clarp-slint: cannot raise the window: {error}");
            }
        }
        mpris::Request::Quit => {
            if let Err(error) = slint::quit_event_loop() {
                eprintln!("clarp-slint: {error}");
            }
        }
        mpris::Request::Playback(action) => {
            audio::with(|audio| audio.playback_command(action));
        }
    }
}

/// Tells the panel what speech is doing now.
pub fn publish_playback() {
    let Some(status) = audio::with(|audio| audio.playback_state()) else { return };
    MPRIS.with(|slot| {
        if let Some(player) = slot.borrow().as_ref() {
            player.publish(status);
        }
    });
}
