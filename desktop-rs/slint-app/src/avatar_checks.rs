//! `--check avatars --out DIR`: agent portraits are drawn at the device
//! size they are shown at, with soft (antialiased) round edges, in two
//! themes, at two interface scales and at two avatar sizes: the explorer,
//! the compact explorer, a pair room, the profile header and the overview.
//! Each stage saves the window and a zoomed crop of Rachel's and Mike's
//! portraits (`avatars-STAGE-NAME-zoom.png`) to look at the edges by eye.
//! Needs the fake Host (Rachel's portrait is a flat blue, so its edge can
//! be measured; Mike gets a real one).

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Rect, Stage, check, control, run_stages, shot};
use crate::{OverviewBridge, ProfileBridge};

/// The default (medium) sizes, in logical pixels: explorer row, compact
/// row, profile and overview card.
const ROW: f32 = 44.0;
/// The large sizes.
const LARGE_ROW: f32 = 52.0;
const LARGE_COMPACT: f32 = 38.0;
const LARGE_CARD: f32 = 48.0;
/// How long a stage waits for what the new size or scale should bring,
/// before it measures anyway (and fails, if it never came).
const SETTLE: Duration = Duration::from_millis(2500);

fn side(logical: f32, scale: f32) -> u32 {
    (logical * scale).round() as u32
}

fn scale(window: &crate::AppWindow) -> f32 {
    window.window().scale_factor()
}

fn row_portrait(window: &crate::AppWindow, session: &str) -> u32 {
    window.get_chats().iter().find(|r| r.session == session).map_or(0, |r| r.portrait.size().width)
}

/// The drawn avatar named `name` (an image with that label) that is
/// `logical` px wide, in logical pixels.
fn avatar(name: &str, logical: f32) -> Option<Rect> {
    use i_slint_backend_testing::{AccessibleRole, ElementQuery};
    let (window, name) = (crate::window()?, name.to_owned());
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_role() == Some(AccessibleRole::Image) && e.accessible_label().is_some_and(|l| l == name.as_str()))
        .find_all()
        .into_iter()
        .map(|e| super::rect(&e))
        .find(|r| (r.2 - logical).abs() < 1.5 && r.2 == r.3)
}

/// How many pixels of the drawn avatar in `bounds` (device pixels) blend
/// its flat colour with what is behind it: an antialiased circle has about
/// one per pixel of its outline, an aliased one almost none.
fn blends(frame: &image::RgbaImage, bounds: (u32, u32, u32)) -> usize {
    let (x0, y0, size) = bounds;
    let at = |x: u32, y: u32| frame.get_pixel((x0 + x).min(frame.width() - 1), (y0 + y).min(frame.height() - 1)).0;
    let (outside, inside) = (at(0, 0), at(size / 2, size / 2));
    let channel = (0..3).max_by_key(|c| (i32::from(inside[*c]) - i32::from(outside[*c])).abs()).unwrap_or(0);
    let span = f32::from(inside[channel]) - f32::from(outside[channel]);
    if span.abs() < 40.0 {
        return 0;
    }
    let mut count = 0;
    for y in 0..size {
        for x in 0..size {
            let t = (f32::from(at(x, y)[channel]) - f32::from(outside[channel])) / span;
            if (0.08..0.92).contains(&t) {
                count += 1;
            }
        }
    }
    count
}

/// Saves the window and zoomed crops of the named avatars; returns the
/// frame and the device box of the first one found.
fn capture(out: &str, stage: &str, window: &crate::AppWindow, names: &[&str], logical: f32) -> Option<(image::RgbaImage, (u32, u32, u32))> {
    let name = format!("avatars-{stage}");
    shot(out, &name);
    let frame = image::open(format!("{out}/{name}.png")).ok()?.to_rgba8();
    let scale = scale(window);
    let mut first = None;
    for who in names {
        // Before avatars were images, the explorer row's left end.
        let found = avatar(who, logical);
        let (x, y, w) = found.or_else(|| super::explorer_row(who).map(|r| (r.0 + 8.0, r.1 + 8.0, 40.0, 40.0))).map(|r| (r.0, r.1, r.2))?;
        let device = ((x * scale).round() as u32, (y * scale).round() as u32, (w * scale).round() as u32);
        let margin = 4;
        let (cx, cy) = (device.0.saturating_sub(margin), device.1.saturating_sub(margin));
        let crop = image::imageops::crop_imm(&frame, cx, cy, device.2 + 2 * margin, device.2 + 2 * margin).to_image();
        let zoom = image::imageops::resize(&crop, crop.width() * 6, crop.height() * 6, image::imageops::FilterType::Nearest);
        if let Err(error) = zoom.save(format!("{out}/avatars-{stage}-{}-zoom.png", who.to_lowercase().replace([' ', '&'], ""))) {
            check(false, &format!("saved the zoomed crop of {who}: {error}"));
        }
        if first.is_none() && found.is_some() {
            first = Some(device);
        }
    }
    first.map(|bounds| (frame, bounds))
}

