//! An agent's publications (`clarp_engine::publications`): everything it
//! published, newest first, a kind at a time if chosen. Images open in the
//! full-size viewer, which moves between the list's images; recordings and
//! films play in the in-app player (`playback`), from the agent's own Host
//! with that Host's token; everything else opens as its card would
//! (`artifacts_view::open`): a report in the report viewer, a form in the
//! browser, a file downloaded and opened, a decision answered in its chat.
//!
//! The panel is its own overlay. Opened from the profile, closing it goes
//! back there; a viewer opened from it comes back to it.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use clarp_engine::Change;
use clarp_engine::publications::{Kind, Publication};
use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::{App, AppWindow, MessageImage, PublicationFilter, PublicationRow, PublicationsBridge, commands};

pub const OVERLAY: &str = "publications";

/// The longest side a list tile's picture is decoded to.
const THUMB_SIDE: u32 = 160;
/// The longest side the viewer's picture is decoded to.
const FULL_SIDE: u32 = 2560;
/// Rows PageUp and PageDown move.
const PAGE_ROWS: i32 = 8;

#[derive(Default)]
struct Panel {
    session: String,
    /// The key the keyboard is on (kept across pages and reloads).
    selected: String,
    /// It was opened over the profile: closing goes back there.
    from_profile: bool,
    /// A viewer it opened (a report) comes back to it when closed.
    returning: bool,
    /// What the list shows now, in order.
    shown: Vec<Publication>,
    /// The viewer's images (keys), and which one shows.
    viewer: Vec<String>,
}

/// A decoded picture, or its decode still running.
#[derive(Clone)]
enum Picture {
    Decoding,
    Ready(slint::Image),
    Failed,
}

thread_local! {
    static PANEL: RefCell<Panel> = RefCell::default();
    /// (file URL, side) -> picture.
    static PICTURES: RefCell<HashMap<(String, u32), Picture>> = RefCell::default();
}

pub fn is_open(app: &App) -> bool {
    *app.overlay.borrow() == OVERLAY
}

pub fn session() -> String {
    PANEL.with(|p| p.borrow().session.clone())
}

/// Opens `session`'s publications from the top.
pub fn open(app: &Rc<App>, window: &AppWindow, session: &str) {
    if session.is_empty() || session.starts_with("pair:") {
        return;
    }
    let from_profile = *app.overlay.borrow() == "profile";
    PANEL.with(|p| {
        let mut panel = p.borrow_mut();
        if panel.session != session {
            panel.selected.clear();
        }
        panel.session = session.to_owned();
        panel.from_profile = from_profile;
        panel.returning = false;
        panel.viewer.clear();
    });
    let bridge = window.global::<PublicationsBridge>();
    close_player(window);
    bridge.set_viewing(-1);
    bridge.set_selected(0);
    app.engine.borrow_mut().load_publications(session);
    commands::open_overlay(app, window, OVERLAY);
    crate::pump_now(app);
    fill(app, window);
}

/// Escape: the viewer first, then the panel (back to the profile when it
/// was opened from there).
pub fn close(app: &App, window: &AppWindow) {
    let bridge = window.global::<PublicationsBridge>();
    if bridge.get_playing() {
        close_player(window);
        return;
    }
    if bridge.get_viewing() >= 0 {
        close_image(window);
        return;
    }
    let from_profile = PANEL.with(|p| {
        let mut panel = p.borrow_mut();
        panel.returning = false;
        std::mem::take(&mut panel.from_profile)
    });
    if from_profile && !crate::profile_view::session().is_empty() {
        crate::profile_view::resume(app, window);
    } else {
        commands::close_overlay(app, window);
    }
}

/// A viewer it opened was closed (`commands::close_overlay`): the panel
/// shows again, with the keyboard. False when it opened none.
pub fn give_back(app: &App, window: &AppWindow, closed: &str) -> bool {
    if closed == OVERLAY || !PANEL.with(|p| std::mem::take(&mut p.borrow_mut().returning)) {
        return false;
    }
    // The viewer's own way back to the chat is not taken.
    let _ = crate::artifacts_view::take_return();
    commands::open_overlay(app, window, OVERLAY);
    fill(app, window);
    true
}

