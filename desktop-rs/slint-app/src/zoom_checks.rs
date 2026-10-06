//! `--check chat-zoom --out DIR`: the chat zooms apart from the window
//! (Peter, 2026-10-06: "zoom in and out dynamically so the heights of the
//! conversation rows change"). Ctrl+= and Ctrl+wheel zoom in, Ctrl+- out,
//! Ctrl+K "Reset zoom" back to 100%, a step at a time with a brief "120%"
//! indicator; at every step the rows tile (the row-overlap watch runs all
//! along), a reader in the middle of a long chat keeps the message they were
//! reading at the top, and a reader at the end stays at the end. The second
//! pass (`restart`) starts again on the same settings: the zoom is kept.

use std::time::Duration;

use serde_json::json;
use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, report, row_overlap_checks, run_stages, shot};
use crate::headless;

const STEP: Duration = Duration::from_millis(900);

fn zoom(window: &crate::AppWindow) -> f32 {
    (window.global::<crate::Look>().get_zoom() * 100.0).round()
}

fn stored() -> Option<f64> {
    app_now().engine.borrow().settings().get("conversation/chatZoom").and_then(serde_json::Value::as_f64)
}

/// A chat long enough to read in the middle of: forty turns of prose.
fn history() -> serde_json::Value {
    let turns: Vec<serde_json::Value> = (0..40)
        .map(|i| {
            let role = if i % 2 == 0 { "user" } else { "assistant" };
            let text = if i % 2 == 0 {
                format!("Question {i}: what does the next part of the plan need?")
            } else {
                format!(
                    "Answer {i}. The next part needs the settings to reach the chat, the rows to be measured again at the new size, and the reader to stay on the message they were reading. It wraps over a few lines at the usual size so that zooming changes how many lines it takes.\n\n- one point\n- another point, long enough to wrap at a large zoom"
                )
            };
            json!({"id": format!("z{i:02}"), "role": role, "text": text})
        })
        .collect();
    json!({"session": "rachel", "turns": turns})
}

/// The message across the top of the viewport.
fn anchor() -> Option<String> {
    row_overlap_checks::rows_from_top().first().map(|(id, _)| id.clone())
}

/// The message `id` is still the one being read: among the first two
/// drawn from the top.
fn anchored(id: &str, when: &str) {
    let rows = row_overlap_checks::rows_from_top();
    let top: Vec<&str> = rows.iter().take(2).map(|(id, _)| id.as_str()).collect();
    check(top.contains(&id), &format!("{when}: the reader stays on {id}: top rows {top:?}"));
}

/// The chat's centre, to turn the wheel over.
fn chat_centre() -> (f32, f32) {
    let window = crate::window().expect("window");
    let size = window.window().size().to_logical(window.window().scale_factor());
    (size.width * 0.65, size.height * 0.45)
}

