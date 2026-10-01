//! The chat's scrolling (`--check scroll`), measured on screen: where the
//! transcript's rows are drawn relative to its viewport, read through the
//! rows' accessible ids. A long chat of rows that differ in height (prose,
//! code, tables, lists, tool calls, user bubbles) opens at its latest
//! message, follows a streaming reply, holds a reader who scrolled up while
//! the agent works, reaches its true end by wheel, Page Down and End, and
//! keeps the reader's place while an older page loads above.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::json;

use super::{Stage, app_now, check, control, report, rows, run_stages, shot};
use crate::headless;

/// The transcript's viewport and the rows drawn in it (window coordinates).
#[derive(Debug, Clone, Default)]
struct Geo {
    left: f32,
    width: f32,
    top: f32,
    bottom: f32,
    /// (row id, top, bottom), top to bottom.
    rows: Vec<(String, f32, f32)>,
}

impl Geo {
    fn find(&self, id: &str) -> Option<(f32, f32)> {
        self.rows.iter().find(|r| r.0 == id).map(|r| (r.1, r.2))
    }

    /// The topmost row showing in the viewport and its top's distance from
    /// the viewport's top.
    fn anchor(&self) -> Option<(String, f32)> {
        self.rows.iter().find(|r| r.2 > self.top + 1.0 && r.1 < self.bottom).map(|r| (r.0.clone(), r.1 - self.top))
    }

    fn drawn(&self) -> String {
        let shown: Vec<&(String, f32, f32)> = self.rows.iter().filter(|r| r.2 > self.top && r.1 < self.bottom).collect();
        match (shown.first(), shown.last()) {
            (Some(first), Some(last)) => format!("{} rows drawn, {}..{}", shown.len(), first.0, last.0),
            _ => "no rows drawn".into(),
        }
    }
}

fn geometry() -> Geo {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return Geo::default() };
    // The viewport is the Transcript itself (found by its type, which
    // needs the debug build's element info); rows by their accessible ids.
    let found = ElementQuery::from_root(&window)
        .match_predicate(|e| e.type_name().is_some_and(|t| t == "Transcript") || e.accessible_id().is_some_and(|id| id.starts_with("row:")))
        .find_all();
    let mut geo = Geo::default();
    for element in found {
        let (position, size) = (element.absolute_position(), element.size());
        match element.accessible_id() {
            Some(id) if size.height > 0.0 => geo.rows.push((id.trim_start_matches("row:").to_owned(), position.y, position.y + size.height)),
            Some(_) => {}
            None => (geo.left, geo.width, geo.top, geo.bottom) = (position.x, size.width, position.y, position.y + size.height),
        }
    }
    geo.rows.sort_by(|a, b| a.1.total_cmp(&b.1));
    if geo.bottom <= geo.top {
        check(false, "the transcript's viewport is found (the checks need a debug build's element debug info)");
    }
    geo
}

fn last_id() -> String {
    crate::window().map(|w| rows(&w)).and_then(|r| r.last().map(|r| r.id.to_string())).unwrap_or_default()
}

/// Whether the chat's last row is drawn with its bottom edge inside the
/// viewport, and the numbers either way.
fn last_row_visible() -> (bool, String) {
    let geo = geometry();
    let id = last_id();
    match geo.find(&id) {
        Some((top, bottom)) => {
            let ok = bottom <= geo.bottom + 1.0 && bottom > geo.top + 1.0 && (top >= geo.top - 1.0 || bottom - top > geo.bottom - geo.top);
            (ok, format!("last row {id} at {top:.1}..{bottom:.1}, viewport {:.1}..{:.1} (gap {:.1}px; {})", geo.top, geo.bottom, geo.bottom - bottom, geo.drawn()))
        }
        None => (false, format!("last row {id} is not drawn (viewport {:.1}..{:.1}; {})", geo.top, geo.bottom, geo.drawn())),
    }
}

