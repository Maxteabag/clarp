//! `--check polish --out DIR`: the small things the 3166 review found
//! (2026-11-28), on the desktop.
//!
//! - A thought never reads "Thought for 0s" (CL-071): under a second it is
//!   "Thought" with its headline, and with nothing to show it gets no row.
//! - Every harness in New Session keeps its whole name in a 520 px window:
//!   the marks wrap onto another line instead of running off the card.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use i_slint_core::accessibility::AccessibleStringProperty;
use i_slint_core::item_tree::ItemRc;
use i_slint_core::window::WindowInner;
use serde_json::json;
use slint::{ComponentHandle, LogicalSize, Model};
use slint::platform::Key;

use super::{Stage, app_now, check, control, report, rows, run_stages, shot};
use crate::{LaunchHub, headless};

const NARROW: f32 = 520.0;
const NARROW_H: f32 = 720.0;
const HARNESSES: [&str; 6] = ["Claude", "Codex", "Grok", "OpenCode", "DeepSeek", "Gemini"];

fn titles() -> Vec<String> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().filter(|r| !r.live.key.is_empty()).map(|r| r.live.title.to_string()).collect()
}

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}

/// Every element whose accessible label is one of `labels`, as (label, x, width).
fn labelled(item: &ItemRc, labels: &[&str], out: &mut Vec<(String, f32, f32)>) {
    if let Some(label) = item.accessible_string_property(AccessibleStringProperty::Label).map(|l| l.to_string())
        && labels.contains(&label.as_str())
    {
        let geometry = item.geometry();
        let origin = item.map_to_window(geometry.origin);
        out.push((label, origin.x, geometry.size.width));
    }
    let mut child = item.first_child();
    while let Some(current) = child {
        labelled(&current, labels, out);
        child = current.next_sibling();
    }
}

pub(super) fn polish_check(out: String) {
    let (o1, o2) = (out.clone(), out);
    let started = Rc::new(Cell::new(0_i64));
    let started2 = started.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::ALWAYS_VISIBLE);
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            let providers: serde_json::Map<String, serde_json::Value> = HARNESSES
                .iter()
                .enumerate()
                .map(|(i, label)| (label.to_lowercase(), json!({"label": label, "sort_index": i + 1, "models": [{"id": format!("{}-1", label.to_lowercase()), "label": format!("{label} model")}]})))
                .collect();
            check(control("/__control/catalog", &json!({"providers": providers})).is_ok(), "the Host offers six harnesses");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("live", Box::new(move |app, _window, _| {
            let engine = app.engine.borrow();
            let subscribed = engine.live_items() && engine.live_view("rachel").and_then(|v| v.lseq()) == Some(0);
            if !subscribed || engine.backend_options().len() != HARNESSES.len() {
                return false;
            }
            drop(engine);
            let now = now_ms();
            started.set(now);
            let thought = |id: &str, ordinal: i64, title: &str, text: &str, from: i64, to: i64| json!({"op": "upsert", "conv": "conv-1", "id": id, "kind": "reasoning", "rev": 1,
                "item": {"id": id, "conv": "conv-1", "turn_id": "tr-p", "kind": "reasoning", "status": "completed", "ordinal": ordinal,
                    "title": title, "text": text, "started_at_ms": now - from, "ended_at_ms": now - to}});
            control("/__control/live-event", &json!({"session": "rachel", "event": {"server_now_ms": now, "ops": [
                {"op": "turn", "conv": "conv-1", "turn": {"turn_id": "tr-p", "status": "running", "started_at_ms": now - 60_000, "ended_at_ms": null, "worked_ms": null, "tool_count": 0}},
                // Codex: told once it is over, so both ends are the same moment.
                thought("cx:r1", 1, "Reading the code", "**Reading the code**\nThe parser first.", 50_000, 50_000),
                // Nothing to show and under a second.
                thought("cx:r2", 2, "", "", 40_000, 39_600),
                thought("cx:r3", 3, "Weighing the failure", "It fails on empty input.", 39_000, 18_000),
            ]}}))
            .is_ok()
        })),
        ("thoughts", Box::new(move |_, _window, elapsed| {
            if titles().len() < 2 || elapsed < Duration::from_millis(600) {
                return false;
            }
            let shown = titles();
            check(!shown.iter().any(|t| t.contains("for 0s")), &format!("no thought reads \"Thought for 0s\": {shown:?}"));
            check(shown == ["Thought: Reading the code", "Thought for 21s: Weighing the failure"], &format!("under a second it reads \"Thought\", an empty one has no row: {shown:?}"));
            shot(&o1, "polish-01-thoughts");
            let now = started2.get() + 1;
            control("/__control/live-event", &json!({"session": "rachel", "event": {"server_now_ms": now, "ops": [
                {"op": "turn", "conv": "conv-1", "turn": {"turn_id": "tr-p", "status": "completed", "started_at_ms": now - 60_000, "ended_at_ms": now, "worked_ms": 60_000, "tool_count": 0}},
            ]}}))
            .is_ok()
        })),
        ("narrow", Box::new(|_, window, _| {
            window.window().set_size(LogicalSize::new(NARROW, NARROW_H));
            if (window.window().size().width as f32 / window.window().scale_factor() - NARROW).abs() > 1.0 {
                return false;
            }
            if !report().composer_focused {
                app_now().focus_composer();
                return false;
            }
            headless::press_with(&[Key::Control], "n");
            true
        })),
        ("hub", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "new-session" || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(window.global::<LaunchHub>().get_providers().row_count() == HARNESSES.len(), "the hub lists all six harnesses");
            let inner = WindowInner::from_pub(window.window());
            let root = ItemRc::new_root(inner.component());
            let mut marks = Vec::new();
            labelled(&root, &HARNESSES, &mut marks);
            // The card is the window less its 20 px margins, its content 18 px in.
            let (left, right) = (20.0 + 18.0, NARROW - 20.0 - 18.0);
            let outside: Vec<String> = marks.iter().filter(|(_, x, w)| *x < left - 0.5 || x + w > right + 0.5).map(|(l, x, w)| format!("{l} {x:.0}..{:.0}", x + w)).collect();
            check(marks.len() == HARNESSES.len(), &format!("every harness has its mark: {:?}", marks.iter().map(|m| &m.0).collect::<Vec<_>>()));
            check(outside.is_empty(), &format!("every mark, whole, stays inside the {left:.0}..{right:.0} px card: {outside:?}"));
            shot(&o2, "polish-02-hub-520");
            true
        })),
    ];
    run_stages(stages);
}
