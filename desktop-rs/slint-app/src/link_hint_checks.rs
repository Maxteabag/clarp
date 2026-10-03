//! `--check link-hints`: every link in the active chat's viewport gets a
//! number and typing it opens that link, from the keyboard alone. A chat
//! with links in prose, a list, a table, a quote, a bare URL and a user's
//! message sits in view, with one link above the viewport and one below.
//! Only the links on screen are numbered, top to bottom; a number opens
//! exactly its link (recorded by `CLARP_TEST_OPEN_URL`, never a browser);
//! Escape opens nothing; the reader never moves. Needs the fake Host.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, report, run_stages, shot};
use crate::headless;

const LOREM: &str = "The agent reads the failing test, finds the stale fixture and rewrites it so the suite runs green again, then reports what changed and why it broke in the first place. ";

/// The links on screen, top to bottom (the user's own message is last).
const SHOWN: [&str; 6] = [
    "https://example.com/guide",
    "https://example.com/releases",
    "https://example.com/cargo-book",
    "https://example.com/warning",
    "https://bare.example/path?x=1",
    "https://user.example/page",
];
const ABOVE: &str = "https://above.example/early";
const BELOW: &str = "https://below.example/late";

fn filler(id: &str, role: &str, lines: usize) -> serde_json::Value {
    json!({"id": id, "role": role, "text": LOREM.repeat(lines)})
}

fn links_chat() -> Vec<serde_json::Value> {
    let mut turns = vec![json!({"id": "above", "role": "assistant", "text": format!("Earlier: see [the early doc]({ABOVE}) first.")})];
    for i in 0..8 {
        turns.push(filler(&format!("before-{i}"), if i % 2 == 0 { "user" } else { "assistant" }, 2));
    }
    turns.push(json!({"id": "target", "role": "assistant", "text": concat!(
        "Here is the [guide](https://example.com/guide) for the build.\n\n",
        "- Install from [the releases page](https://example.com/releases)\n",
        "- Then run the tests\n\n",
        "| Tool | Docs |\n|---|---|\n| cargo | [book](https://example.com/cargo-book) |\n\n",
        "> Read [the warning](https://example.com/warning) before you start.\n\n",
        "Raw: https://bare.example/path?x=1.",
    )}));
    turns.push(json!({"id": "user-link", "role": "user", "text": "Also check https://user.example/page please"}));
    for i in 0..10 {
        turns.push(filler(&format!("after-{i}"), if i % 2 == 0 { "assistant" } else { "user" }, 3));
    }
    turns.push(json!({"id": "below", "role": "assistant", "text": format!("Later: [the late doc]({BELOW}).")}));
    turns
}

/// Twelve links in one reply, for two-digit numbers.
fn many_chat() -> Vec<serde_json::Value> {
    let list: Vec<String> = (1..=12).map(|n| format!("- [Link {n}](https://many.example/{n})")).collect();
    vec![json!({"id": "many", "role": "assistant", "text": format!("Twelve links:\n\n{}", list.join("\n"))})]
}

/// The active pane's transcript viewport and its rows (window coordinates).
#[derive(Debug, Clone, Default)]
struct Geo {
    left: f32,
    right: f32,
    top: f32,
    bottom: f32,
    rows: Vec<(String, f32, f32)>,
}

impl Geo {
    fn row(&self, id: &str) -> Option<(f32, f32)> {
        self.rows.iter().find(|r| r.0 == id).map(|r| (r.1, r.2))
    }

    fn visible(&self, id: &str) -> bool {
        self.row(id).is_some_and(|(top, bottom)| bottom > self.top && top < self.bottom)
    }

    /// The topmost row showing and its top's distance from the viewport's.
    fn anchor(&self) -> Option<(String, f32)> {
        self.rows.iter().find(|r| r.2 > self.top + 1.0 && r.1 < self.bottom).map(|r| (r.0.clone(), r.1 - self.top))
    }
}

