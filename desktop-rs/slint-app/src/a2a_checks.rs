//! `--check a2a`: a message another agent sent into a chat shows folded to
//! one line, "Rachel prompted · <first line>", by the keyboard: K reaches
//! it, O opens it to the whole message and folds it again, a click does the
//! same, nothing moves under a reader who scrolled up, a follower stays at
//! the end, and it stays open when the chat is presented again. Shot in a
//! light and a dark theme.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::Model;

use super::scroll_checks::{anchor_moved, last_row_visible, place_of, row_height, wheel_up};
use super::{Stage, app_now, check, click_at, control, report, rows, run_stages, shot};
use crate::headless;

const KEY: &str = "a2a:u7";

fn row(id: &str) -> Option<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().find(|r| r.id == id)
}

fn open() -> bool {
    row("u7").is_some_and(|r| r.prompt.expanded)
}

fn hints() -> Vec<(String, String)> {
    crate::artifacts_view::selected_hints(&app_now()).unwrap_or_default()
}

fn turns() -> serde_json::Value {
    let mut turns = vec![json!({"id": "u3", "role": "user", "origin": "agent", "sender_name": "Mike", "sender_agent_id": "a2", "text": "Check the deploy"})];
    for i in 0..24 {
        let role = if i % 2 == 0 { "user" } else { "assistant" };
        turns.push(json!({"id": format!("f{i}"), "role": role, "text": format!("Row {i}: the build runs the unit tests first, then the integration suite.")}));
    }
    turns.push(json!({"id": "u7", "role": "user", "origin": "agent", "sender_name": "Rachel", "sender_agent_id": "a1",
        "text": "## Review the parser change\n\nRead `src/tokenizer.ts` and its tests, then report back.\n\n```bash\nnpm test -- tokenizer\n```\n\n- keep the main checkout untouched\n- say what failed"}));
    turns.push(json!({"id": "f-reply", "role": "assistant", "text": "On it."}));
    turns.push(json!({"id": "f-own", "role": "user", "text": "Thanks, both\nof you"}));
    json!({"session": "rachel", "turns": turns})
}