/// Brings the open panel up to date.
pub fn refresh(app: &App, window: &AppWindow, changes: &[Change]) {
    if !is_open(app) {
        // Closed some other way: what it played stops with it.
        if *app.overlay.borrow() != "report" {
            close_player(window);
        }
        return;
    }
    // The same agent after a reconnect or a peer Host read anew: its list
    // is read again when this Host lost it or now names another agent.
    if changes.iter().any(|c| matches!(c, Change::Roster | Change::Connection)) {
        let session = session();
        app.engine.borrow_mut().ensure_publications(&session);
    }
    let relevant = changes.iter().any(|c| matches!(c, Change::Publications | Change::Updates | Change::Roster | Change::Connection | Change::Media));
    if relevant {
        // A file or film opened from here lands with the Updates.
        crate::artifacts_view::settle_downloads(app, &session());
        fill(app, window);
    }
}

fn badge(kind: Kind) -> &'static str {
    match kind {
        Kind::Image => "IMG",
        Kind::Video => "VID",
        Kind::Audio => "AUD",
        Kind::Document => "DOC",
        Kind::Data => "DATA",
        Kind::Decision => "ASK",
        Kind::Other => "•••",
    }
}

fn kind_word(item: &Publication) -> String {
    match item.kind {
        Kind::Image => "Image".into(),
        Kind::Video => "Video".into(),
        Kind::Audio => "Audio".into(),
        Kind::Data => "Data".into(),
        Kind::Decision if item.artifact_type == "question" => "Question".into(),
        Kind::Decision => "Decision".into(),
        _ if !item.artifact_type.is_empty() => {
            let words = item.artifact_type.replace('_', " ");
            let mut chars = words.chars();
            chars.next().map(|c| c.to_uppercase().collect::<String>() + chars.as_str()).unwrap_or_default()
        }
        _ => "File".into(),
    }
}

/// When, in the reader's time: "14:05" today, "26 Nov 14:05" this year,
/// "26 Nov 2025" before.
fn when(ms: i64) -> String {
    use chrono::{Datelike, Local, TimeZone};
    let Some(at) = Local.timestamp_millis_opt(ms).single().filter(|_| ms > 0) else { return String::new() };
    let now = Local::now();
    if at.date_naive() == now.date_naive() {
        at.format("%H:%M").to_string()
    } else if at.year() == now.year() {
        at.format("%-d %b %H:%M").to_string()
    } else {
        at.format("%-d %b %Y").to_string()
    }
}

/// A cached image file as a picture at most `side` on its longest side,
/// decoded off the UI thread; None while it decodes (the panel fills again
/// when it lands).
fn picture(url: &str, side: u32) -> Picture {
    if url.is_empty() {
        return Picture::Failed;
    }
    let key = (url.to_owned(), side);
    if let Some(found) = PICTURES.with(|p| p.borrow().get(&key).cloned()) {
        return found;
    }
    PICTURES.with(|p| p.borrow_mut().insert(key.clone(), Picture::Decoding));
    let Some(path) = crate::profile_view::file_path(url) else {
        PICTURES.with(|p| p.borrow_mut().insert(key, Picture::Failed));
        return Picture::Failed;
    };
    crate::platform::runtime::handle().spawn_blocking(move || {
        let pixels = std::fs::read(&path).map_err(|e| e.to_string()).and_then(|bytes| {
            let decoded = image::load_from_memory(&bytes).map_err(|e| e.to_string())?;
            let decoded = if decoded.width().max(decoded.height()) > side { decoded.thumbnail(side, side) } else { decoded };
            let rgba = decoded.into_rgba8();
            Ok(slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), rgba.width(), rgba.height()))
        });
        let landed = slint::invoke_from_event_loop(move || {
            let picture = match pixels {
                Ok(pixels) => Picture::Ready(slint::Image::from_rgba8(pixels)),
                Err(error) => {
                    eprintln!("clarp-slint: cannot show {}: {error}", key.0);
                    Picture::Failed
                }
            };
            PICTURES.with(|p| p.borrow_mut().insert(key, picture));
            if let (Some(app), Some(window)) = (crate::app(), crate::window())
                && is_open(&app)
            {
                fill(&app, &window);
            }
        });
        if let Err(error) = landed {
            eprintln!("clarp-slint: dropped a decoded picture: {error}");
        }
    });
    Picture::Decoding
}

/// The filters: everything (0), then each kind (1-7).
fn filters(chosen: Option<Kind>) -> Vec<PublicationFilter> {
    let mut all = vec![PublicationFilter { label: "All".into(), key: "0".into(), chosen: chosen.is_none() }];
    all.extend(Kind::ALL.iter().enumerate().map(|(index, kind)| PublicationFilter {
        label: kind.label().into(),
        key: (index + 1).to_string().into(),
        chosen: chosen == Some(*kind),
    }));
    all
}