fn geometry() -> Geo {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return Geo::default() };
    let chat = format!("chat:{}", app_now().active_id());
    let found = ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|id| id == chat || id.starts_with("row:")))
        .find_all();
    let mut geo = Geo::default();
    let mut rows = Vec::new();
    let mut transcript = None;
    for element in found {
        let (position, size) = (element.absolute_position(), element.size());
        match element.accessible_id() {
            Some(id) if id.starts_with("row:") => {
                if size.height > 0.0 {
                    rows.push((id.trim_start_matches("row:").to_owned(), position.x + size.width / 2.0, position.y, position.y + size.height));
                }
            }
            Some(_) => transcript = Some((position, size)),
            None => {}
        }
    }
    match transcript {
        Some((position, size)) => (geo.left, geo.right, geo.top, geo.bottom) = (position.x, position.x + size.width, position.y, position.y + size.height),
        None => check(false, "the active pane's transcript is found by its accessible id"),
    }
    // Rows of another pane lie beside this transcript.
    geo.rows = rows.into_iter().filter(|r| r.1 >= geo.left && r.1 < geo.right).map(|r| (r.0, r.2, r.3)).collect();
    geo.rows.sort_by(|a, b| a.1.total_cmp(&b.1));
    geo
}

fn badges() -> Vec<crate::LinkBadge> {
    crate::window().map(|w| w.global::<crate::LinkHints>().get_badges().iter().collect()).unwrap_or_default()
}

fn active() -> bool {
    crate::window().is_some_and(|w| w.global::<crate::LinkHints>().get_active())
}

/// The numbers in order of their first badge, with their links.
fn numbered() -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    let mut shown = badges();
    shown.sort_by(|a, b| a.y.total_cmp(&b.y).then(a.x.total_cmp(&b.x)));
    for badge in shown {
        if !out.iter().any(|(label, _)| *label == badge.label.as_str()) {
            out.push((badge.label.to_string(), badge.url.to_string()));
        }
    }
    out.sort_by_key(|(label, _)| label.parse::<usize>().unwrap_or(usize::MAX));
    out
}

/// What the opener received so far, one URL a line.
fn opened() -> Vec<String> {
    let Some(path) = std::env::var_os("CLARP_TEST_OPEN_URL") else { return Vec::new() };
    std::fs::read_to_string(path).map(|t| t.lines().map(str::to_owned).collect()).unwrap_or_default()
}

fn bar() -> Vec<(String, String)> {
    crate::window().map(|w| w.get_hints().iter().map(|h| (h.keys.to_string(), h.label.to_string())).collect()).unwrap_or_default()
}

#[derive(Default)]
struct State {
    step: usize,
    offset: f32,
    anchor: Option<(String, f32)>,
    opened: usize,
}

/// The reader's place is where it was: the same offset, and the same row
/// first on screen at the same distance from the top.
fn reader_held(state: &State, what: &str) {
    let offset = report().offset;
    check((offset - state.offset).abs() < 0.5, &format!("{what}: the chat's offset holds ({:.1} → {offset:.1})", state.offset));
    let anchor = geometry().anchor();
    let same = match (&state.anchor, &anchor) {
        (Some(a), Some(b)) => a.0 == b.0 && (a.1 - b.1).abs() < 0.5,
        _ => false,
    };
    check(same, &format!("{what}: the first visible row holds ({:?} → {anchor:?})", state.anchor));
}

fn opened_since(state: &State) -> Vec<String> {
    opened().into_iter().skip(state.opened).collect()
}