pub fn a2a_check(out: String) {
    let anchor: Rc<RefCell<Option<(String, f32)>>> = Rc::default();
    let (anchor1, anchor2, anchor3) = (anchor.clone(), anchor.clone(), anchor.clone());
    let (out1, out2, out3, out4) = (out.clone(), out.clone(), out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            app.engine.borrow_mut().set_reading_theme("paper");
            check(control("/__control/turns", &turns()).is_ok(), "the Host takes a chat with two agents' prompts");
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("folded", Box::new(move |_, _, elapsed| {
            if row("u7").is_none() || elapsed < Duration::from_millis(1200) {
                return false;
            }
            let prompt = row("u7").map(|r| r.prompt).unwrap_or_default();
            check(prompt.key == KEY && !prompt.expanded, &format!("another agent's message is folded by default: {prompt:?}"));
            check(prompt.sender == "Rachel" && prompt.line == "Review the parser change", &format!("its line is the sender and the first line: {prompt:?}"));
            let height = row_height("u7");
            check(height > 0.0 && height <= 36.0, &format!("folded, it is one line tall ({height:.1}px)"));
            let older = row("u3").map(|r| r.prompt).unwrap_or_default();
            check(older.key == "a2a:u3" && !older.expanded, "every agent's prompt folds, not only the latest");
            check(row("f-own").is_some_and(|r| r.prompt.key.is_empty()), "the user's own message is not a prompt");
            check(report().follows && report().at_end, "the chat opens at its latest message");
            shot(&out1, "a2a-01-folded-light");
            app_now().focus_transcript();
            true
        })),
        ("transcript focused", Box::new(|_, _, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press("k");
            true
        })),
        ("selected", Box::new(|app, _, elapsed| {
            if crate::artifacts_view::selected(app).as_deref() != Some(KEY) {
                return elapsed > Duration::from_secs(3) && {
                    check(false, &format!("K reaches the prompt: {:?}", app.artifact_cursor.borrow()));
                    true
                };
            }
            check(hints().iter().any(|(k, l)| k == "O" && l == "Expand"), &format!("the selected prompt names its key, O Expand: {:?}", hints()));
            headless::press("o");
            true
        })),
        ("expanded", Box::new(move |_, _, elapsed| {
            if !open() || elapsed < Duration::from_millis(800) {
                return false;
            }
            let kinds: Vec<String> = row("u7").map(|r| r.blocks.iter().map(|b| b.kind.to_string()).collect()).unwrap_or_default();
            check(kinds.iter().any(|k| k == "heading") && kinds.iter().any(|k| k == "code"), &format!("O opens it to the whole message as Markdown: {kinds:?}"));
            let height = row_height("u7");
            check(height > 120.0, &format!("opened, it shows every line ({height:.1}px)"));
            check(hints().iter().any(|(k, l)| k == "O" && l == "Collapse"), &format!("and its key now folds it: {:?}", hints()));
            let (visible, detail) = last_row_visible();
            check(report().follows && visible, &format!("a follower stays at the end: {detail}"));
            shot(&out2, "a2a-02-open-light");
            wheel_up(160.0);
            true
        })),
        ("reader up", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(1000) {
                return false;
            }
            check(!report().follows, "the reader scrolled up");
            *anchor1.borrow_mut() = place_of("u7");
            check(anchor1.borrow().is_some(), "the prompt is on screen");
            headless::press("o");
            true
        })),
        ("folds in place", Box::new(move |_, _, elapsed| {
            if open() || elapsed < Duration::from_millis(800) {
                return false;
            }
            let (still, detail) = anchor2.borrow().as_ref().map_or((false, "no anchor".into()), anchor_moved);
            check(still, &format!("O folds it without moving the reader: {detail}"));
            check(!report().follows, "and the reader is not pulled to the end");
            headless::press("o");
            true
        })),
        ("opens in place", Box::new(move |app, _, elapsed| {
            if !open() || elapsed < Duration::from_millis(800) {
                return false;
            }
            let (still, detail) = anchor3.borrow().as_ref().map_or((false, "no anchor".into()), anchor_moved);
            check(still, &format!("O opens it without moving the reader: {detail}"));
            let Some((x, y, _, _)) = crate::artifacts_view::card_rect(app, KEY) else {
                check(false, "the prompt reports where it is");
                return true;
            };
            click_at(&crate::window().expect("the window"), x + 40.0, y + 10.0);
            true
        })),
        ("a click folds it", Box::new(move |app, _, elapsed| {
            if open() {
                return elapsed > Duration::from_secs(3) && {
                    check(false, "a click on its line folds the prompt");
                    true
                };
            }
            check(true, "a click on its line folds the prompt");
            headless::press(slint::platform::Key::End);
            app.engine.borrow_mut().set_reading_theme("night");
            crate::pump();
            true
        })),
        ("dark", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(1500) {
                return false;
            }
            check(report().follows, "End follows again");
            shot(&out3, "a2a-03-folded-dark");
            headless::press("k");
            true
        })),
        ("dark selected", Box::new(|app, _, elapsed| {
            if crate::artifacts_view::selected(app).as_deref() != Some(KEY) {
                return elapsed > Duration::from_secs(3) && {
                    check(false, &format!("K reaches the prompt again: {:?}", app.artifact_cursor.borrow()));
                    true
                };
            }
            headless::press("o");
            true
        })),
        ("dark open", Box::new(move |app, _, elapsed| {
            if !open() || elapsed < Duration::from_millis(1200) {
                return false;
            }
            shot(&out4, "a2a-04-open-dark");
            app.engine.borrow_mut().select("mike");
            crate::pump();
            true
        })),
        ("away", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("back", Box::new(|_, _, elapsed| {
            if row("u7").is_none() || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(open(), "the prompt stays open when the chat is shown again");
            check(row("u3").is_some_and(|r| !r.prompt.expanded), "and the other prompt stays folded");
            true
        })),
    ];
    run_stages(stages);
}