fn fill(app: &App, window: &AppWindow) {
    let session = session();
    if session.is_empty() {
        return;
    }
    let bridge = window.global::<PublicationsBridge>();
    let engine = app.engine.borrow();
    let state = engine.publications_state(&session);
    let shown: Vec<Publication> = engine.publications(&session).iter().filter(|item| state.kind.is_none_or(|kind| item.kind == kind)).cloned().collect();
    let rows: Vec<PublicationRow> = shown
        .iter()
        .map(|item| {
            let source = if item.kind == Kind::Image { engine.publication_image(&session, &item.asset_id).unwrap_or_default().to_owned() } else { String::new() };
            let thumb = if source.is_empty() { None } else { Some(picture(&source, THUMB_SIDE)) };
            let status = if item.artifact_id.is_empty() { "" } else { engine.artifact_status(&item.artifact_id) };
            let detail = [kind_word(item), when(item.created_at), item.summary.clone()].into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" · ");
            PublicationRow {
                key: item.key.as_str().into(),
                kind: item.kind.name().into(),
                badge: badge(item.kind).into(),
                title: item.title.as_str().into(),
                detail: detail.into(),
                status: status.into(),
                has_thumb: matches!(thumb, Some(Picture::Ready(_))),
                thumb_loading: item.kind == Kind::Image && (source.is_empty() || matches!(thumb, Some(Picture::Decoding))),
                thumb: match thumb {
                    Some(Picture::Ready(image)) => image,
                    _ => slint::Image::default(),
                },
            }
        })
        .collect();
    let name = engine.chat_name(&session);
    let footer = if !state.known {
        "Not loaded: this agent's Host is not connected".to_owned()
    } else if state.loading && shown.is_empty() {
        "Loading…".to_owned()
    } else if shown.is_empty() && !state.has_more {
        match state.kind {
            Some(kind) => format!("No {} from this agent", kind.label().to_lowercase()),
            None => "Nothing published by this agent yet".to_owned(),
        }
    } else if state.loading {
        format!("{} shown · loading older…", shown.len())
    } else if state.has_more {
        format!("{} shown · older ones not loaded yet", shown.len())
    } else {
        format!("All {} shown", shown.len())
    };
    // Each list says for itself what failed; End asks again.
    let error = [("Images", &state.media_error), ("Artifacts", &state.artifacts_error)]
        .into_iter()
        .filter(|(_, why)| !why.is_empty())
        .map(|(what, why)| format!("{what} could not be read further: {why}. End tries again."))
        .collect::<Vec<_>>()
        .join("\n");
    drop(engine);

    bridge.set_agent(name.into());
    bridge.set_filters(ModelRc::new(VecModel::from(filters(state.kind))));
    bridge.set_footer(footer.into());
    bridge.set_error(error.into());
    bridge.set_loading(state.loading);
    bridge.set_has_more(state.has_more);
    bridge.set_reduced_motion(crate::profile_view::reduced_motion(app));
    // The keyboard stays on its item as rows arrive above or below it.
    let selected_key = PANEL.with(|p| p.borrow().selected.clone());
    let at = shown.iter().position(|item| item.key == selected_key).map_or_else(|| bridge.get_selected().clamp(0, (rows.len() as i32 - 1).max(0)), |i| i as i32);
    let same = bridge.get_rows().row_count() == rows.len() && bridge.get_rows().iter().zip(&rows).all(|(a, b)| a == *b);
    if !same {
        bridge.set_rows(ModelRc::new(VecModel::from(rows)));
    }
    bridge.set_selected(if shown.is_empty() { -1 } else { at });
    PANEL.with(|p| {
        let mut panel = p.borrow_mut();
        panel.selected = shown.get(at.max(0) as usize).map(|item| item.key.clone()).unwrap_or_default();
        panel.shown = shown;
    });
    if bridge.get_viewing() >= 0 {
        show_images(app, window);
    }
}

fn select(window: &AppWindow, index: i32) {
    let bridge = window.global::<PublicationsBridge>();
    let count = PANEL.with(|p| p.borrow().shown.len()) as i32;
    if count == 0 {
        return;
    }
    let index = index.clamp(0, count - 1);
    bridge.set_selected(index);
    PANEL.with(|p| {
        let mut panel = p.borrow_mut();
        panel.selected = panel.shown.get(index as usize).map(|item| item.key.clone()).unwrap_or_default();
    });
}