/// How far the anchor row moved on screen since it was recorded.
fn anchor_moved(anchor: &(String, f32)) -> (bool, String) {
    let geo = geometry();
    match geo.find(&anchor.0) {
        Some((top, _)) => {
            let moved = (top - geo.top) - anchor.1;
            (moved.abs() <= 1.0, format!("row {} moved {moved:.1}px ({:.1} → {:.1} from the top; {})", anchor.0, anchor.1, top - geo.top, geo.drawn()))
        }
        None => (false, format!("row {} is no longer drawn ({})", anchor.0, geo.drawn())),
    }
}

fn wheel(delta_y: f32) {
    let geo = geometry();
    headless::wheel(geo.left + geo.width / 2.0, (geo.top + geo.bottom) / 2.0, delta_y);
}

const LOREM: &str = "The agent reads the failing test, finds the stale fixture and rewrites it so the suite runs green again. ";

/// The streaming reply after `step` updates: paragraphs and code blocks.
fn streamed(step: usize) -> String {
    let mut text = String::from("Working on it.");
    for n in 0..step {
        text.push_str(&format!("\n\nPart {n}. {}", LOREM.repeat(2 + n)));
        if n % 2 == 1 {
            text.push_str(&format!("\n\n```sh\n{}\n```", (0..4 + n).map(|i| format!("cargo test case_{i}")).collect::<Vec<_>>().join("\n")));
        }
    }
    text
}

fn upsert(session: &str, turns: serde_json::Value) -> bool {
    let sent = control("/__control/upsert", &json!({"session": session, "turns": turns}));
    if let Err(error) = &sent {
        check(false, &format!("the Host takes an update: {error}"));
    }
    sent.is_ok()
}

/// Whether the chat's row `id` holds `text` (the update has landed).
fn holds(id: &str, text: &str) -> bool {
    let app = app_now();
    let engine = app.engine.borrow();
    engine.conversation(engine.selected_session()).is_some_and(|c| c.rows().iter().any(|m| m.id == id && m.text == text))
}

fn has_row(id: &str) -> bool {
    crate::window().map(|w| rows(&w)).is_some_and(|r| r.iter().any(|r| r.id == id))
}

/// Ids of rows with their own tool calls, before and after the drawn rows.
fn tool_rows_around() -> (Option<String>, Option<String>) {
    let geo = geometry();
    let rows = crate::window().map(|w| rows(&w)).unwrap_or_default();
    let index = |id: &str| rows.iter().position(|r| r.id == id);
    let shown: Vec<usize> = geo.rows.iter().filter(|r| r.2 > geo.top && r.1 < geo.bottom).filter_map(|r| index(&r.0)).collect();
    let (Some(&first), Some(&last)) = (shown.iter().min(), shown.iter().max()) else { return (None, None) };
    let tools = |i: &usize| !rows[*i].activity_label.is_empty() && rows[*i].group_id.is_empty() && !rows[*i].expanded;
    let above = (0..first.saturating_sub(3)).rev().find(tools).map(|i| rows[i].id.to_string());
    let below = (last + 3..rows.len()).find(tools).map(|i| rows[i].id.to_string());
    (above, below)
}

fn toggle(id: &str) {
    if let Some(window) = crate::window() {
        window.invoke_toggle_activity(app_now().active_id(), id.into(), "".into());
    }
}

fn expanded(id: &str) -> bool {
    crate::window().map(|w| rows(&w)).is_some_and(|r| r.iter().any(|r| r.id == id && r.expanded))
}

#[derive(Default)]
struct State {
    step: usize,
    since: Option<Instant>,
    anchor: Option<(String, f32)>,
    offset: f32,
    still: usize,
    target: String,
    below: Option<String>,
    above: Option<String>,
}

/// Wheels (or presses) once per poll until the offset stops changing; true
/// once it has held still for three polls or `limit` turns were taken.
fn until_still(state: &mut State, limit: usize, act: impl Fn()) -> bool {
    let offset = report().offset;
    if (offset - state.offset).abs() < 0.5 {
        state.still += 1;
    } else {
        state.still = 0;
    }
    state.offset = offset;
    state.step += 1;
    if state.still >= 3 || state.step > limit {
        return true;
    }
    act();
    false
}