pub fn zoom_check(out: String) {
    if std::env::var("CLARP_CHECK_PASS").as_deref() == Ok("restart") {
        return restart_check();
    }
    let o = move || out.clone();
    let (o1, o2, o3, o4, o5) = (o(), o(), o(), o(), o());
    let held = std::rc::Rc::new(std::cell::RefCell::new(String::new()));
    let (h1, h2, h3, h4, h5) = (held.clone(), held.clone(), held.clone(), held.clone(), held.clone());
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(zoom(window) == 100.0, "the chat starts at 100%");
            check(control("/__control/turns", &history()).is_ok(), "the Host takes a long chat");
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("loaded", Box::new(|app, _, elapsed| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && c.rows().iter().any(|r| r.id == "z39"));
            if !loaded || elapsed < Duration::from_millis(800) {
                return false;
            }
            app.focus_transcript();
            true
        })),
        // Into the middle of it, to read there.
        ("middle", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            for _ in 0..4 {
                headless::press(Key::PageUp);
            }
            true
        })),
        ("reading", Box::new(move |_, _, elapsed| {
            if elapsed < STEP || report().at_end {
                return false;
            }
            let Some(id) = anchor() else { return false };
            *h1.borrow_mut() = id;
            row_overlap_checks::assert_tiled("at 100%, in the middle of the chat");
            shot(&o1, "chat-zoom-01-100");
            headless::press_with(&[Key::Control], "=");
            true
        })),
        ("110", Box::new(move |_, window, elapsed| {
            if zoom(window) != 110.0 || elapsed < STEP {
                return false;
            }
            check(window.global::<crate::Look>().get_zoom_note() == "110%" || elapsed > Duration::from_secs(2), "a brief 110% shows");
            check(stored() == Some(110.0), "Ctrl+= zooms in a step, and keeps it");
            row_overlap_checks::assert_tiled("at 110%");
            anchored(&h2.borrow(), "Ctrl+= to 110%");
            check(!report().at_end, "and does not jump to the end");
            // Ctrl+wheel up over the chat zooms in too.
            let (x, y) = chat_centre();
            headless::pointer_move(x, y);
            headless::hold(Key::Control);
            headless::wheel(x, y, 60.0);
            headless::release(Key::Control);
            true
        })),
        ("120", Box::new(move |_, window, elapsed| {
            if zoom(window) != 120.0 || elapsed < STEP {
                return false;
            }
            check(true, "Ctrl+wheel up zooms in a step");
            row_overlap_checks::assert_tiled("at 120%");
            anchored(&h3.borrow(), "Ctrl+wheel to 120%");
            shot(&o2, "chat-zoom-02-120");
            headless::press_with(&[Key::Control], "-");
            headless::press_with(&[Key::Control], "-");
            headless::press_with(&[Key::Control], "-");
            true
        })),
        ("90", Box::new(move |_, window, elapsed| {
            if zoom(window) != 90.0 || elapsed < STEP {
                return false;
            }
            check(true, "Ctrl+- zooms out a step at a time");
            row_overlap_checks::assert_tiled("at 90%");
            anchored(&h4.borrow(), "Ctrl+- to 90%");
            // Ctrl+wheel down zooms out.
            let (x, y) = chat_centre();
            headless::hold(Key::Control);
            headless::wheel(x, y, -60.0);
            headless::release(Key::Control);
            true
        })),
        ("80", Box::new(move |_, window, elapsed| {
            if zoom(window) != 80.0 || elapsed < STEP {
                return false;
            }
            check(true, "Ctrl+wheel down zooms out");
            row_overlap_checks::assert_tiled("at 80%");
            anchored(&h5.borrow(), "Ctrl+wheel to 80%");
            shot(&o3, "chat-zoom-03-80");
            // A plain wheel turn still scrolls (no Ctrl): the zoom stays.
            let (x, y) = chat_centre();
            headless::wheel(x, y, 60.0);
            true
        })),
        ("scrolled", Box::new(|_, window, elapsed| {
            if elapsed < STEP {
                return false;
            }
            check(zoom(window) == 80.0, "a wheel turn without Ctrl scrolls and leaves the zoom");
            crate::commands::run(&app_now(), window, "jump-latest");
            true
        })),
        ("at the end", Box::new(|_, _, elapsed| {
            if !report().at_end || elapsed < STEP {
                return false;
            }
            headless::press_with(&[Key::Control], "=");
            headless::press_with(&[Key::Control], "=");
            headless::press_with(&[Key::Control], "=");
            true
        })),
        ("follower", Box::new(move |_, window, elapsed| {
            if zoom(window) != 110.0 || elapsed < STEP {
                return false;
            }
            check(report().at_end, "a reader at the end stays at the end as the rows grow");
            row_overlap_checks::assert_tiled("at 110%, at the end");
            shot(&o4, "chat-zoom-04-end-110");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("ctrl+k", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("reset zoom");
            true
        })),
        ("reset row", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "reset zoom" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let rows: Vec<crate::SwitcherRow> = window.get_switcher_rows().iter().collect();
            let first = rows.iter().find(|r| r.kind == "command").cloned().unwrap_or_default();
            check(first.label == "Reset zoom" && first.key == "Ctrl+0", &format!("Ctrl+K has Reset zoom with its key: {:?} {:?}", first.label, first.key));
            shot(&o5, "chat-zoom-05-ctrlk-reset");
            if let Some(i) = rows.iter().position(|r| r.label == "Reset zoom") {
                window.invoke_switcher_moved(i as i32);
            }
            headless::press(Key::Return);
            true
        })),
        ("reset", Box::new(|_, window, elapsed| {
            if zoom(window) != 100.0 || window.get_switcher_open() || elapsed < STEP {
                return false;
            }
            check(stored().is_none(), "Reset zoom goes back to 100% and keeps nothing");
            check(report().at_end, "still at the end");
            row_overlap_checks::assert_tiled("back at 100%");
            // 130% for the restart to find.
            for _ in 0..3 {
                headless::press_with(&[Key::Control], "=");
            }
            true
        })),
        ("kept", Box::new(|_, window, elapsed| {
            if zoom(window) != 130.0 || elapsed < STEP {
                return false;
            }
            check(stored() == Some(130.0), "130% is saved for the restart");
            check(window.window().scale_factor() == 1.0, "and the window's own scale never moved");
            true
        })),
    ];
    run_stages(stages);
}

/// The second pass: the same settings, a new app.
fn restart_check() {
    let stages: Vec<Stage> = vec![("restarted", Box::new(|app, window, elapsed| {
        let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
        if !open || elapsed < STEP {
            return false;
        }
        check(zoom(window) == 130.0, &format!("the zoom survives a restart: {}%", zoom(window)));
        row_overlap_checks::assert_tiled("at 130% after a restart");
        true
    }))];
    run_stages(stages);
}