fn load_older(app: &App) {
    let session = session();
    app.engine.borrow_mut().load_older_publications(&session);
}

fn choose_kind(app: &App, window: &AppWindow, index: i32) {
    let kind = if index <= 0 { None } else { Kind::ALL.get(index as usize - 1).copied() };
    if index > Kind::ALL.len() as i32 {
        return;
    }
    close_player(window);
    let session = session();
    app.engine.borrow_mut().set_publication_kind(&session, kind);
    window.global::<PublicationsBridge>().set_selected(0);
    PANEL.with(|p| p.borrow_mut().selected.clear());
    fill(app, window);
}

// ---- the image viewer ----------------------------------------------------------

/// The viewer's images: the list's, the shown one decoded at full size and
/// the others when they show.
fn show_images(app: &App, window: &AppWindow) {
    let bridge = window.global::<PublicationsBridge>();
    let session = session();
    let viewing = bridge.get_viewing();
    let keys = PANEL.with(|p| p.borrow().viewer.clone());
    let engine = app.engine.borrow();
    let items: Vec<Publication> = keys.iter().filter_map(|key| engine.publications(&session).iter().find(|item| item.key == *key).cloned()).collect();
    let images: Vec<MessageImage> = items
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let source = engine.publication_image(&session, &item.asset_id).unwrap_or_default().to_owned();
            let picture = if index as i32 == viewing && !source.is_empty() { picture(&source, FULL_SIDE) } else { Picture::Decoding };
            MessageImage {
                url: item.media_path().unwrap_or_default().into(),
                alt: item.title.as_str().into(),
                loaded: matches!(picture, Picture::Ready(_)),
                failed: matches!(picture, Picture::Failed),
                foreign: false,
                image: match picture {
                    Picture::Ready(image) => image,
                    _ => slint::Image::default(),
                },
            }
        })
        .collect();
    drop(engine);
    bridge.set_images(ModelRc::new(VecModel::from(images)));
}

/// Opens the list's image at `index` (of the list) in the viewer, among
/// the list's other images.
fn open_image(app: &App, window: &AppWindow, index: usize) {
    let (keys, at) = PANEL.with(|p| {
        let panel = p.borrow();
        let images: Vec<&Publication> = panel.shown.iter().filter(|item| item.kind == Kind::Image).collect();
        let key = panel.shown.get(index).map(|item| item.key.clone()).unwrap_or_default();
        (images.iter().map(|item| item.key.clone()).collect::<Vec<_>>(), images.iter().position(|item| item.key == key))
    });
    let Some(at) = at else { return };
    PANEL.with(|p| p.borrow_mut().viewer = keys);
    let bridge = window.global::<PublicationsBridge>();
    bridge.set_viewing(at as i32);
    show_images(app, window);
}

fn move_image(app: &App, window: &AppWindow, delta: i32) {
    let bridge = window.global::<PublicationsBridge>();
    let count = PANEL.with(|p| p.borrow().viewer.len()) as i32;
    if count == 0 || bridge.get_viewing() < 0 {
        return;
    }
    let next = (bridge.get_viewing() + delta).clamp(0, count - 1);
    bridge.set_viewing(next);
    // The list's cursor follows the picture.
    let key = PANEL.with(|p| p.borrow().viewer.get(next as usize).cloned()).unwrap_or_default();
    if let Some(row) = PANEL.with(|p| p.borrow().shown.iter().position(|item| item.key == key)) {
        select(window, row as i32);
    }
    show_images(app, window);
}

fn close_image(window: &AppWindow) {
    let bridge = window.global::<PublicationsBridge>();
    bridge.set_viewing(-1);
    bridge.set_images(ModelRc::new(VecModel::from(Vec::<MessageImage>::new())));
    PANEL.with(|p| p.borrow_mut().viewer.clear());
}

// ---- opening -------------------------------------------------------------------