pub fn link_hints_check(out: String) {
    let state = Rc::new(RefCell::new(State::default()));
    let s = || state.clone();
    let (out1, out2, out3, out4, out5) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/add-agent", &json!({"session": "links", "persona": "Links"})).is_ok(), "the Host takes a chat with links");
            check(control("/__control/add-agent", &json!({"session": "many", "persona": "Many"})).is_ok(), "the Host takes a chat with twelve links");
            true
        })),
        ("fill", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("links").is_none() || app.engine.borrow().roster().find("many").is_none() {
                return false;
            }
            check(control("/__control/turns", &json!({"session": "links", "turns": links_chat()})).is_ok(), "the links chat is filled");
            check(control("/__control/turns", &json!({"session": "many", "turns": many_chat()})).is_ok(), "the twelve-link chat is filled");
            app.engine.borrow_mut().select("links");
            crate::pump();
            true
        })),
        ("opened", Box::new(|_, window, elapsed| {
            let rows = super::rows(window);
            if rows.len() < 21 || elapsed < Duration::from_millis(800) || !report().at_end {
                return false;
            }
            // The chat, not the composer: Up and Down scroll it.
            headless::press(Key::Escape);
            true
        })),
        // Up and Down bring the reply with the links to the top of the view.
        ("scroll to the links", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if !report().transcript_focused || elapsed < Duration::from_millis(150) {
                    return false;
                }
                let mut st = state.borrow_mut();
                st.step += 1;
                if st.step % 2 == 0 {
                    return false;
                }
                let geo = geometry();
                match geo.row("target") {
                    None => headless::press(Key::PageUp),
                    Some((top, _)) if top < geo.top + 8.0 => headless::press(Key::UpArrow),
                    Some((top, _)) if top > geo.top + 100.0 => headless::press(Key::DownArrow),
                    Some(_) => {
                        st.step = 0;
                        return true;
                    }
                }
                false
            }
        })),
        ("settled", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(600) {
                    return false;
                }
                let geo = geometry();
                check(!geo.visible("above") && !geo.visible("below"), &format!("one link sits above the viewport and one below ({:?})", geo.rows.iter().filter(|r| r.0 == "above" || r.0 == "below").collect::<Vec<_>>()));
                check(geo.row("user-link").is_some_and(|(_, bottom)| bottom < geo.bottom), "the user's message with a link is on screen");
                let mut st = state.borrow_mut();
                st.offset = report().offset;
                st.anchor = geo.anchor();
                st.opened = opened().len();
                headless::press("f");
                true
            }
        })),
        // F from the chat numbers the links on screen, top to bottom.
        ("hints", Box::new({
            let state = s();
            move |_, window, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                check(active(), "F shows the link hints");
                let numbers = numbered();
                let expected: Vec<(String, String)> = SHOWN.iter().enumerate().map(|(i, u)| ((i + 1).to_string(), (*u).to_owned())).collect();
                check(numbers == expected, &format!("the links on screen are numbered 1-6 top to bottom: {numbers:?}"));
                let all = badges();
                check(!all.iter().any(|b| b.url == ABOVE || b.url == BELOW), "the links above and below the viewport get no number");
                let geo = geometry();
                let inside = all.iter().all(|b| b.x >= geo.left && b.x < geo.right && b.y >= geo.top - 1.0 && b.y < geo.bottom);
                check(inside, &format!("every badge sits in the chat's viewport {:.0}..{:.0} × {:.0}..{:.0}: {:?}", geo.left, geo.right, geo.top, geo.bottom, all.iter().map(|b| (b.label.to_string(), b.x, b.y)).collect::<Vec<_>>()));
                // Each badge is over its own message.
                let row_of = |url: &str| if url == SHOWN[5] { "user-link" } else { "target" };
                let placed = all.iter().all(|b| geo.row(row_of(&b.url)).is_some_and(|(top, bottom)| b.y >= top - 2.0 && b.y < bottom));
                check(placed, "each badge is drawn over its link's message");
                let hints = bar();
                check(hints.contains(&("1-6".into(), "Open".into())) && hints.contains(&("Esc".into(), "Cancel".into())), &format!("the shortcut bar shows the hint keys: {hints:?}"));
                check(window.get_keyboard_mode() == "HINTS", &format!("the bar's mode says HINTS: {}", window.get_keyboard_mode()));
                reader_held(&state.borrow(), "showing hints");
                shot(&out1, "link-hints-01-shown");
                headless::press("3");
                true
            }
        })),
        ("opened 3", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let st = state.borrow();
                check(opened_since(&st) == [SHOWN[2]], &format!("3 opens exactly the third link: {:?}", opened_since(&st)));
                check(!active(), "opening a link hides the hints");
                reader_held(&st, "after opening");
                drop(st);
                state.borrow_mut().opened = opened().len();
                headless::press("f");
                true
            }
        })),
        ("shown again", Box::new(|_, _, elapsed| {
            if !active() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("cancelled", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let st = state.borrow();
                check(!active(), "Escape hides the hints");
                check(opened_since(&st).is_empty(), &format!("Escape opens nothing: {:?}", opened_since(&st)));
                reader_held(&st, "after Escape");
                check(report().transcript_focused, "the chat keeps the keyboard after Escape");
                headless::press("i");
                true
            }
        })),
        // From the composer, Ctrl+L.
        ("composer", Box::new(|_, _, elapsed| {
            if !report().composer_focused || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press_with(&[Key::Control], "l");
            true
        })),
        ("composer hints", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                check(active(), "Ctrl+L shows the link hints from the composer");
                check(numbered().len() == SHOWN.len(), &format!("the same six links are numbered: {:?}", numbered()));
                reader_held(&state.borrow(), "hints from the composer");
                shot(&out2, "link-hints-02-composer");
                headless::press("1");
                true
            }
        })),
        ("composer opened", Box::new({
            let state = s();
            move |app, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let st = state.borrow();
                check(opened_since(&st) == [SHOWN[0]], &format!("1 opens the first link: {:?}", opened_since(&st)));
                check(app.active_draft().is_empty(), &format!("the digit is not typed into the composer: {:?}", app.active_draft()));
                check(report().composer_focused, "the composer keeps the keyboard after a link opens");
                reader_held(&st, "after opening from the composer");
                drop(st);
                state.borrow_mut().opened = opened().len();
                headless::press_with(&[Key::Control], "k");
                true
            }
        })),
        // Ctrl+K's "Open a link".
        ("switcher", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("open a link");
            true
        })),
        ("switcher typed", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "open a link" || elapsed < Duration::from_millis(200) {
                return false;
            }
            let first = window.get_switcher_rows().row_data(0).map(|r| r.label.to_string()).unwrap_or_default();
            check(first == "Open a link", &format!("Ctrl+K lists \"Open a link\" first: {first:?}"));
            headless::press(Key::Return);
            true
        })),
        ("switcher hints", Box::new({
            let state = s();
            move |_, window, elapsed| {
                if window.get_switcher_open() || elapsed < Duration::from_millis(400) {
                    return false;
                }
                check(active() && numbered().len() == SHOWN.len(), &format!("\"Open a link\" numbers the links: {:?}", numbered()));
                reader_held(&state.borrow(), "hints from Ctrl+K");
                headless::press(Key::Escape);
                true
            }
        })),
        // Twelve links: two digits.
        ("many", Box::new(|app, _, elapsed| {
            if active() || elapsed < Duration::from_millis(200) {
                return false;
            }
            app.engine.borrow_mut().select("many");
            crate::pump();
            true
        })),
        ("many opened", Box::new(|app, window, elapsed| {
            let loaded = app.engine.borrow().conversation("many").is_some_and(|c| !c.rows().is_empty());
            if !loaded || super::rows(window).is_empty() || elapsed < Duration::from_millis(800) {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("many chat", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if !report().transcript_focused || elapsed < Duration::from_millis(300) {
                    return false;
                }
                let mut st = state.borrow_mut();
                st.offset = report().offset;
                st.anchor = geometry().anchor();
                st.opened = opened().len();
                headless::press("f");
                true
            }
        })),
        ("twelve", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let numbers = numbered();
            let expected: Vec<(String, String)> = (1..=12).map(|n| (n.to_string(), format!("https://many.example/{n}"))).collect();
            check(numbers == expected, &format!("twelve links are numbered 1-12: {numbers:?}"));
            check(bar().contains(&("1-12".into(), "Open".into())), &format!("the bar says 1-12 Open: {:?}", bar()));
            headless::press("1");
            true
        })),
        ("typed 1", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                check(active() && opened_since(&state.borrow()).is_empty(), "with twelve links, 1 waits for a second digit");
                let dimmed: Vec<String> = badges().iter().filter(|b| b.dim).map(|b| b.label.to_string()).collect();
                check(dimmed.len() == 8 && !dimmed.contains(&"12".to_owned()), &format!("the numbers 1 rules out are dimmed: {dimmed:?}"));
                shot(&out3, "link-hints-03-two-digits");
                headless::press("2");
                true
            }
        })),
        ("opened 12", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let st = state.borrow();
                check(opened_since(&st) == ["https://many.example/12"], &format!("1 then 2 opens the twelfth link: {:?}", opened_since(&st)));
                check(!active(), "the hints close once the link opens");
                drop(st);
                state.borrow_mut().opened = opened().len();
                headless::press("f");
                headless::press("1");
                headless::press(Key::Return);
                true
            }
        })),
        ("opened 1", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let st = state.borrow();
                check(opened_since(&st) == ["https://many.example/1"], &format!("1 then Enter opens the first link: {:?}", opened_since(&st)));
                drop(st);
                state.borrow_mut().opened = opened().len();
                headless::press("f");
                headless::press("5");
                true
            }
        })),
        ("opened 5", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let st = state.borrow();
                check(opened_since(&st) == ["https://many.example/5"], &format!("5 opens at once (no 50s): {:?}", opened_since(&st)));
                reader_held(&st, "after the twelve-link chat");
                drop(st);
                state.borrow_mut().opened = opened().len();
                // A light theme: the badges keep the theme's accent roles.
                if let Some(window) = crate::window() {
                    crate::view::apply_theme(&window, "paper");
                }
                headless::press("f");
                true
            }
        })),
        ("light theme", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(active(), "hints show in a light theme");
            shot(&out4, "link-hints-04-paper");
            headless::press(Key::Escape);
            crate::view::apply_theme(window, "terminal");
            // A second pane on the links chat: only the active pane's
            // links are numbered.
            headless::press_with(&[Key::Control, Key::Alt], "v");
            true
        })),
        ("split", Box::new(|app, window, elapsed| {
            if app.engine.borrow().panes().pane_count() != 2 || elapsed < Duration::from_millis(300) {
                return false;
            }
            window.invoke_chat_chosen("links".into());
            true
        })),
        ("split links", Box::new(|app, window, elapsed| {
            let shows = app.active_view().is_some_and(|v| v.session == "links");
            if !shows || super::rows(window).len() < 21 || elapsed < Duration::from_millis(1000) {
                return false;
            }
            headless::press_with(&[Key::Control], "l");
            true
        })),
        ("split hints", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let geo = geometry();
            let all = badges();
            check(active() && !all.is_empty(), &format!("Ctrl+L numbers the active pane's links ({} badges)", all.len()));
            check(all.iter().all(|b| b.x >= geo.left && b.x < geo.right), &format!("every badge is in the active pane ({:.0}..{:.0}): {:?}", geo.left, geo.right, all.iter().map(|b| (b.label.to_string(), b.x)).collect::<Vec<_>>()));
            check(!all.iter().any(|b| b.url.starts_with("https://many.example/")), "the other pane's links get no number");
            shot(&out5, "link-hints-05-split");
            headless::press(Key::Escape);
            true
        })),
    ];
    run_stages(stages);
}