fn settled(state: &State, ms: u64) -> bool {
    state.since.is_some_and(|s| s.elapsed() >= Duration::from_millis(ms))
}

/// `--check scroll --out DIR`.
pub(super) fn scroll_check(out: String) {
    use slint::platform::Key;
    let state = Rc::new(RefCell::new(State::default()));
    let s = || state.clone();
    let (out1, out2, out3) = (out.clone(), out.clone(), out.clone());
    const LONG: usize = 400;
    let streams: usize = 5;
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/add-agent", &json!({"session": "scroll", "persona": "Scroll"})).is_ok(), "the Host takes a new chat");
            true
        })),
        ("select", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("scroll").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("scroll");
            crate::pump();
            true
        })),
        ("fill", Box::new(|app, _, elapsed| {
            if app.engine.borrow().conversation("scroll").is_none() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(control("/__control/rich", &json!({"session": "scroll", "count": LONG})).is_ok(), &format!("the Host fills the chat with {LONG} varied rows"));
            true
        })),
        ("filled", Box::new(|app, window, elapsed| {
            if rows(window).len() < LONG || elapsed < Duration::from_millis(800) {
                return false;
            }
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("away", Box::new(|app, _, elapsed| {
            if app.engine.borrow().selected_session() != "rachel" || elapsed < Duration::from_millis(400) {
                return false;
            }
            app.engine.borrow_mut().select("scroll");
            crate::pump();
            true
        })),
        // 1. A chat opens at its latest message, fully visible.
        ("opens at the latest", Box::new(move |_, window, elapsed| {
            if rows(window).len() < LONG || elapsed < Duration::from_millis(1200) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(report().follows && report().at_end, &format!("a {LONG}-row chat opens following at its end (follows {}, at end {}, offset {})", report().follows, report().at_end, report().offset));
            check(visible, &format!("the opened chat shows its last row whole: {numbers}"));
            shot(&out1, "scroll-01-opened");
            true
        })),
        // 2. Following: new rows and a growing streaming reply stay in view.
        ("follows a stream", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let step = st.step;
                // Each update: wait for it to land, then measure.
                if step > 0 {
                    let landed = if step <= streams { holds("stream-1", &streamed(step - 1)) } else { has_row(&st.target) };
                    if !landed {
                        return false;
                    }
                    if st.since.is_none() {
                        st.since = Some(Instant::now());
                    }
                    if !settled(&st, 250) {
                        return false;
                    }
                    let (visible, numbers) = last_row_visible();
                    check(visible && report().follows, &format!("following, update {step} keeps the last row in view (follows {}): {numbers}", report().follows));
                }
                st.since = None;
                st.step += 1;
                let next = st.step;
                if next <= streams {
                    upsert("scroll", json!([{"id": "stream-1", "role": "assistant", "text": streamed(next - 1)}]));
                } else if next == streams + 1 {
                    upsert("scroll", json!([{"id": "scroll-user-1", "role": "user", "text": "Thanks, and the docs?"},
                                            {"id": "scroll-reply-1", "role": "assistant", "text": LOREM.repeat(4)}]));
                    st.target = "scroll-reply-1".into();
                } else {
                    st.step = 0;
                    return true;
                }
                false
            }
        })),
        // 3. The wheel scrolls up into the history and stops following.
        ("wheel up", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                if st.step == 0 {
                    st.offset = report().offset;
                }
                st.step += 1;
                if st.step <= 6 {
                    wheel(240.0);
                    return false;
                }
                if st.step < 12 {
                    return false;
                }
                let moved = report().offset - st.offset;
                check(moved > 400.0, &format!("the wheel scrolls up ({:.1} → {:.1}, {moved:.1}px)", st.offset, report().offset));
                check(!report().follows && !report().at_end, &format!("a reader who wheeled up stops following (follows {}, at end {})", report().follows, report().at_end));
                st.anchor = geometry().anchor();
                st.step = 0;
                true
            }
        })),
        ("holds still", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(800) {
                    return false;
                }
                let mut st = state.borrow_mut();
                let Some(anchor) = st.anchor.clone() else {
                    check(false, "a row shows after wheeling up");
                    return true;
                };
                let (held, numbers) = anchor_moved(&anchor);
                check(held, &format!("with nothing happening the reader's place holds: {numbers}"));
                st.anchor = geometry().anchor();
                (st.above, st.below) = tool_rows_around();
                true
            }
        })),
        // 4. Activity does not move a reader in the history.
        ("activity while reading", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let step = st.step;
                if step > 0 {
                    let landed = match step {
                        1 => has_row("scroll-new-3"),
                        2..=4 => holds("stream-2", &streamed(step - 1)),
                        5 => st.below.as_deref().is_none_or(expanded),
                        _ => st.above.as_deref().is_none_or(expanded),
                    };
                    if !landed {
                        return false;
                    }
                    if st.since.is_none() {
                        st.since = Some(Instant::now());
                    }
                    if !settled(&st, 400) {
                        return false;
                    }
                    let what = ["", "three rows arrive", "a reply starts streaming", "the reply grows", "the reply grows again", "tool calls open below the reader", "tool calls open above the reader"][step];
                    let Some(anchor) = st.anchor.clone() else { return true };
                    let (held, numbers) = anchor_moved(&anchor);
                    check(held && !report().follows, &format!("{what}: the reader stays put (follows {}): {numbers}", report().follows));
                    // Measure each change on its own.
                    st.anchor = geometry().anchor();
                }
                st.since = None;
                st.step += 1;
                match st.step {
                    1 => {
                        upsert("scroll", json!([
                            {"id": "scroll-new-1", "role": "user", "text": "One more thing"},
                            {"id": "scroll-new-2", "role": "assistant", "text": LOREM.repeat(6)},
                            {"id": "scroll-new-3", "role": "assistant", "text": format!("```\n{}\n```", "line\n".repeat(12))}]));
                    }
                    2..=4 => {
                        let n = st.step - 1;
                        upsert("scroll", json!([{"id": "stream-2", "role": "assistant", "text": streamed(n)}]));
                    }
                    5 => match st.below.clone() {
                        Some(id) => toggle(&id),
                        None => check(false, "a row with tool calls lies below the reader"),
                    },
                    6 => match st.above.clone() {
                        Some(id) => toggle(&id),
                        None => check(false, "a row with tool calls lies above the reader"),
                    },
                    _ => {
                        st.step = 0;
                        st.still = 0;
                        return true;
                    }
                }
                false
            }
        })),
        // 5. The wheel reaches the true end and following resumes.
        ("wheel to the end", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                if !until_still(&mut st, 150, || wheel(-300.0)) {
                    return false;
                }
                let (visible, numbers) = last_row_visible();
                check(visible && report().at_end, &format!("wheeling down reaches the end (at end {}, offset {}): {numbers}", report().at_end, report().offset));
                check(report().follows, "reaching the end by wheel follows again");
                shot(&out2, "scroll-02-wheeled-down");
                st.step = 0;
                st.still = 0;
                st.offset = report().offset;
                app_now().focus_transcript();
                true
            }
        })),
        // 6. Page Down from the history reaches the true end.
        ("page up", Box::new({
            let state = s();
            move |_, _, _| {
                if !report().transcript_focused {
                    return false;
                }
                let mut st = state.borrow_mut();
                st.step += 1;
                if st.step <= 8 {
                    headless::press(Key::PageUp);
                    return false;
                }
                if st.step < 12 {
                    return false;
                }
                check(!report().follows && !report().at_end, &format!("Page Up scrolls into the history (offset {})", report().offset));
                st.step = 0;
                st.still = 0;
                true
            }
        })),
        ("page down to the end", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                if !until_still(&mut st, 150, || headless::press(Key::PageDown)) {
                    return false;
                }
                let (visible, numbers) = last_row_visible();
                check(visible && report().at_end, &format!("Page Down reaches the end (at end {}, offset {}): {numbers}", report().at_end, report().offset));
                st.step = 0;
                st.still = 0;
                headless::press(Key::Home);
                true
            }
        })),
        // 7. End (and Ctrl+End) return to the latest and follow again.
        ("end", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            check(report().offset > -50.0 && !report().follows, &format!("Home reaches the top (offset {})", report().offset));
            headless::press(Key::End);
            true
        })),
        ("at the end", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(visible && report().follows && report().at_end, &format!("End returns to the latest and follows (follows {}): {numbers}", report().follows));
            upsert("scroll", json!([{"id": "scroll-after-end", "role": "assistant", "text": LOREM.repeat(5)}]));
            true
        })),
        ("follows after end", Box::new(|_, _, elapsed| {
            if !has_row("scroll-after-end") || elapsed < Duration::from_millis(500) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(visible && report().follows, &format!("after End a new reply stays in view: {numbers}"));
            for _ in 0..4 {
                headless::press(Key::PageUp);
            }
            true
        })),
        ("ctrl end", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            check(!report().follows, "Page Up leaves the end again");
            headless::press_with(&[Key::Control], Key::End);
            true
        })),
        ("at the end again", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(visible && report().follows && report().at_end, &format!("Ctrl+End returns to the latest and follows (follows {}): {numbers}", report().follows));
            true
        })),
        // 8. An older page loading above keeps the reader's place.
        ("older pages", Box::new(|_, _, _| {
            check(
                control("/__control/older-delay", &json!({"seconds": 1.5})).is_ok()
                    && control("/__control/add-agent", &json!({"session": "paged", "persona": "Paged"})).is_ok()
                    && control("/__control/rich", &json!({"session": "paged", "count": 350})).is_ok(),
                "the Host holds a 350-row chat whose older pages come slowly",
            );
            true
        })),
        ("open paged", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("paged").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("paged");
            crate::pump();
            true
        })),
        ("first page", Box::new({
            let state = s();
            move |_, window, elapsed| {
                let rows = rows(window);
                if rows.len() != 100 || elapsed < Duration::from_millis(1000) {
                    return false;
                }
                let (visible, numbers) = last_row_visible();
                check(visible, &format!("the paged chat opens at its end: {numbers}"));
                let mut st = state.borrow_mut();
                st.step = 0;
                st.still = 0;
                true
            }
        })),
        ("wheel to the top", Box::new({
            let state = s();
            move |_, window, _| {
                let mut st = state.borrow_mut();
                if rows(window).len() > 100 {
                    check(false, "the older page came before the reader stopped (delay too short)");
                    return true;
                }
                if !until_still(&mut st, 150, || wheel(400.0)) {
                    return false;
                }
                check(report().offset > -1.0, &format!("the wheel reaches the top (offset {})", report().offset));
                st.anchor = geometry().anchor();
                true
            }
        })),
        ("older page lands", Box::new({
            let state = s();
            move |_, window, _| {
                let mut st = state.borrow_mut();
                if rows(window).len() < 200 {
                    // Keep the place the reader has right before it lands.
                    st.anchor = geometry().anchor();
                    st.since = None;
                    return false;
                }
                if st.since.is_none() {
                    st.since = Some(Instant::now());
                }
                if !settled(&st, 600) {
                    return false;
                }
                let Some(anchor) = st.anchor.clone() else {
                    check(false, "a row shows at the top");
                    return true;
                };
                let (held, numbers) = anchor_moved(&anchor);
                check(held && !report().follows, &format!("an older page loading above keeps the reader's place (follows {}): {numbers}", report().follows));
                shot(&out3, "scroll-03-older-page");
                true
            }
        })),
    ];
    run_stages(stages);
}