/// Plays a recording or a film in the in-app player, from the agent's own
/// Host (`/media/<asset_id>` read with that Host's token; a paired Host's
/// chat plays from that Host). Only the user's Enter or click gets here,
/// so it starts playing at once.
fn play(app: &App, window: &AppWindow, item: &Publication) {
    let Some(path) = item.media_path() else {
        eprintln!("clarp-slint: {} names no published file to play", item.key);
        return;
    };
    let kind = if item.kind == Kind::Video { crate::playback::Kind::Video } else { crate::playback::Kind::Audio };
    crate::playback::open(
        app,
        crate::playback::Request {
            key: item.key.clone(),
            session: session(),
            kind,
            path,
            title: item.title.clone(),
            poster: None,
            duration_ms: item.duration_ms,
            autoplay: true,
        },
    );
    window.global::<PublicationsBridge>().set_playing(true);
}

/// Ends what the panel plays.
fn close_player(window: &AppWindow) {
    let bridge = window.global::<PublicationsBridge>();
    if bridge.get_playing() {
        crate::playback::close();
        bridge.set_playing(false);
    }
}

/// The window's key (a keyboard-map chord) as the player reads it: the
/// key's text and Shift.
fn player_key(chord: &str) -> Option<(String, bool)> {
    use slint::platform::Key;
    let (shift, key) = chord.strip_prefix("Shift+").map_or((false, chord), |rest| (true, rest));
    let named = |k: Key| slint::SharedString::from(k).to_string();
    let text = match key {
        "Space" => " ".to_owned(),
        "Escape" => named(Key::Escape),
        "Left" => named(Key::LeftArrow),
        "Right" => named(Key::RightArrow),
        "Up" => named(Key::UpArrow),
        "Down" => named(Key::DownArrow),
        "Home" => named(Key::Home),
        "End" => named(Key::End),
        letter if letter.len() == 1 && letter.as_bytes()[0].is_ascii_alphabetic() => letter.to_ascii_lowercase(),
        _ => return None,
    };
    Some((text, shift))
}

/// Enter on the row at `index`.
fn activate(app: &App, window: &AppWindow, index: i32) {
    let Some(item) = PANEL.with(|p| p.borrow().shown.get(index.max(0) as usize).cloned()) else { return };
    select(window, index);
    match item.kind {
        Kind::Image => open_image(app, window, index as usize),
        Kind::Video | Kind::Audio => play(app, window, &item),
        _ if item.artifact_id.is_empty() => {}
        // A decision is answered in its chat, with the keyboard on it.
        Kind::Decision => {
            let session = session();
            PANEL.with(|p| {
                let mut panel = p.borrow_mut();
                panel.from_profile = false;
                panel.returning = false;
            });
            commands::close_overlay(app, window);
            if app.engine.borrow().selected_session() != session {
                app.engine.borrow_mut().select(&session);
            }
            crate::artifacts_view::open(app, window, &item.artifact_id);
        }
        _ => {
            crate::artifacts_view::open(app, window, &item.artifact_id);
            // A viewer over it (a report) comes back here when closed.
            let over = app.overlay.borrow().clone();
            PANEL.with(|p| p.borrow_mut().returning = !over.is_empty() && over != OVERLAY);
        }
    }
    if is_open(app) {
        fill(app, window);
    }
}

// ---- the keyboard --------------------------------------------------------------

/// The hints the shortcut bar shows while the panel has the keyboard.
pub fn hints(window: &AppWindow) -> Vec<crate::Hint> {
    let bridge = window.global::<PublicationsBridge>();
    let pairs: &[(&str, &str)] = if bridge.get_playing() {
        &[("Space", "Play/pause"), ("←/→", "5 s"), ("J/L", "10 s"), ("M", "Mute"), ("F", "Fullscreen"), ("Esc", "Close")]
    } else if bridge.get_viewing() >= 0 {
        &[("←/→", "Previous / next"), ("Esc", "Back")]
    } else {
        &[("J/K", "Move"), ("Enter", "Open"), ("0-7", "Kind"), ("End", "Older"), ("R", "Reload"), ("Esc", "Close")]
    };
    pairs.iter().map(|(keys, label)| crate::Hint { keys: (*keys).into(), label: (*label).into() }).collect()
}

