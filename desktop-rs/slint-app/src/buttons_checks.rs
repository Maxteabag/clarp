//! `--check composer-buttons`: the chat's action buttons do what their keys
//! do. Send shows only with something to send and sends it, as Enter does;
//! Stop shows while the agent works and stops it, as Ctrl+. does; scrolled
//! up from the end a jump-to-latest button floats over the chat (no row
//! moves when it comes) and brings the chat back to its end, where it goes.
//! Needs the fake Host.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;

use super::{Stage, app_now, check, control, posts, report, run_stages, sends, shot};
use crate::headless;

fn element(id: &str) -> Option<i_slint_backend_testing::ElementHandle> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
}

fn drawn(id: &str) -> bool {
    element(id).is_some()
}

/// Presses the element as a screen reader or a click would.
fn press(id: &str) -> bool {
    element(id).map(|e| e.invoke_accessible_default_action()).is_some()
}

/// The open chat's box in the window: (x, y, width, height).
fn chat() -> Option<(f32, f32, f32, f32)> {
    let e = element(&format!("chat:{}", app_now().active_id()))?;
    let (at, size) = (e.absolute_position(), e.size());
    Some((at.x, at.y, size.width, size.height))
}

fn jump_id() -> String {
    format!("jump-latest:{}", app_now().active_id())
}

pub fn buttons_check(out: String) {
    let (out1, out2, out3) = (out.clone(), out.clone(), out);
    let box_at_end: Rc<Cell<Option<(f32, f32, f32, f32)>>> = Rc::new(Cell::new(None));
    let (end1, end2) = (box_at_end.clone(), box_at_end);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, elapsed| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open || !report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!drawn("composer-send"), "an empty composer shows no Send button");
            check(!drawn("composer-stop"), "an idle agent shows no Stop button");
            headless::type_text("sent by the button");
            true
        })),
        ("send shown", Box::new(move |_, _, elapsed| {
            if app_now().active_draft() != "sent by the button" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(drawn("composer-send"), "with text, the composer shows Send");
            shot(&out1, "buttons-01-send");
            check(press("composer-send"), "Send is pressed");
            true
        })),
        ("sent", Box::new(|_, _, elapsed| {
            let sent = sends().iter().any(|s| s["body"]["text"].as_str().is_some_and(|t| t.contains("sent by the button")));
            if !sent {
                check(elapsed < Duration::from_secs(5), "Send sends the text, as Enter does");
                return elapsed >= Duration::from_secs(5);
            }
            check(app_now().active_draft().is_empty(), "and empties the composer");
            let set = control("/__control/stop", &json!({"respond": {"ok": true, "terminated": 1}}))
                .and_then(|()| control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "thinking", "busy": true}})));
            check(set.is_ok(), &format!("Rachel works: {set:?}"));
            true
        })),
        ("stop shown", Box::new(move |_, _, elapsed| {
            if !drawn("composer-stop") {
                check(elapsed < Duration::from_secs(5), "while the agent works the composer shows Stop");
                return elapsed >= Duration::from_secs(5);
            }
            shot(&out2, "buttons-02-stop");
            check(press("composer-stop"), "Stop is pressed");
            true
        })),
        ("stopped", Box::new(|_, _, elapsed| {
            let stops = posts("/stop");
            if stops.is_empty() {
                check(elapsed < Duration::from_secs(5), "Stop stops the agent, as Ctrl+. does");
                return elapsed >= Duration::from_secs(5);
            }
            check(stops[0]["body"]["session"] == "rachel", "the Stop is for Rachel");
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "idle", "busy": false}}));
            check(set.is_ok(), "Rachel is idle again");
            // A long chat to scroll in.
            let turns: Vec<_> = (0..40)
                .map(|i| json!({"id": format!("long-{i}"), "role": if i % 2 == 0 { "user" } else { "assistant" },
                    "text": format!("Line {i}: a note long enough to fill some of the chat's height on its own.")}))
                .collect();
            check(control("/__control/turns", &json!({"session": "rachel", "turns": turns})).is_ok(), "the Host holds a long chat");
            headless::press(slint::platform::Key::F5);
            true
        })),
        ("long", Box::new(move |app, _, elapsed| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| c.index_of("long-39").is_some() && !c.loading());
            if !loaded || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(report().at_end, "the chat is at its end");
            check(!drawn(&jump_id()), "at the end there is no jump button");
            let Some((x, y, w, h)) = chat() else { return false };
            end1.set(Some((x, y, w, h)));
            for _ in 0..8 {
                headless::wheel(x + w / 2.0, y + h / 2.0, 120.0);
            }
            true
        })),
        ("scrolled up", Box::new(move |_, _, elapsed| {
            if report().at_end || elapsed < Duration::from_millis(800) {
                if elapsed >= Duration::from_secs(5) {
                    check(false, "the wheel scrolls the chat up from its end");
                    return true;
                }
                return false;
            }
            check(drawn(&jump_id()), "scrolled up, a jump-to-latest button shows");
            check(chat() == end2.get(), &format!("it floats: the chat keeps its place and size: {:?} then {:?}", end2.get(), chat()));
            shot(&out3, "buttons-03-jump");
            check(press(&jump_id()), "the jump button is pressed");
            true
        })),
        ("at the end", Box::new(|_, _, elapsed| {
            if !report().at_end {
                check(elapsed < Duration::from_secs(5), "the jump button brings the chat to its end, as Ctrl+End does");
                return elapsed >= Duration::from_secs(5);
            }
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(!drawn(&jump_id()), "and goes at the end");
            true
        })),
    ];
    run_stages(stages);
}