/// Rachel's explorer portrait is `logical` px at this scale, and drawn with
/// soft edges.
fn measure(out: &str, stage: &str, window: &crate::AppWindow, logical: f32) {
    let scale = scale(window);
    let want = side(logical, scale);
    let got = row_portrait(window, "rachel");
    check(got == want, &format!("{stage}: Rachel's portrait is made at its device size, {want} px ({logical} px at {scale}x): {got} px"));
    match capture(out, stage, window, &["Rachel", "Mike"], logical) {
        Some((frame, bounds)) => {
            check(bounds.2.abs_diff(want) <= 1, &format!("{stage}: and drawn {want} px wide: {} px", bounds.2));
            let soft = blends(&frame, bounds);
            check(soft as f32 >= 1.5 * bounds.2 as f32, &format!("{stage}: its round edge is antialiased ({soft} blended pixels round {} px)", bounds.2));
        }
        None => check(false, &format!("{stage}: Rachel's avatar is an image in the explorer")),
    }
}

fn change_setting(window: &crate::AppWindow, id: &str) {
    if let Some(app) = crate::app() {
        crate::settings_view::change(&app, window, id, 1);
    }
}

pub fn avatars_check(out: String) {
    let o = |n: usize| (0..n).map(|_| out.clone()).collect::<Vec<_>>();
    let [o1, o2, o3, o4, o5, o6, o7, o8, o9, o10] = <[String; 10]>::try_from(o(10)).expect("ten");
    // Frames drawn when the monitor's scale last changed.
    let frames = std::rc::Rc::new(std::cell::Cell::new(0u64));
    let frames2 = std::rc::Rc::new(std::cell::Cell::new(0u64));
    let (frames_seen, frames2_at) = (frames.clone(), frames2.clone());
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, window, _| {
            let sessions: Vec<String> = window.get_chats().iter().map(|r| r.session.to_string()).collect();
            if app.engine.borrow().connection_state() != "live" || !sessions.iter().any(|s| s == "mike") {
                return false;
            }
            app.engine.borrow_mut().set_reading_theme("paper");
            let mike = json!({"session": "mike", "set": {"avatar_url": "/static/avatars/jasper.png"}});
            check(control("/__control/agent", &mike).is_ok(), "Mike has a real portrait");
            app.engine.borrow_mut().select("rachel");
            true
        })),
        ("portraits", Box::new(move |_, window, elapsed| {
            if row_portrait(window, "rachel") == 0 || row_portrait(window, "mike") == 0 || elapsed < Duration::from_millis(800) {
                return false;
            }
            measure(&o1, "paper-1x", window, ROW);
            if let Some(app) = crate::app() {
                app.engine.borrow_mut().set_reading_theme("night");
            }
            true
        })),
        ("night", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            measure(&o2, "night-1x", window, ROW);
            crate::platform::desktop::set_ui_scale(1.5);
            true
        })),
        ("scaled", Box::new(move |_, window, elapsed| {
            if (scale(window) - 1.5).abs() > 0.01 || (row_portrait(window, "rachel") != side(ROW, 1.5) && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            measure(&o3, "night-1.5x", window, ROW);
            change_setting(window, "avatar-size");
            true
        })),
        ("large", Box::new(move |app, window, elapsed| {
            if (row_portrait(window, "rachel") != side(LARGE_ROW, 1.5) && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(app.engine.borrow().settings().get("appearance/avatarSize").and_then(|v| v.as_str().map(str::to_owned)).as_deref() == Some("large"), "the avatar size setting is kept as large");
            measure(&o4, "night-1.5x-large", window, LARGE_ROW);
            app.engine.borrow_mut().set_reading_theme("paper");
            let mike = json!({"session": "mike", "set": {"latest_state": "thinking"}});
            check(control("/__control/agent", &mike).is_ok(), "Mike starts working");
            true
        })),
        ("ring", Box::new(move |app, window, elapsed| {
            let working = app.engine.borrow().roster().find("mike").is_some_and(|a| a.busy);
            if !working || elapsed < Duration::from_millis(800) {
                return false;
            }
            measure(&o5, "paper-1.5x-large-working", window, LARGE_ROW);
            change_setting(window, "compact-explorer");
            true
        })),
        ("compact", Box::new(move |_, window, elapsed| {
            if !window.get_explorer_compact() || (row_portrait(window, "rachel") != side(LARGE_COMPACT, 1.5) && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            measure(&o6, "paper-1.5x-large-compact", window, LARGE_COMPACT);
            change_setting(window, "compact-explorer");
            if let Some(app) = crate::app() {
                crate::commands::run(&app, window, "list-rooms");
            }
            true
        })),
        ("pair room", Box::new(move |_, window, elapsed| {
            let room = window.get_rooms().iter().next().map_or(0, |r| r.portrait.size().width);
            if window.get_explorer_compact() || (room != side(LARGE_ROW, 1.5) && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(room == side(LARGE_ROW, 1.5), &format!("a pair room's two portraits are one image at its device size: {room} px"));
            capture(&o7, "paper-1.5x-large-pair", window, &["Rachel & Mike"], LARGE_ROW);
            window.invoke_show_pairs(false);
            if let Some(app) = crate::app() {
                crate::profile_view::open(&app, window, "rachel");
            }
            true
        })),
        ("profile", Box::new(move |_, window, elapsed| {
            let info = window.global::<ProfileBridge>().get_info();
            let want = side(LARGE_CARD, scale(window));
            if window.get_overlay() != "profile" || info.portrait.size().width == 0 || (info.portrait.size().width != want && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(info.portrait.size().width == want, &format!("the profile header's portrait is {want} px: {}", info.portrait.size().width));
            capture(&o8, "paper-1.5x-large-profile", window, &["Rachel"], LARGE_CARD);
            if let Some(app) = crate::app() {
                crate::overview_view::open(&app, window);
            }
            true
        })),
        ("overview", Box::new(move |_, window, elapsed| {
            let cards: Vec<crate::OverviewCard> = window.global::<OverviewBridge>().get_cards().iter().collect();
            let want = side(LARGE_CARD, scale(window));
            let sizes: Vec<u32> = cards.iter().map(|c| c.portrait.size().width).collect();
            if window.get_overlay() != "overview" || sizes.len() < 2 || (sizes.iter().any(|s| *s != want) && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(sizes.iter().all(|s| *s == want), &format!("the overview's portraits are {want} px: {sizes:?}"));
            capture(&o9, "paper-1.5x-large-overview", window, &["Rachel", "Mike"], LARGE_CARD);
            crate::platform::desktop::set_ui_scale(1.0);
            true
        })),
        ("back to 1x", Box::new(move |_, window, elapsed| {
            let want = side(LARGE_CARD, 1.0);
            let sizes: Vec<u32> = window.global::<OverviewBridge>().get_cards().iter().map(|c| c.portrait.size().width).collect();
            if (scale(window) - 1.0).abs() > 0.01 || (sizes.iter().any(|s| *s != want) && elapsed < SETTLE) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(sizes.iter().all(|s| *s == want), &format!("back at 1x, the portraits are made again at {want} px: {sizes:?}"));
            capture(&o10, "paper-1x-large-overview", window, &["Rachel", "Mike"], LARGE_CARD);
            // An external display of another scale, as when one is plugged in
            // on a Mac: the window keeps its pixels until the window system
            // resizes it. Before `desktop::rescale` the content kept its
            // logical size, outgrew the renderer's buffer and panicked.
            frames.set(crate::headless::frames_drawn());
            crate::platform::desktop::monitor_scale_changed(2.3);
            true
        })),
        ("another monitor", Box::new(move |_, window, elapsed| {
            if (scale(window) - 2.3).abs() > 0.01 || crate::headless::frames_drawn() <= frames_seen.get() || elapsed < Duration::from_millis(400) {
                return elapsed > Duration::from_secs(5) && {
                    check(false, &format!("a 2.3x monitor draws: scale {}, {} frames since", scale(window), crate::headless::frames_drawn() - frames_seen.get()));
                    true
                };
            }
            check(true, "a 2.3x monitor draws the window in the pixels it has");
            frames2.set(crate::headless::frames_drawn());
            crate::platform::desktop::monitor_scale_changed(1.0);
            true
        })),
        ("monitor back", Box::new(move |_, window, elapsed| {
            if (scale(window) - 1.0).abs() > 0.01 || crate::headless::frames_drawn() <= frames2_at.get() || elapsed < Duration::from_millis(400) {
                return elapsed > Duration::from_secs(5) && {
                    check(false, &format!("back on a 1x monitor it draws: scale {}", scale(window)));
                    true
                };
            }
            check(true, "back on a 1x monitor it draws at 1x");
            true
        })),
    ];
    run_stages(stages);
}