/// A key while the panel shows; false for one it leaves alone.
pub fn key(app: &Rc<App>, window: &AppWindow, chord: &str) -> bool {
    let bridge = window.global::<PublicationsBridge>();
    if bridge.get_playing() {
        // The player's keys; Escape leaves its fullscreen first, then closes it.
        let used = player_key(chord).is_some_and(|(text, shift)| crate::playback::key_pressed(&text, shift));
        if !used && matches!(chord, "Escape" | "Q" | "Backspace") {
            close_player(window);
        }
        if !crate::playback::active() {
            bridge.set_playing(false);
        }
        return true;
    }
    if bridge.get_viewing() >= 0 {
        match chord {
            "Left" | "H" | "K" | "Up" | "PageUp" => move_image(app, window, -1),
            "Right" | "L" | "J" | "Down" | "PageDown" | "Space" => move_image(app, window, 1),
            "Home" => move_image(app, window, -i32::MAX / 2),
            "End" | "Shift+G" => move_image(app, window, i32::MAX / 2),
            "Escape" | "Q" | "Return" | "Backspace" => close_image(window),
            // The viewer keeps every other key.
            _ => {}
        }
        return true;
    }
    let count = PANEL.with(|p| p.borrow().shown.len()) as i32;
    let at = bridge.get_selected();
    let has_more = app.engine.borrow().publications_state(&session()).has_more;
    match chord {
        "Down" | "J" => {
            if at + 1 >= count && has_more {
                load_older(app);
            }
            select(window, at + 1);
        }
        "Up" | "K" => select(window, at - 1),
        "PageDown" => {
            if at + PAGE_ROWS >= count && has_more {
                load_older(app);
            }
            select(window, at + PAGE_ROWS);
        }
        "PageUp" => select(window, at - PAGE_ROWS),
        "Home" | "G" => select(window, 0),
        "End" | "Shift+G" => {
            if has_more {
                load_older(app);
            }
            select(window, count - 1);
        }
        "Return" | "O" | "Space" => activate(app, window, at),
        "R" | "F5" => {
            close_player(window);
            let session = session();
            app.engine.borrow_mut().load_publications(&session);
        }
        "Tab" | "Shift+Tab" => {
            let chosen = app.engine.borrow().publications_state(&session()).kind;
            let now = chosen.and_then(|kind| Kind::ALL.iter().position(|k| *k == kind)).map_or(0, |i| i as i32 + 1);
            let total = Kind::ALL.len() as i32 + 1;
            let next = if chord == "Tab" { (now + 1) % total } else { (now + total - 1) % total };
            choose_kind(app, window, next);
        }
        "Escape" | "Q" | "Backspace" => close(app, window),
        digit if digit.len() == 1 && digit.as_bytes()[0].is_ascii_digit() => {
            let index = i32::from(digit.as_bytes()[0] - b'0');
            if index > Kind::ALL.len() as i32 {
                return false;
            }
            choose_kind(app, window, index);
        }
        _ => return false,
    }
    crate::pump_now(app);
    true
}

/// Connects the panel's commands.
pub fn wire(window: &AppWindow) {
    fn with(act: impl FnOnce(&Rc<App>, &AppWindow)) {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            act(&app, &window);
            crate::pump_now(&app);
        }
    }
    let bridge = window.global::<PublicationsBridge>();
    bridge.on_close(|| with(|app, window| close(app, window)));
    bridge.on_open(|session| with(|app, window| open(app, window, &session)));
    bridge.on_choose_filter(|index| with(|app, window| choose_kind(app, window, index)));
    bridge.on_activate(|index| with(|app, window| activate(app, window, index)));
    bridge.on_load_older(|| with(|app, _| load_older(app)));
    bridge.on_reload(|| {
        with(|app, window| {
            close_player(window);
            let session = session();
            app.engine.borrow_mut().load_publications(&session);
        })
    });
    bridge.on_move_image(|delta| with(|app, window| move_image(app, window, delta)));
    bridge.on_close_image(|| with(|_, window| close_image(window)));
    bridge.on_close_player(|| with(|_, window| close_player(window)));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filters_number_every_kind_after_all() {
        let all = filters(Some(Kind::Audio));
        assert_eq!(all.len(), Kind::ALL.len() + 1);
        assert_eq!(all[0].key, "0");
        assert!(!all[0].chosen);
        assert_eq!(all.iter().filter(|f| f.chosen).count(), 1);
        assert_eq!(all[3].label, "Audio");
        assert!(all[3].chosen);
    }

    #[test]
    fn a_file_without_an_artifact_type_is_a_file() {
        let item = Publication { kind: Kind::Document, ..Publication::default() };
        assert_eq!(kind_word(&item), "File");
        let item = Publication { kind: Kind::Other, artifact_type: "code_change".into(), ..Publication::default() };
        assert_eq!(kind_word(&item), "Code change");
    }
}
