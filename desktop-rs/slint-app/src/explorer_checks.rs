//! `--check explorer`: the explorer over a fleet of 180 agents makes only
//! the rows in view (a frame walks a screenful, not the fleet), and the
//! keyboard's cursor stays in view as it moves: J down past the last row
//! in view, a jump to the last agent and back to the first, K up again.
//! Needs the fake Host with `--roster 180`.

use std::cell::Cell;
use std::collections::HashSet;
use std::rc::Rc;
use std::time::Duration;

use slint::Model;
use slint::platform::Key;

use super::{Rect, Stage, check, explorer_row, rect, report, run_stages, shot, within};
use crate::headless;

/// The explorer's list, as drawn.
fn explorer_list() -> Option<Rect> {
    use i_slint_backend_testing::{AccessibleRole, ElementQuery};
    let window = crate::window()?;
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_role() == Some(AccessibleRole::List) && e.size().width > 0.0)
        .find_all()
        .into_iter()
        .map(|e| rect(&e))
        .min_by(|a, b| a.0.total_cmp(&b.0))
}

/// Explorer rows the window holds (made by the list), of `names`.
fn rows_made(names: &HashSet<String>) -> usize {
    use i_slint_backend_testing::{AccessibleRole, ElementQuery};
    let Some(window) = crate::window() else { return 0 };
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_role() == Some(AccessibleRole::ListItem))
        .find_all()
        .into_iter()
        .filter(|e| e.accessible_label().is_some_and(|l| names.contains(l.as_str())))
        .count()
}

fn name_of(window: &crate::AppWindow, session: &str) -> String {
    window.get_chats().iter().find(|c| c.session == session).map(|c| c.name.to_string()).unwrap_or_default()
}

/// The cursor's row is drawn wholly inside the list.
fn cursor_in_view(window: &crate::AppWindow) -> bool {
    let name = name_of(window, &window.get_sidebar_cursor());
    match (explorer_row(&name), explorer_list()) {
        (Some(row), Some(list)) => within(&row, &list),
        _ => false,
    }
}

/// A stage that waits (up to a second) for the cursor's row to be in view
/// after `what`, then runs `next`.
fn in_view(name: &'static str, what: &'static str, next: impl Fn(&crate::AppWindow) + 'static) -> Stage {
    (name, Box::new(move |_, window, elapsed| {
        if !cursor_in_view(window) && elapsed < Duration::from_secs(1) {
            return false;
        }
        let cursor = window.get_sidebar_cursor();
        check(cursor_in_view(window), &format!("{what}: the cursor's row ({cursor}) is in view"));
        next(window);
        true
    }))
}

pub fn explorer_check(out: String) {
    let pressed = Rc::new(Cell::new(0usize));
    let pressed2 = pressed.clone();
    // When the last J went (the stage's clock).
    let at = Rc::new(Cell::new(Duration::ZERO));
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let ready = app.engine.borrow().roster().agents().len() >= 180 && report().composer_focused;
            if ready {
                headless::press_with(&[Key::Control], "e");
            }
            ready
        })),
        ("explorer", Box::new(|_, window, elapsed| {
            if !window.get_sidebar_focused() || elapsed < Duration::from_millis(300) {
                return false;
            }
            let names: HashSet<String> = window.get_chats().iter().map(|c| c.name.to_string()).collect();
            let made = rows_made(&names);
            check(
                names.len() >= 150 && made > 0 && made < 60,
                &format!("the explorer makes only the rows in view: {made} of {} chats", names.len()),
            );
            check(cursor_in_view(window), "the cursor starts in view");
            true
        })),
        ("down", Box::new(move |_, window, elapsed| {
            // J forty times, each one's row in view before the next.
            if pressed.get() > 0 && !cursor_in_view(window) {
                if elapsed.saturating_sub(at.get()) < Duration::from_secs(1) {
                    return false;
                }
                check(false, &format!("J {}: the cursor's row ({}) is in view", pressed.get(), window.get_sidebar_cursor()));
                return true;
            }
            if pressed.get() == 40 {
                check(true, "J forty times: each time the cursor's row comes into view");
                return true;
            }
            pressed.set(pressed.get() + 1);
            at.set(elapsed);
            headless::press("j");
            false
        })),
        ("jump to the last", Box::new(|_, window, _| {
            let last = window.get_chats().iter().last().map(|c| c.session.to_string()).unwrap_or_default();
            window.set_sidebar_cursor(last.as_str().into());
            true
        })),
        in_view("last", "the cursor jumps to the last agent", |window| {
            let first = window.get_chats().row_data(0).map(|c| c.session.to_string()).unwrap_or_default();
            window.set_sidebar_cursor(first.as_str().into());
        }),
        in_view("first", "and back to the first", |_| {}),
        ("five down", Box::new(move |_, window, _| {
            for _ in 0..pressed2.get().min(5) {
                headless::press("j");
            }
            check(window.get_sidebar_focused(), "the explorer keeps the keyboard");
            true
        })),
        in_view("j from the top", "J from the top", |_| headless::press("k")),
        in_view("k", "K", |_| {}),
        ("shot", Box::new(move |_, _, _| {
            shot(&out, "explorer-long");
            true
        })),
    ];
    run_stages(stages);
}
