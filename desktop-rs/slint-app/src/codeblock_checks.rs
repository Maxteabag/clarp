//! `--check codeblocks --out DIR`: a code block has a header row over its
//! code with its language's name and Copy and Expand icons; the copy puts
//! the code on the clipboard and the icon says Copied, then Copy code
//! again. Expand, or a click on the code, opens it over the window,
//! wrapped, with wrap, copy and close icons; Escape closes it. A table wider than the chat has an expand icon
//! over it that opens it over the window, where its copy copies the
//! table's Markdown. Shots in the paper theme (`CLARP_CHECK_THEME` picks
//! another). Needs the fake Host.

use std::cell::Cell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::json;

use super::{Stage, check, control, run_stages, shot};
use crate::headless;

const CODE: &str = "def summarize(rows):\n    totals = {row[\"team\"]: sum(r[\"hours\"] for r in rows if r[\"team\"] == row[\"team\"]) for row in rows}\n    return sorted(totals.items(), key=lambda item: item[1], reverse=True)\n\nprint(summarize(load(\"hours.csv\")))";

fn element(id: &str) -> Option<i_slint_backend_testing::ElementHandle> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
}

fn press(id: &str) -> bool {
    element(id).map(|e| e.invoke_accessible_default_action()).is_some()
}

fn label(id: &str) -> String {
    element(id).and_then(|e| e.accessible_label()).map(|l| l.to_string()).unwrap_or_default()
}

fn clipboard() -> String {
    std::env::var_os("CLARP_TEST_CLIPBOARD").and_then(|p| std::fs::read_to_string(p).ok()).unwrap_or_default()
}

/// A left click inside the element, `at` of the way down it.
fn click_in(id: &str, at: f32) -> bool {
    let Some(e) = element(id) else { return false };
    let (origin, size) = (e.absolute_position(), e.size());
    headless::click(origin.x + size.width / 2.0, origin.y + size.height * at);
    true
}

pub(super) fn codeblocks_check(out: String) {
    let theme = std::env::var("CLARP_CHECK_THEME").unwrap_or_else(|_| "paper".to_owned());
    let header = (0..9).map(|i| format!("Column {i}")).collect::<Vec<_>>();
    let mut table = format!("| {} |\n|{}\n", header.join(" | "), "---|".repeat(9));
    for row in 0..4 {
        let cells: Vec<String> = (0..9).map(|c| format!("value {row}.{c} here")).collect();
        table.push_str(&format!("| {} |\n", cells.join(" | ")));
    }
    let turns = json!({"session": "rachel", "turns": [
        {"id": "q1", "role": "user", "text": "Which team logged the most hours?"},
        {"id": "a1", "role": "assistant", "text": format!("This sums the hours per team:\n\n```python\n{CODE}\n```\n\nRun it next to the export.")},
        {"id": "q2", "role": "user", "text": "And the whole sheet?"},
        {"id": "a2", "role": "assistant", "text": format!("Here it is:\n\n{table}")},
    ]});
    let since: Rc<Cell<Option<Instant>>> = Rc::new(Cell::new(None));
    let settled = move |since: &Cell<Option<Instant>>, wait: u64| {
        let at = *since.get().get_or_insert_with(Instant::now);
        since.set(Some(at));
        if at.elapsed() < Duration::from_millis(wait) {
            return false;
        }
        since.set(None);
        true
    };
    let (o1, o2, o3, o4, o5) = (out.clone(), out.clone(), out.clone(), out.clone(), out);
    let (s1, s2, s3, s4, s5, s6) = (since.clone(), since.clone(), since.clone(), since.clone(), since.clone(), since);
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            app.engine.borrow_mut().set_reading_theme(&theme);
            check(control("/__control/turns", &turns).is_ok(), "the Host takes the chat with code and a wide table");
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("drawn", Box::new(move |app, _, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && c.rows().iter().any(|r| r.id.as_str() == "a2"));
            if !loaded || element("code-copy").is_none() || !settled(&s1, 800) {
                return false;
            }
            check(label("code-block") == "Python", &format!("the code's line names its language: {:?}", label("code-block")));
            check(element("code-expand").is_some(), "the code's header has an Expand icon");
            check(element("table-expand").is_some(), "the table wider than the chat has an expand icon");
            shot(&o1, "codeblocks-01-header");
            check(press("code-copy"), "the code's copy icon is pressed");
            check(clipboard() == CODE, &format!("its copy puts the code on the clipboard: {:?}", clipboard()));
            check(label("code-copy") == "Copied", &format!("the icon says Copied: {:?}", label("code-copy")));
            shot(&o1, "codeblocks-01-copied");
            true
        })),
        ("copy again", Box::new(move |_, _, _| {
            if !settled(&s2, 1800) {
                return false;
            }
            check(label("code-copy") == "Copy code", &format!("after a moment it says Copy code again: {:?}", label("code-copy")));
            check(press("code-expand"), "the code's Expand icon is pressed");
            true
        })),
        ("code viewer", Box::new(move |_, window, _| {
            if window.get_overlay() != crate::block_view::OVERLAY || !settled(&s3, 400) {
                return false;
            }
            check(element("block-view-code").is_some(), "Expand opened the code over the window");
            check(element("block-view-wrap").and_then(|e| e.accessible_checked()) == Some(true), "it is wrapped");
            shot(&o2, "codeblocks-02-viewer-wrapped");
            check(press("block-view-wrap"), "the wrap icon is pressed");
            true
        })),
        ("unwrapped", Box::new(move |_, _, _| {
            if !settled(&s4, 300) {
                return false;
            }
            check(element("block-view-wrap").and_then(|e| e.accessible_checked()) == Some(false), "the wrap icon turns wrapping off");
            shot(&o3, "codeblocks-02-viewer-unwrapped");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape closes the viewer");
            check(click_in("code-block", 0.7), "the code is clicked");
            true
        })),
        ("clicked open", Box::new(|_, window, _| {
            if window.get_overlay() != crate::block_view::OVERLAY {
                return false;
            }
            check(true, "a click on the code opens it over the window");
            headless::press(slint::platform::Key::Escape);
            true
        })),
        ("closed again", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(press("table-expand"), "the table's expand icon is pressed");
            true
        })),
        ("table viewer", Box::new(move |_, window, _| {
            if window.get_overlay() != crate::block_view::OVERLAY || !settled(&s5, 400) {
                return false;
            }
            shot(&o4, "codeblocks-03-table-viewer");
            check(press("block-view-copy"), "the viewer's copy is pressed");
            let copied = clipboard();
            check(copied.starts_with("| Column 0 | Column 1 |") && copied.contains("\n| --- |") && copied.ends_with("| value 3.8 here |"),
                &format!("it copies the table's Markdown: {copied:?}"));
            check(press("block-view-close"), "the close icon is pressed");
            true
        })),
        ("table closed", Box::new(move |_, window, _| {
            if !window.get_overlay().is_empty() || !settled(&s6, 300) {
                return false;
            }
            check(true, "close closes the table");
            shot(&o5, "codeblocks-03-table-chat");
            true
        })),
    ];
    run_stages(stages);
}
