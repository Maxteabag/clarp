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
        // The transcript's own id is its pane's ("chat:…").
        match element.accessible_id() {
            Some(id) if id.starts_with("row:") => {
                if size.height > 0.0 {
                    geo.rows.push((id.trim_start_matches("row:").to_owned(), position.y, position.y + size.height));
                }
            }
            _ => (geo.left, geo.width, geo.top, geo.bottom) = (position.x, size.width, position.y, position.y + size.height),
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
pub(super) fn last_row_visible() -> (bool, String) {
    let geo = geometry();
    let id = last_id();
    match geo.find(&id) {
        Some((top, bottom)) => {
            // A row's accessible box leaves out its padding (half the
            // message spacing above and below), which the gap under it at
            // the end shows: a row that fits the view only without it is
            // cut at its top by the rest of the padding, as it should be.
            let padding = (geo.bottom - bottom).max(0.0);
            let fits = bottom - top + 2.0 * padding <= geo.bottom - geo.top + 1.0;
            let ok = bottom <= geo.bottom + 1.0 && bottom > geo.top + 1.0 && (top >= geo.top - 1.0 || !fits);
            (ok, format!("last row {id} at {top:.1}..{bottom:.1}, viewport {:.1}..{:.1} (gap {:.1}px; {})", geo.top, geo.bottom, geo.bottom - bottom, geo.drawn()))
        }
        None => (false, format!("last row {id} is not drawn (viewport {:.1}..{:.1}; {})", geo.top, geo.bottom, geo.drawn())),
    }
}

/// The topmost row on screen and its distance from the viewport's top.
pub(super) fn anchor_now() -> Option<(String, f32)> {
    geometry().anchor()
}

/// Row `id`'s place on screen, as `anchor_moved` compares it.
pub(super) fn place_of(id: &str) -> Option<(String, f32)> {
    let geo = geometry();
    geo.find(id).map(|(top, _)| (id.to_owned(), top - geo.top))
}

/// The drawn height of row `id` (0 when it is not drawn).
pub(super) fn row_height(id: &str) -> f32 {
    geometry().find(id).map_or(0.0, |(top, bottom)| bottom - top)
}

/// The reader's wheel, `px` up the chat.
pub(super) fn wheel_up(px: f32) {
    wheel(px);
}

/// How far the anchor row moved on screen since it was recorded.
pub(super) fn anchor_moved(anchor: &(String, f32)) -> (bool, String) {
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

/// Whether row `id`'s first line is at the viewport's top (its box starts
/// within the row's top padding of it), and the numbers either way.
fn at_top(id: &str) -> (bool, String) {
    let geo = geometry();
    match geo.find(id) {
        Some((top, bottom)) => {
            let from = top - geo.top;
            ((-1.0..=16.0).contains(&from), format!("row {id} at {top:.1}..{bottom:.1}, {from:.1}px below the viewport's top {:.1} ({})", geo.top, geo.drawn()))
        }
        None => (false, format!("row {id} is not drawn ({})", geo.drawn())),
    }
}

/// Whether the open pane shows the jump-to-latest dot (more below).
fn news() -> bool {
    app_now().active_view().is_some_and(|v| v.news)
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
    let below = (last + 1..rows.len()).find(tools).map(|i| rows[i].id.to_string());
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


fn event(body: serde_json::Value) {
    if let Err(error) = control("/__control/event", &body) {
        check(false, &format!("the Host pushes an event: {error}"));
    }
}

fn agent_state(session: &str, state: &str) {
    if let Err(error) = control("/__control/agent", &json!({"session": session, "set": {"latest_state": state}})) {
        check(false, &format!("the Host takes the agent's state: {error}"));
    }
    event(json!({"type": "agent-state", "session": session, "kind": state, "ts": 1}));
}

/// One working turn of an agent as the Host reports it: it starts
/// thinking, runs tools (activity rows that come and go), files tool-only
/// turns, streams a live reply that grows, then the durable reply
/// replaces the live row and the agent goes idle.
fn agent_turn(session: &'static str, turn: usize) -> Vec<(String, Box<dyn Fn()>)> {
    let id = move |name: &str| format!("{session}-t{turn}-{name}");
    let mut steps: Vec<(String, Box<dyn Fn()>)> = vec![
        ("the agent starts thinking".into(), Box::new(move || agent_state(session, "thinking"))),
        ("a tool starts".into(), Box::new(move || {
            agent_state(session, "tool");
            event(json!({"type": "agent-activity", "session": session, "state": "tool", "activity_tool": "Read",
                         "activity_file_path": format!("src/file{turn}.rs"), "activity_summary": format!("src/file{turn}.rs"), "activity_status": "running"}));
            event(json!({"type": "agent-activity", "session": session, "state": "tool", "activity_tool": "Bash",
                         "activity_summary": "cargo test --workspace", "activity_status": "running"}));
        })),
        ("the tools finish and are filed".into(), Box::new(move || {
            event(json!({"type": "agent-activity", "session": session, "state": "tool", "activity_tool": "Read",
                         "activity_file_path": format!("src/file{turn}.rs"), "activity_summary": format!("src/file{turn}.rs"), "activity_status": "ok"}));
            upsert(session, json!([
                {"id": id("tool-a"), "role": "assistant", "text": "", "tools": [{"name": "Read", "file_path": format!("src/file{turn}.rs"), "status": "completed", "result": "fn main() {}"}]},
                {"id": id("tool-b"), "role": "assistant", "text": "", "tools": [{"name": "Bash", "command": "cargo test --workspace", "status": "completed", "result": "test result: ok. 41 passed"}]}]));
        })),
        ("the agent thinks again".into(), Box::new(move || agent_state(session, "thinking"))),
    ];
    for n in 0..4 {
        steps.push((format!("the live reply grows ({n})"), Box::new(move || {
            upsert(session, json!([{"id": id("live"), "role": "assistant", "kind": "live", "text": streamed(n)}]));
        })));
    }
    steps.push(("the durable reply replaces the live one".into(), Box::new(move || {
        upsert(session, json!([{"id": id("reply"), "role": "assistant", "text": streamed(4)}]));
    })));
    steps.push(("the agent goes idle".into(), Box::new(move || agent_state(session, "idle"))));
    steps
}

/// A stage that plays `steps`, 500 ms apart, and checks after each that
/// the reader's first visible row has not moved from where it was when
/// the stage began.
fn reader_holds(state: Rc<RefCell<State>>, label: &'static str, steps: Vec<(String, Box<dyn Fn()>)>) -> Stage {
    (label, Box::new(move |_, _, _| {
        let mut st = state.borrow_mut();
        if st.step == 0 {
            st.anchor = geometry().anchor();
            st.since = None;
        }
        if st.step > 0 {
            if st.since.is_none() {
                st.since = Some(Instant::now());
            }
            if !settled(&st, 500) {
                return false;
            }
            let Some(anchor) = st.anchor.clone() else {
                check(false, &format!("{label}: a row shows"));
                return true;
            };
            let (held, numbers) = anchor_moved(&anchor);
            check(held && !report().follows, &format!("{label}, {}: the reader stays put (follows {}): {numbers}", steps[st.step - 1].0, report().follows));
        }
        st.since = None;
        if st.step == steps.len() {
            st.step = 0;
            return true;
        }
        (steps[st.step].1)();
        st.step += 1;
        false
    }))
}

/// The row drawn nearest the viewport's middle and its top's distance
/// from the viewport's top: it stays drawn through a wheel notch.
fn middle_row() -> Option<(String, f32)> {
    let geo = geometry();
    let middle = (geo.top + geo.bottom) / 2.0;
    geo.rows
        .iter()
        .filter(|r| r.2 > geo.top && r.1 < geo.bottom)
        .min_by(|a, b| (a.1 - middle).abs().total_cmp(&(b.1 - middle).abs()))
        .map(|r| (r.0.clone(), r.1 - geo.top))
}

/// A stage of `count` wheel notches of `delta`, 300 ms apart: each must
/// move the rows by exactly `delta` on screen (the wheel's own distance),
/// whatever the agent does meanwhile (`stream`: a reply growing each notch).
fn wheel_steps(state: Rc<RefCell<State>>, label: &'static str, delta: f32, count: usize, stream: Option<&'static str>) -> Stage {
    (label, Box::new(move |_, _, _| {
        let mut st = state.borrow_mut();
        if st.step > 0 {
            if st.since.is_none() {
                st.since = Some(Instant::now());
            }
            if !settled(&st, 300) {
                return false;
            }
            if let Some(anchor) = st.anchor.clone() {
                let geo = geometry();
                let line = match geo.find(&anchor.0) {
                    Some((top, _)) => {
                        let moved = (top - geo.top) - anchor.1;
                        let ok = (moved - delta).abs() <= 2.0;
                        if !ok {
                            st.still += 1;
                        }
                        format!("{}notch {}: {} moved {moved:.1}px", if ok { "" } else { "WRONG " }, st.step, anchor.0)
                    }
                    None => {
                        st.still += 1;
                        format!("WRONG notch {}: {} vanished ({})", st.step, anchor.0, geo.drawn())
                    }
                };
                st.target.push_str(&line);
                st.target.push_str("; ");
            }
        }
        st.since = None;
        if st.step == count {
            if let Some(session) = stream {
                upsert(session, json!([{"id": format!("{session}-{}", label.replace(' ', "-")), "role": "assistant", "kind": "", "text": streamed(count)}]));
            }
            let wrong = st.still;
            check(wrong == 0, &format!("{label}: every notch of {delta}px moves the rows {delta}px ({wrong} of {count} wrong): {}", st.target));
            check(!report().follows, &format!("{label}: the reader is not following"));
            st.step = 0;
            st.still = 0;
            st.target.clear();
            return true;
        }
        if st.step == 0 {
            st.still = 0;
            st.target.clear();
        }
        if let Some(session) = stream {
            upsert(session, json!([{"id": format!("{session}-{}", label.replace(' ', "-")), "role": "assistant", "kind": "live", "text": streamed(st.step)}]));
        }
        st.anchor = middle_row();
        wheel(delta);
        st.step += 1;
        false
    }))
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
    let (out1, out2, out3, out4, out5) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
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
        // 2. One wheel notch up from the end leaves the end and stays there.
        ("one notch up", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                st.step += 1;
                if st.step == 1 {
                    st.anchor = middle_row();
                    wheel(120.0);
                    return false;
                }
                if st.step < 8 {
                    return false;
                }
                let geo = geometry();
                let moved = st.anchor.as_ref().and_then(|a| geo.find(&a.0).map(|(top, _)| top - geo.top - a.1));
                check(
                    moved.is_some_and(|m| (m - 120.0).abs() <= 2.0) && !report().follows,
                    &format!("one wheel notch (120px) up from the end moves the rows down {moved:?}px and stops following (follows {})", report().follows),
                );
                st.step = 0;
                true
            }
        })),
        // 3. The owner's bug: an agent works while the reader is just above
        // the end (the latest rows partly on screen).
        reader_holds(s(), "an agent works just above the reader's place", agent_turn("scroll", 1)),
        // 4. The wheel scrolls up into the history and stops following.
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
        // 5. An agent works and its reply streams while the reader is deep
        // in the history; their place must not change at all (measured
        // against where they stopped, not step by step).
        ("streams while reading", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let step = st.step;
                if step > 0 {
                    if !holds("stream-1", &streamed(step - 1)) {
                        return false;
                    }
                    if st.since.is_none() {
                        st.since = Some(Instant::now());
                    }
                    if !settled(&st, 400) {
                        return false;
                    }
                    let Some(anchor) = st.anchor.clone() else {
                        check(false, "a row shows after wheeling up");
                        return true;
                    };
                    let (held, numbers) = anchor_moved(&anchor);
                    check(held && !report().follows, &format!("an agent streams (update {step} of {streams}) while the reader is up: the reader stays put (follows {}): {numbers}", report().follows));
                }
                st.since = None;
                st.step += 1;
                if step == 0 {
                    check(control("/__control/agent", &json!({"session": "scroll", "set": {"latest_state": "thinking"}})).is_ok(), "the agent starts working");
                }
                if st.step <= streams {
                    upsert("scroll", json!([{"id": "stream-1", "role": "assistant", "text": streamed(st.step - 1)}]));
                    return false;
                }
                st.step = 0;
                st.anchor = geometry().anchor();
                true
            }
        })),
        reader_holds(s(), "an agent works while the reader is deep in the history", agent_turn("scroll", 2)),
        // Each wheel notch moves the rows by its own distance, with and
        // without an agent streaming meanwhile.
        wheel_steps(s(), "wheeling up in the history", 120.0, 8, None),
        wheel_steps(s(), "wheeling down in the history", -120.0, 8, None),
        wheel_steps(s(), "wheeling up while an agent streams", 120.0, 8, Some("scroll")),
        wheel_steps(s(), "wheeling down while an agent streams", -120.0, 8, Some("scroll")),
        // 6. Other activity does not move a reader in the history either.
        ("activity while reading", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let step = st.step;
                if step == 0 {
                    st.anchor = geometry().anchor();
                    (st.above, st.below) = tool_rows_around();
                }
                if step > 0 {
                    let landed = match step {
                        1 => has_row("scroll-new-3"),
                        2 => st.below.as_deref().is_none_or(expanded),
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
                    let what = ["", "three rows arrive", "tool calls open below the reader", "tool calls open above the reader"][step];
                    let Some(anchor) = st.anchor.clone() else { return true };
                    let (held, numbers) = anchor_moved(&anchor);
                    check(held && !report().follows, &format!("{what}: the reader stays put (follows {}): {numbers}", report().follows));
                    // Measure each change on its own.
                    st.anchor = geometry().anchor();
                    (st.above, st.below) = tool_rows_around();
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
                    2 => match st.below.clone() {
                        Some(id) => toggle(&id),
                        None => check(false, "a row with tool calls lies below the reader"),
                    },
                    3 => match st.above.clone() {
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
        // 7. The wheel reaches the true end and following resumes.
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
        // 8. Following a streaming reply: it is followed until its first
        // line reaches the top of the view; then the view holds there while
        // it grows, and the dot says there is more below.
        ("follows a stream", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let step = st.step;
                // Each update: wait for it to land, then measure.
                if step > 0 {
                    if !holds("stream-2", &streamed(step - 1)) {
                        return false;
                    }
                    if st.since.is_none() {
                        st.since = Some(Instant::now());
                    }
                    if !settled(&st, 250) {
                        return false;
                    }
                    let (visible, numbers) = last_row_visible();
                    let (top, placed) = at_top("stream-2");
                    if st.still == 0 && report().follows {
                        check(visible, &format!("following, update {step} keeps the reply's end in view: {numbers}"));
                    } else {
                        // Stopped: from now on the reply's first line stays at the top.
                        st.still = 1;
                        check(top && !report().follows, &format!("update {step}: following stopped with the reply's first line at the top (follows {}): {placed}", report().follows));
                        check(news(), &format!("update {step}: the jump-to-latest dot says there is more below"));
                    }
                }
                st.since = None;
                st.step += 1;
                let next = st.step;
                if next <= streams {
                    upsert("scroll", json!([{"id": "stream-2", "role": "assistant", "text": streamed(next - 1)}]));
                    return false;
                }
                check(st.still == 1, &format!("a reply {:.0}px tall in a {:.0}px view stops being followed at its first line", row_height("stream-2"), { let g = geometry(); g.bottom - g.top }));
                shot(&out4, "scroll-03-stopped-at-first-line");
                st.step = 0;
                st.still = 0;
                true
            }
        })),
        // Back at the end (End), a short reply is followed to its end.
        ("short reply", Box::new(|app, _, _| {
            app.to_latest();
            true
        })),
        ("short reply arrives", Box::new({
            let state = s();
            move |_, _, elapsed| {
                let mut st = state.borrow_mut();
                if st.step == 0 {
                    if elapsed < Duration::from_millis(500) {
                        return false;
                    }
                    check(report().follows && report().at_end && !news(), &format!("End follows again and the news is read (follows {}, news {})", report().follows, news()));
                    upsert("scroll", json!([{"id": "scroll-user-2", "role": "user", "text": "Thanks, and the docs?"},
                                            {"id": "scroll-reply-2", "role": "assistant", "text": LOREM.repeat(4)}]));
                    st.step = 1;
                    return false;
                }
                if !has_row("scroll-reply-2") || elapsed < Duration::from_millis(1000) {
                    return false;
                }
                let (visible, numbers) = last_row_visible();
                check(visible && report().follows, &format!("a reply shorter than the view is followed to its end (follows {}): {numbers}", report().follows));
                st.step = 0;
                true
            }
        })),
        // A reply many views tall arriving whole: its first line ends at the top.
        ("long reply arrives", Box::new(|_, _, _| {
            upsert("scroll", json!([{"id": "scroll-long", "role": "assistant", "text": format!("{}\n\n{}", "First line of the long reply.", LOREM.repeat(80))}]));
            true
        })),
        ("long reply read from its top", Box::new(|_, _, elapsed| {
            if !has_row("scroll-long") || elapsed < Duration::from_millis(1200) {
                return false;
            }
            let (top, placed) = at_top("scroll-long");
            check(top && !report().follows && !report().at_end, &format!("a reply many views tall stops at its first line (follows {}, at end {}): {placed}", report().follows, report().at_end));
            check(news(), "and the jump-to-latest dot says there is more below");
            true
        })),
        // Opening the chat again lands on its newest message, as always.
        ("away again", Box::new(|app, _, _| {
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("back again", Box::new(|app, _, elapsed| {
            if app.engine.borrow().selected_session() != "rachel" || elapsed < Duration::from_millis(400) {
                return false;
            }
            app.engine.borrow_mut().select("scroll");
            crate::pump();
            true
        })),
        ("opens at the newest", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(1200) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(visible && report().follows && report().at_end, &format!("the chat opened again lands on its newest message and follows (follows {}): {numbers}", report().follows));
            app_now().focus_transcript();
            true
        })),
        // 9. Page Down from the history reaches the true end.
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
        // 10. End (and Ctrl+End) return to the latest and follow again.
        ("end", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            check(report().offset > -50.0 && !report().follows, &format!("Home reaches the top (offset {})", report().offset));
            true
        })),
        reader_holds(s(), "an agent works while the reader is at the top", agent_turn("scroll", 4)),
        ("press end", Box::new(|_, _, _| {
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
        // A touchpad swipe up from the end (gesture phases, then a glide)
        // stops following, and the agent's activity leaves the reader alone.
        ("touchpad swipe up", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let geo = geometry();
                let (x, y) = (geo.left + geo.width / 2.0, (geo.top + geo.bottom) / 2.0);
                st.step += 1;
                match st.step {
                    1 => headless::scroll(x, y, 0.0, headless::Phase::Started),
                    2..=7 => headless::scroll(x, y, 40.0, headless::Phase::Moved),
                    8 => headless::scroll(x, y, 0.0, headless::Phase::Ended),
                    9..=20 => {}
                    _ => {
                        check(!report().follows && !report().at_end, &format!("a touchpad swipe up from the end stops following (follows {}, at end {})", report().follows, report().at_end));
                        st.step = 0;
                        return true;
                    }
                }
                false
            }
        })),
        reader_holds(s(), "an agent works after a touchpad swipe up", agent_turn("scroll", 3)),
        // 11. An older page loading above keeps the reader's place.
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
        // 12. A long reply in Conversation view, as the Host sends a turn
        // with no live items: the user's message, the Running box at the
        // end while the agent works, then the reply in the box's place (the
        // row count as it was). It stops at its first line all the same.
        ("conversation chat", Box::new(|app, _, _| {
            let mut turns: Vec<serde_json::Value> = (0..14)
                .map(|i| json!({"id": format!("convo-{i}"), "role": if i % 2 == 0 { "user" } else { "assistant" }, "text": LOREM.repeat(2),
                                "view": "conversation", "phase": "final", "timestamp": "2026-11-25T09:00:00Z"}))
                .collect();
            turns.push(json!({"id": "convo-hello", "role": "assistant", "text": "Hello.", "view": "conversation", "phase": "final", "timestamp": "2026-11-25T09:00:00Z"}));
            check(
                control("/__control/add-agent", &json!({"session": "convo", "persona": "Convo"})).is_ok()
                    && control("/__control/turns", &json!({"session": "convo", "turns": turns})).is_ok()
                    && control("/__control/agent", &json!({"session": "convo", "set": {"chat_view": "conversation"}})).is_ok()
                    && control("/__control/chat-view", &json!({"on": true})).is_ok(),
                "the Host holds a chat in Conversation view",
            );
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("open conversation chat", Box::new(|app, _, _| {
            if !app.engine.borrow().chat_view_offered("convo") {
                return false;
            }
            app.engine.borrow_mut().select("convo");
            crate::pump();
            true
        })),
        ("conversation opens at the newest", Box::new(|_, _, elapsed| {
            if !has_row("convo-hello") || elapsed < Duration::from_millis(1200) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(visible && report().follows, &format!("the conversation chat opens at its newest message and follows (follows {}): {numbers}", report().follows));
            // The user's message lands and the agent works on it: no live
            // turn says so, so a Running box stands at the end.
            upsert("convo", json!([{"id": "convo-ask-a", "role": "user", "text": "Write a detailed guide to starting a small bakery.", "origin": "user",
                                    "trace_id": "convo-ta", "view": "conversation", "timestamp": "2026-11-25T09:01:00Z"}]));
            check(control("/__control/agent", &json!({"session": "convo", "set": {"latest_state": "thinking", "busy": true, "turn_started_at": now_ms() - 5_000}})).is_ok(), "the agent starts working");
            true
        })),
        ("running box at the end", Box::new(|_, _, elapsed| {
            if last_id() != "live:fold:convo-ta" || elapsed < Duration::from_millis(800) {
                return false;
            }
            let (visible, numbers) = last_row_visible();
            check(visible && report().follows, &format!("the Running box is followed at the end (follows {}): {numbers}", report().follows));
            // The turn ends: the reply takes the box's place.
            upsert("convo", json!([{"id": "convo-long-a", "role": "assistant", "text": format!("{}\n\n{}", "First line of the bakery guide.", LOREM.repeat(80)),
                                    "phase": "final", "trace_id": "convo-ta", "view": "conversation", "session_rows": 0, "timestamp": "2026-11-25T09:01:50Z",
                                    "turn": {"turn_id": "convo-ta", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 51_000, "worked_ms": 50_000, "tool_count": 0}}]));
            check(control("/__control/agent", &json!({"session": "convo", "set": {"latest_state": "idle", "busy": false}})).is_ok(), "the agent goes idle");
            true
        })),
        ("reply in the box's place read from its top", Box::new({
            let out = out5;
            move |_, _, elapsed| {
                if !has_row("convo-long-a") || has_row("live:fold:convo-ta") || elapsed < Duration::from_millis(1500) {
                    return false;
                }
                let (top, placed) = at_top("convo-long-a");
                check(top && !report().follows && !report().at_end, &format!("a long reply in the Running box's place stops at its first line (follows {}, at end {}): {placed}", report().follows, report().at_end));
                check(news(), "and the jump-to-latest dot says there is more below");
                shot(&out, "scroll-04-conversation-reply-top");
                app_now().to_latest();
                true
            }
        })),
        // A reply's row that comes first without text and fills in place.
        ("reply row fills in place", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(report().follows && report().at_end, &format!("End follows again (follows {}, at end {})", report().follows, report().at_end));
            upsert("convo", json!([{"id": "convo-ask-b", "role": "user", "text": "And the permits?", "origin": "user", "trace_id": "convo-tb", "view": "conversation", "timestamp": "2026-11-25T09:02:00Z"},
                                   {"id": "convo-long-b", "role": "assistant", "text": "", "phase": "final", "trace_id": "convo-tb", "view": "conversation", "session_rows": 0, "timestamp": "2026-11-25T09:02:01Z"}]));
            true
        })),
        ("empty reply row followed", Box::new(|_, _, elapsed| {
            if !has_row("convo-long-b") || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(report().follows, &format!("the empty reply row is followed (follows {})", report().follows));
            upsert("convo", json!([{"id": "convo-long-b", "role": "assistant", "text": format!("{}\n\n{}", "First line of the permits answer.", LOREM.repeat(80)),
                                    "phase": "final", "trace_id": "convo-tb", "view": "conversation", "session_rows": 0, "timestamp": "2026-11-25T09:02:40Z"}]));
            true
        })),
        ("filled reply read from its top", Box::new(|_, _, elapsed| {
            if !holds("convo-long-b", &format!("{}\n\n{}", "First line of the permits answer.", LOREM.repeat(80))) || elapsed < Duration::from_millis(1500) {
                return false;
            }
            let (top, placed) = at_top("convo-long-b");
            check(top && !report().follows && !report().at_end, &format!("a reply that fills its row in place stops at its first line (follows {}, at end {}): {placed}", report().follows, report().at_end));
            check(news(), "and the jump-to-latest dot says there is more below");
            true
        })),
    ];
    run_stages(stages);
}

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}

// ---- the chat jumping far up (`--check scroll-jump`)

/// What the reader sees: the drawn rows' indexes in the chat, the first
/// one's id, and whether the last row is whole on screen.
#[derive(Debug, Clone, Default)]
struct Seen {
    session: String,
    first: usize,
    last: usize,
    first_id: String,
    at_end: bool,
    offset: f32,
    follows: bool,
    drawn: String,
}

impl Seen {
    fn line(&self) -> String {
        format!("rows #{}..#{} (first {}), offset {:.0}, follows {}, last row whole {}", self.first, self.last, self.first_id, self.offset, self.follows, self.at_end)
    }
}

/// None while more than one transcript is drawn (a split pane).
fn seen() -> Option<Seen> {
    let app = app_now();
    if app.pane_state.borrow().len() != 1 {
        return None;
    }
    let geo = geometry();
    let rows = crate::window().map(|w| rows(&w)).unwrap_or_default();
    let index = |id: &str| rows.iter().position(|r| r.id == id);
    let shown: Vec<usize> = geo.rows.iter().filter(|r| r.2 > geo.top + 1.0 && r.1 < geo.bottom - 1.0).filter_map(|r| index(&r.0)).collect();
    let (first, last) = (*shown.iter().min()?, *shown.iter().max()?);
    Some(Seen {
        session: app.engine.borrow().selected_session().to_owned(),
        first,
        last,
        first_id: rows[first].id.to_string(),
        at_end: last_row_visible().0,
        offset: report().offset,
        follows: report().follows,
        drawn: geo.drawn(),
    })
}

const JUMP_ROWS: usize = 320;
/// 2026-09-15T08:00:00Z, the rich chat's stamp: cards made then sit on its
/// first reply, far up; the replies after it are stamped later.
const OLD_CARD_MS: i64 = 1_789_459_200_000;

#[derive(Default)]
struct Jump {
    before: Option<Seen>,
    /// The lowest first row drawn since the event, and how many polls the
    /// reader spent wholly above the rows they saw before it.
    lowest: Option<Seen>,
    above: usize,
    done: usize,
    stable: usize,
    begun: bool,
    /// The scroll journal's lines before the event.
    journal: usize,
    /// The dashcam's flashes before the event.
    flashes: usize,
}

type Act = Box<dyn Fn(&Rc<crate::App>, &crate::AppWindow)>;

/// A reader at the end and following (or, `following` false, one wheel
/// notch up from it), `focus` on the transcript or the composer; then
/// `steps` run at their times and the reader is watched for `watch_ms`
/// (and until `ready`). A reader at the end must end at the end, and no
/// poll may find them more than a viewport above where they were.
fn jump_case(state: Rc<RefCell<Jump>>, label: &'static str, following: bool, transcript: bool, steps: Vec<(u64, Act)>, watch_ms: u64, ready: Box<dyn Fn() -> bool>) -> Vec<Stage> {
    jump_case_from(state, label, following, transcript, steps, watch_ms, ready, None)
}

/// `jump_case`, with the reader's place taken again at `rebase` ms (after
/// steps that moved them on purpose).
#[allow(clippy::too_many_arguments)]
fn jump_case_from(state: Rc<RefCell<Jump>>, label: &'static str, following: bool, transcript: bool, steps: Vec<(u64, Act)>, watch_ms: u64, ready: Box<dyn Fn() -> bool>, rebase: Option<u64>) -> Vec<Stage> {
    let place = state.clone();
    let place_label: &'static str = Box::leak(format!("place: {label}").into_boxed_str());
    vec![
        (place_label, Box::new(move |app, _, elapsed| {
            let mut st = place.borrow_mut();
            if !st.begun {
                *st = Jump { begun: true, ..Jump::default() };
                crate::artifacts_view::leave(app);
                app.to_latest();
                return false;
            }
            let _ = elapsed;
            if transcript && !report().transcript_focused {
                app.focus_transcript();
                return false;
            }
            if !transcript && !report().composer_focused {
                app.focus_composer();
                return false;
            }
            let Some(now) = seen() else { return false };
            if !(now.at_end && now.follows) && st.done == 0 {
                st.stable = 0;
                app.to_latest();
                return false;
            }
            st.stable += 1;
            if st.stable < 6 {
                return false;
            }
            if !following {
                // One wheel notch up, then still for a moment.
                st.done += 1;
                if st.done == 1 {
                    wheel(120.0);
                    return false;
                }
                if st.done < 10 {
                    return false;
                }
                if now.follows {
                    check(false, &format!("{label}: one notch up stops following ({})", now.line()));
                }
            }
            st.before = Some(now);
            st.done = 0;
            st.journal = crate::scroll_journal::logged().len();
            st.flashes = crate::scroll_journal::flashes_seen();
            true
        })),
        (label, Box::new(move |_, window, elapsed| {
            let app = app_now();
            let mut st = state.borrow_mut();
            let ms = elapsed.as_millis() as u64;
            while st.done < steps.len() && ms >= steps[st.done].0 {
                (steps[st.done].1)(&app, window);
                st.done += 1;
            }
            if rebase.is_some_and(|at| ms >= at) && st.stable != usize::MAX {
                st.stable = usize::MAX;
                st.before = seen();
                st.above = 0;
                st.lowest = None;
            }
            let Some(before) = st.before.clone() else { return true };
            if let Some(now) = seen() {
                if std::env::var_os("CLARP_JUMP_TRACE").is_some() { eprintln!("trace {label} {ms}ms: {} ({})", now.line(), now.drawn); }
                if now.session == before.session && now.last < before.first {
                    st.above += 1;
                }
                if now.session == before.session && st.lowest.as_ref().is_none_or(|l| now.first < l.first) {
                    st.lowest = Some(now);
                }
            }
            if ms < watch_ms || st.done < steps.len() || (!ready() && ms < 12_000) {
                return false;
            }
            let after = seen().unwrap_or_default();
            let lowest = st.lowest.clone().unwrap_or_default();
            let journal: Vec<String> = crate::scroll_journal::logged().split_off(st.journal);
            // A reply taller than most of the view is followed only until its
            // first line reaches the top (#825): the reader stays there, the
            // reply's first row at the top of the view.
            let stopped = !after.follows && (after.first_id == "jump-reply" || after.first_id == "jump-live");
            if following && stopped {
                check(after.session == before.session && after.first > before.first, &format!("{label}: a long reply stops at its first line: before {}; after {} ({})", before.line(), after.line(), after.drawn));
            } else if following {
                check(after.at_end && after.follows, &format!("{label}: a reader at the end stays at the end: before {}; after {} ({})", before.line(), after.line(), after.drawn));
            } else {
                check(after.last >= before.first, &format!("{label}: a reader one notch up stays within a viewport: before {}; after {} ({})", before.line(), after.line(), after.drawn));
            }
            check(st.above == 0, &format!("{label}: never more than a viewport up meanwhile ({} polls wholly above; highest place rows #{}..#{}, before #{}..#{})", st.above, lowest.first, lowest.last, before.first, before.last));
            if following {
                check(!journal.iter().any(|c| c == "follow"), &format!("{label}: following, the scroll journal logs no pins (logged {journal:?})"));
            }
            let flashes = crate::scroll_journal::flashes_seen() - st.flashes;
            check(flashes == 0, &format!("{label}: the dashcam finds no flash ({flashes} found)"));
            *st = Jump::default();
            true
        })),
    ]
}

fn act(f: impl Fn(&Rc<crate::App>, &crate::AppWindow) + 'static) -> Act {
    Box::new(f)
}

fn always() -> Box<dyn Fn() -> bool> {
    Box::new(|| true)
}

fn live() -> Box<dyn Fn() -> bool> {
    Box::new(|| app_now().engine.borrow().connection_state() == "live")
}

/// The cards, as the Host lists them: two on the chat's first replies
/// (rows 1 and 2, its rows a minute apart) and one in its middle (row 161).
fn old_cards(summary: &str) -> serde_json::Value {
    let made = [30_000, 90_000, 160 * 60_000 - 30_000];
    json!(made.iter().enumerate().map(|(n, at)| json!({"artifact_id": format!("jump-card-{n}"), "type": "document", "status": "ready", "session": "jump",
        "title": format!("Findings {n}"), "summary": summary, "content": "The **report**.", "created_at": OLD_CARD_MS + at, "updated_at": OLD_CARD_MS + at})).collect::<Vec<_>>())
}

const MID_CARD: &str = "jump-card-2";

/// `--check scroll-jump --out DIR`: nothing but the reader's own scrolling
/// moves them far up. A 320-row chat with artifact cards only near its
/// top: a reader following at its end (and one a notch above it) goes
/// through J and K in the chat, refreshes (also a failing one), the Host's
/// stream dropping, a switch to another chat and back, splitting and
/// zooming a pane, a theme change, timestamps on and off, a long streaming
/// reply and the far cards changing.
pub(super) fn scroll_jump_check(out: String) {
    let state = Rc::new(RefCell::new(Jump::default()));
    let s = || state.clone();
    let out1 = out.clone();
    let mut stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/add-agent", &json!({"session": "jump", "persona": "Jump"})).is_ok(), "the Host takes a new chat");
            true
        })),
        ("select", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("jump").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("jump");
            crate::pump();
            true
        })),
        ("fill", Box::new(|app, _, elapsed| {
            if app.engine.borrow().conversation("jump").is_none() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(control("/__control/rich", &json!({"session": "jump", "count": JUMP_ROWS, "stamp_step": 60})).is_ok(), &format!("the Host fills the chat with {JUMP_ROWS} varied rows"));
            check(control("/__control/artifact-update", &json!({"session": "jump", "add": old_cards("What we found.")})).is_ok(), "the Host lists three cards made near the chat's top");
            true
        })),
        ("filled", Box::new(move |app, window, elapsed| {
            let cards = crate::artifacts_view::selectables(app);
            if rows(window).len() < JUMP_ROWS || cards.len() < 3 || elapsed < Duration::from_millis(1500) {
                return false;
            }
            let rows = rows(window);
            let at: Vec<usize> = rows.iter().enumerate().filter(|(_, r)| slint::Model::row_count(&r.artifacts) > 0).map(|(i, _)| i).collect();
            check(at == [1, 2, 161], &format!("two cards sit near the top and one in the middle, none near the end: rows {at:?} of {}", rows.len()));
            shot(&out1, "scroll-jump-01-opened");
            true
        })),
    ];
    let k = || act(|_, _| headless::press("k"));
    let j = || act(|_, _| headless::press("j"));
    let refresh = || act(|app, window| { crate::commands::run(app, window, "refresh"); });
    // Suspect 1: J/K with no card on screen.
    stages.extend(jump_case(s(), "K in the chat with no card on screen", true, true, vec![(0, k())], 3000, always()));
    stages.extend(jump_case(s(), "J in the chat with no card on screen", true, true, vec![(0, j())], 3000, always()));
    // Suspect 2: a reader who reached the top of a chat with no older page
    // waits there; the list re-estimating its rows' heights must not move them.
    stages.extend::<Vec<Stage>>(vec![
        ("home", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(100) {
                app.to_latest();
                app.focus_transcript();
                return false;
            }
            if !report().transcript_focused || elapsed < Duration::from_millis(800) {
                return false;
            }
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("at the top", Box::new({
            let state = s();
            move |_, _, elapsed| {
                if elapsed < Duration::from_millis(800) {
                    return false;
                }
                let mut st = state.borrow_mut();
                st.before = seen();
                st.lowest = st.before.clone();
                st.above = 0;
                let journal = crate::scroll_journal::logged();
                let recent = &journal[journal.len().saturating_sub(3)..];
                check(recent.iter().any(|c| c == "key:Home"), &format!("the scroll journal logs Home's move (last causes {recent:?})"));
                true
            }
        })),
        ("waits at the top", Box::new({
            let state = s();
            move |_, _, elapsed| {
                let mut st = state.borrow_mut();
                if let Some(now) = seen() {
                    if st.lowest.as_ref().is_none_or(|l| now.first > l.first) {
                        st.lowest = Some(now);
                    }
                }
                if elapsed < Duration::from_secs(3) {
                    return false;
                }
                let (before, after, furthest) = (st.before.clone().unwrap_or_default(), seen().unwrap_or_default(), st.lowest.clone().unwrap_or_default());
                check(before.first == 0 && after.first == 0 && furthest.first <= before.last, &format!(
                    "Home reaches the top of a chat with no older page, and the reader stays there: at the top {}; furthest rows #{}..#{}; after 3 s {}", before.line(), furthest.first, furthest.last, after.line()));
                *st = Jump::default();
                true
            }
        })),
    ]);
    // A seek still under way after the reader went back to the latest
    // (Ctrl+End): from the top, K selects a card there and J sets off for
    // the one in the middle.
    let j_to_mid = || act(|app, _| if *app.artifact_cursor.borrow() != MID_CARD { headless::press("j") });
    stages.extend(jump_case_from(s(), "Ctrl+End while J is still bringing a card into view", true, true, vec![
        (0, act(|_, _| headless::press(slint::platform::Key::Home))),
        (1200, k()),
        (1800, j_to_mid()),
        (2400, j_to_mid()),
        (2700, act(|_, _| headless::press_with(&[slint::platform::Key::Control], slint::platform::Key::End)))], 7000, always(), Some(3300)));
    // The owner clicks the chat (to bring the window forward, or on a
    // message) and types a reply: the click gave the chat the keyboard.
    stages.extend(jump_case(s(), "a click on the chat, then typing \"ok\"", true, false, vec![
        (0, act(|_, window| {
            let geo = geometry();
            super::click_at(window, geo.left + geo.width * 0.6, (geo.top + geo.bottom) / 2.0);
        })),
        (300, act(|_, _| headless::type_text("ok")))], 3000, always()));
    // Suspect 3: reloads and layout.
    stages.extend(jump_case(s(), "F5 refreshes the chat", true, true, vec![(0, refresh())], 1500, always()));
    stages.extend(jump_case(s(), "F5 with the Host timing out (504)", true, true, vec![
        (0, act(|_, _| { check(control("/__control/fail", &json!({"path": "/log", "status": 504, "count": 1})).is_ok(), "the next fetch fails"); })),
        (50, refresh()),
        (1500, refresh())], 3000, always()));
    stages.extend(jump_case(s(), "the Host's stream drops and reconnects", true, true, vec![
        (0, act(|_, _| { check(control("/__control/outage", &json!({"seconds": 1.5})).is_ok(), "the Host drops its streams"); }))], 4000, live()));
    // (Opening a chat empties the list and fills it again: the probe can
    // find its top laid out once before the end, between frames.)
    stages.extend(jump_case_from(s(), "another chat and back", true, true, vec![
        (0, act(|app, _| { app.engine.borrow_mut().select("rachel"); crate::pump(); })),
        (800, act(|app, _| { app.engine.borrow_mut().select("jump"); crate::pump(); }))], 2500, always(), Some(1300)));
    stages.extend(jump_case(s(), "a split, zoom and close", true, true, vec![
        (0, act(|app, window| { crate::commands::run(app, window, "split-right"); })),
        (700, act(|app, window| { crate::commands::run(app, window, "zoom"); })),
        (1400, act(|app, window| { crate::commands::run(app, window, "zoom"); })),
        (2100, act(|app, window| { crate::commands::run(app, window, "close-pane"); }))], 3500, always()));
    stages.extend(jump_case(s(), "a reading theme change", true, true, vec![
        (0, act(|app, window| { crate::commands::run(app, window, "setting:reading:paper"); })),
        (1000, act(|app, window| { crate::commands::run(app, window, "setting:reading:night"); }))], 2500, always()));
    stages.extend(jump_case(s(), "timestamps off and on", true, true, vec![
        (0, act(|app, window| { crate::commands::run(app, window, "setting:timestampsVisible"); })),
        (1000, act(|app, window| { crate::commands::run(app, window, "setting:timestampsVisible"); }))], 2500, always()));
    stages.extend(jump_case(s(), "a long reply streams in", true, false, (0..10).map(|n| {
        (n * 250, act(move |_, _| { upsert("jump", json!([{"id": "jump-live", "role": "assistant", "kind": "live", "text": streamed(n as usize)}])); }))
    }).chain(std::iter::once((2600, act(|_, _| { upsert("jump", json!([{"id": "jump-reply", "role": "assistant", "text": streamed(10)}])); })))).collect(), 4000, always()));
    stages.extend(jump_case(s(), "the far cards change", true, true, vec![
        (0, act(|_, _| { check(control("/__control/artifacts", &json!({"session": "jump", "artifacts": old_cards(&"Much more was found. ".repeat(12))})).is_ok(), "the far cards grow"); }))], 2000, always()));
    // A reader a notch above the end.
    stages.extend(jump_case(s(), "K a notch above the end", false, true, vec![(0, k())], 3000, always()));
    stages.extend(jump_case(s(), "J a notch above the end", false, true, vec![(0, j())], 3000, always()));
    stages.extend(jump_case(s(), "F5 a notch above the end", false, true, vec![(0, refresh())], 1500, always()));
    stages.extend(jump_case(s(), "the stream reconnects a notch above the end", false, true, vec![
        (0, act(|_, _| { check(control("/__control/outage", &json!({"seconds": 1.5})).is_ok(), "the Host drops its streams"); }))], 4000, live()));
    stages.extend(jump_case(s(), "a theme change a notch above the end", false, true, vec![
        (0, act(|app, window| { crate::commands::run(app, window, "setting:reading:paper"); }))], 1500, always()));
    stages.extend(jump_case(s(), "the far cards change a notch above the end", false, true, vec![
        (0, act(|_, _| { check(control("/__control/artifacts", &json!({"session": "jump", "artifacts": old_cards("Short again.")})).is_ok(), "the far cards shrink"); }))], 2000, always()));
    stages.extend(jump_case(s(), "a split and close a notch above the end", false, true, vec![
        (0, act(|app, window| { crate::commands::run(app, window, "split-right"); })),
        (1000, act(|app, window| { crate::commands::run(app, window, "close-pane"); }))], 2500, always()));
    stages.extend(stop_at_end_stages(out.clone()));
    run_stages(stages);
}

/// A Stop is the reader's own act (#846): a reader following a reply to
/// the end who stops it stays at the end while the stopped turn settles
/// (the partial answer filed under its own id, taller than the view, with
/// the line saying it was stopped), never pulled up to its first line.
fn stop_at_end_stages(out: String) -> Vec<Stage> {
    vec![
        ("stop: at the end", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                app.to_latest();
                return false;
            }
            if !(report().at_end && report().follows) {
                check(elapsed < Duration::from_secs(5), "the reader is at the end, following");
                return elapsed >= Duration::from_secs(5);
            }
            agent_state("jump", "thinking");
            upsert("jump", json!([{"id": "jump-stop-live", "role": "assistant", "kind": "live", "text": streamed(0)}]));
            true
        })),
        ("stop: the reply streams", Box::new(|_, _, elapsed| {
            // Two more parts: still shorter than the view, so followed.
            if elapsed < Duration::from_millis(1200) {
                let part = if elapsed < Duration::from_millis(400) { 1 } else { 2 };
                if !holds("jump-stop-live", &streamed(part)) {
                    upsert("jump", json!([{"id": "jump-stop-live", "role": "assistant", "kind": "live", "text": streamed(part)}]));
                }
                return false;
            }
            check(report().at_end && report().follows, &format!("a short reply is followed to its end: {:?}", seen().map(|s| s.line())));
            true
        })),
        ("stop: Ctrl+.", Box::new(|_, window, _| {
            check(crate::commands::run(&app_now(), window, "stop-agent"), "Stop runs, as Ctrl+. does");
            // The stopped turn settles: its partial answer, by now taller
            // than the view, filed under its own id in the live one's
            // place and last in the chat (a new reply to the transcript,
            // which armed the follow-stop on it), the stop said under it.
            upsert("jump", json!([{"id": "jump-stop-reply", "role": "assistant", "text": format!("{}\n\nStopped by its user.", streamed(10))}]));
            agent_state("jump", "idle");
            true
        })),
        ("stop: settled", Box::new(move |_, _, elapsed| {
            if !has_row("jump-stop-reply") || elapsed < Duration::from_millis(2500) {
                check(elapsed < Duration::from_secs(8), "the stopped turn settles in the chat");
                return elapsed >= Duration::from_secs(8);
            }
            let now = seen().unwrap_or_default();
            check(report().at_end && report().follows, &format!("after Stop the reader stays at the end, the stop line in view, not pulled up to the answer's first line: {} (at end {})", now.line(), report().at_end));
            shot(&out, "scroll-jump-02-stopped-at-end");
            true
        })),
    ]
}

// ---- the chat flashing another scroll state (`--check scroll-paint`)

/// What the frames drawn while the chat streamed showed.
#[derive(Default)]
struct Painted {
    /// Frames drawn while the chat followed, and those of them that did not
    /// show its end.
    following: usize,
    off_end: Vec<String>,
    done: usize,
    begun: Option<Instant>,
}

/// `--check scroll-paint --out DIR`: a chat following its end shows its
/// end in every frame drawn, pixel for pixel. Frames are drawn as the real
/// window draws them (only what changed is repainted, at 60 Hz), and every
/// other one is compared with a whole repaint of the same state: a region
/// the partial repaint missed keeps an older frame's rows on screen, the
/// flash of another scroll state. Agent turns stream into a 200-row chat
/// (tools come and go, rows are filed, a live reply grows and is replaced,
/// rows land in bursts, the working footer comes and goes), the composer
/// unfolds and folds under it, and the reader switches to another chat and
/// back.
pub(super) fn scroll_paint_check(out: String) {
    const SESSION: &str = "paint";
    const ROWS: usize = 200;
    const STEP_MS: u64 = 120;
    let mut steps: Vec<(String, Box<dyn Fn()>)> = Vec::new();
    for turn in 0..3 {
        steps.extend(agent_turn(SESSION, turn));
        steps.push((format!("turn {turn}: three rows land at once"), Box::new(move || {
            upsert(SESSION, json!((0..3).map(|n| json!({"id": format!("paint-burst-{turn}-{n}"), "role": if n == 0 { "user" } else { "assistant" }, "text": LOREM.repeat(1 + n)})).collect::<Vec<_>>()));
        })));
        for n in 0..6 {
            steps.push((format!("turn {turn}: a fast reply grows ({n})"), Box::new(move || {
                upsert(SESSION, json!([{"id": format!("paint-fast-{turn}"), "role": "assistant", "kind": "live", "text": streamed(n)}]));
            })));
        }
        steps.push((format!("turn {turn}: the fast reply is filed"), Box::new(move || {
            upsert(SESSION, json!([{"id": format!("paint-fast-done-{turn}"), "role": "assistant", "text": streamed(6)}]));
        })));
        // The composer unfolds under the chat and folds again: the view
        // shrinks and grows while it follows.
        steps.push((format!("turn {turn}: the composer takes the keyboard"), Box::new(|| app_now().focus_composer())));
        steps.push((format!("turn {turn}: the chat takes it back"), Box::new(|| app_now().focus_transcript())));
        steps.push((format!("turn {turn}: another chat"), Box::new(|| {
            app_now().engine.borrow_mut().select("rachel");
            crate::pump();
        })));
        steps.push((format!("turn {turn}: back to the streaming chat"), Box::new(|| {
            app_now().engine.borrow_mut().select(SESSION);
            crate::pump();
        })));
    }
    let steps = Rc::new(steps);
    let count = steps.len();
    let state = Rc::new(RefCell::new(Painted::default()));
    let (s1, s2) = (state.clone(), state.clone());
    let saved = out.clone();
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/add-agent", &json!({"session": SESSION, "persona": "Paint"})).is_ok(), "the Host takes a new chat");
            true
        })),
        ("select", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find(SESSION).is_none() {
                return false;
            }
            app.engine.borrow_mut().select(SESSION);
            crate::pump();
            true
        })),
        ("fill", Box::new(|app, _, elapsed| {
            if app.engine.borrow().conversation(SESSION).is_none() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(control("/__control/rich", &json!({"session": SESSION, "count": ROWS})).is_ok(), &format!("the Host fills the chat with {ROWS} varied rows"));
            true
        })),
        ("at the end", Box::new(move |app, window, elapsed| {
            if rows(window).len() < ROWS || elapsed < Duration::from_millis(1200) {
                return false;
            }
            if !(report().follows && report().at_end && last_row_visible().0) {
                app.to_latest();
                return false;
            }
            headless::pace(headless::Pacing { partial: true, hz: 60 });
            headless::verify_paint(true, Some(saved.clone()));
            let watch = s1.clone();
            headless::after_draw(Some(Box::new(move || {
                let mut st = watch.borrow_mut();
                let app = app_now();
                // Only the streaming chat, and only once it is drawn again.
                if app.engine.borrow().selected_session() != SESSION || app.active_messages().is_none_or(|m| slint::Model::row_count(&*m) < ROWS) {
                    return;
                }
                let now = report();
                if !now.follows {
                    return;
                }
                st.following += 1;
                // Both what the transcript says and where its last row is
                // drawn: its bottom edge inside the view, and on the
                // view's bottom (not above it with blank below: past the end).
                let (visible, numbers) = last_row_visible();
                let geo = geometry();
                let past = geo.find(&last_id()).is_some_and(|(top, bottom)| top > geo.top + 1.0 && geo.bottom - bottom > 12.0 && geo.rows.first().is_some_and(|r| r.1 < geo.top - 1.0));
                if !now.at_end || !visible || past {
                    let at = st.begun.map_or(0, |b| b.elapsed().as_millis());
                    st.off_end.push(format!("frame {} at {at}ms: at-end {}, offset {:.0}, top row #{}: {numbers}", headless::frames_drawn(), now.at_end, now.offset, now.top));
                }
            })));
            s1.borrow_mut().begun = Some(Instant::now());
            true
        })),
        ("streams", Box::new(move |_, _, elapsed| {
            let due = (elapsed.as_millis() as u64 / STEP_MS) as usize;
            loop {
                let next = {
                    let st = s2.borrow();
                    (st.done < count && st.done <= due).then_some(st.done)
                };
                let Some(next) = next else { break };
                (steps[next].1)();
                s2.borrow_mut().done += 1;
            }
            if s2.borrow().done < count || elapsed < Duration::from_millis(STEP_MS * count as u64 + 1500) {
                return false;
            }
            headless::after_draw(None);
            let paint = headless::paint();
            headless::verify_paint(false, None);
            headless::pace(headless::Pacing::default());
            let st = s2.borrow();
            check(paint.compared >= 5, &format!("the paint check compared partial frames with whole repaints while {count} updates streamed in ({} compared)", paint.compared));
            check(paint.stale.is_empty(), &format!("every partial frame shows what a whole repaint shows: {} of {} differed (first: {:?}; saved as stale-*.png)", paint.stale.len(), paint.compared, paint.stale.iter().take(4).collect::<Vec<_>>()));
            check(st.following >= 10, &format!("frames were drawn while the chat followed ({} frames)", st.following));
            check(st.off_end.is_empty(), &format!("no frame of the following chat is drawn off its end: {} of {} were: {:?}", st.off_end.len(), st.following, st.off_end.iter().take(6).collect::<Vec<_>>()));
            let (visible, numbers) = last_row_visible();
            check(report().follows && visible, &format!("the chat still follows and shows its last row whole: {numbers}"));
            shot(&out, "scroll-paint-01-streamed");
            true
        })),
    ];
    run_stages(stages);
}

/// What the dashcam saw while a check streamed into a following chat: its
/// frames (by when they were drawn) and the flashes found before it began.
#[derive(Default)]
struct Dash {
    frames: Vec<crate::scroll_journal::Frame>,
    flashes: usize,
    posted: usize,
    pane: String,
    done: usize,
}

impl Dash {
    /// Takes the frames drawn since the last look.
    fn look(&mut self) {
        let last = self.frames.last().map(|f| f.at);
        let new: Vec<_> = crate::scroll_journal::frames(&self.pane).into_iter().filter(|f| last.is_none_or(|at| f.at > at)).collect();
        self.frames.extend(new);
    }
}

/// `--check scroll-flash --out DIR`: a chat following its end never draws
/// a frame anywhere else. Agent turns stream into a 200-row chat (tools
/// come and go, rows are filed, a live reply grows and is replaced by the
/// durable one, new rows land in bursts) while the dashcam keeps every
/// frame drawn: none may leave the end, and its flash detector finds none.
pub(super) fn scroll_flash_check(out: String) {
    const SESSION: &str = "flash";
    const TALL: &str = "flash-tall";
    const SHORT: &str = "flash-short";
    const ROWS: usize = 200;
    let state = Rc::new(RefCell::new(Dash::default()));
    let (s1, s2, s3) = (state.clone(), state.clone(), state.clone());
    let mut steps: Vec<(String, Box<dyn Fn()>)> = Vec::new();
    for turn in 0..3 {
        steps.extend(agent_turn(SESSION, turn));
        // A burst: three new rows at once, then a reply that grows fast.
        steps.push((format!("turn {turn}: three rows land at once"), Box::new(move || {
            upsert(SESSION, json!((0..3).map(|n| json!({"id": format!("flash-burst-{turn}-{n}"), "role": if n == 0 { "user" } else { "assistant" }, "text": LOREM.repeat(1 + n)})).collect::<Vec<_>>()));
        })));
        for n in 0..6 {
            steps.push((format!("turn {turn}: a fast reply grows ({n})"), Box::new(move || {
                upsert(SESSION, json!([{"id": format!("flash-fast-{turn}"), "role": "assistant", "kind": "live", "text": streamed(n)}]));
            })));
        }
        steps.push((format!("turn {turn}: the fast reply is filed"), Box::new(move || {
            upsert(SESSION, json!([{"id": format!("flash-fast-done-{turn}"), "role": "assistant", "text": streamed(6)}]));
        })));
    }
    let steps = Rc::new(steps);
    let count = steps.len();
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            // The dashcam is off by default and records nothing then;
            // Settings → Diagnostics turns it on.
            check(!crate::scroll_journal::on(), "the scroll dashcam is off by default");
            check(crate::scroll_journal::frames(&app.active_id()).is_empty(), "and keeps no frames while off");
            app.engine.borrow_mut().settings_mut().set(crate::scroll_journal::SETTING, true);
            check(crate::scroll_journal::on(), "Settings → Diagnostics turns it on");
            check(control("/__control/add-agent", &json!({"session": SESSION, "persona": "Flash"})).is_ok(), "the Host takes a new chat");
            // Two more to switch to: one of a few very tall rows, one of
            // many short ones (the list's estimate of a row's height differs
            // most between them).
            check(control("/__control/add-agent", &json!({"session": TALL, "persona": "Tall"})).is_ok(), "the Host takes a chat of tall rows");
            check(control("/__control/add-agent", &json!({"session": SHORT, "persona": "Short"})).is_ok(), "the Host takes a chat of short rows");
            check(control("/__control/rich", &json!({"session": SHORT, "count": 300})).is_ok(), "the Host fills the short chat");
            upsert(TALL, json!((0..24).map(|n| json!({"id": format!("tall-{n}"), "role": if n % 4 == 0 { "user" } else { "assistant" },
                "text": if n % 4 == 0 { format!("Short question {n}?") } else { format!("Long answer {n}. {}", LOREM.repeat(20 + 7 * (n % 5))) }})).collect::<Vec<_>>()));
            true
        })),
        ("select", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find(SESSION).is_none() {
                return false;
            }
            app.engine.borrow_mut().select(SESSION);
            crate::pump();
            true
        })),
        ("fill", Box::new(|app, _, elapsed| {
            if app.engine.borrow().conversation(SESSION).is_none() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(control("/__control/rich", &json!({"session": SESSION, "count": ROWS})).is_ok(), &format!("the Host fills the chat with {ROWS} varied rows"));
            true
        })),
        ("at the end", Box::new(move |app, window, elapsed| {
            if rows(window).len() < ROWS || elapsed < Duration::from_millis(1200) {
                return false;
            }
            if !(report().follows && report().at_end && last_row_visible().0) {
                app.to_latest();
                return false;
            }
            let mut st = s1.borrow_mut();
            st.pane = app.active_id().to_string();
            st.flashes = crate::scroll_journal::flashes_seen();
            st.posted = crate::scroll_journal::flashes().len();
            st.frames.clear();
            true
        })),
        ("streams", Box::new(move |_, _, elapsed| {
            let mut st = s2.borrow_mut();
            // A step every 150 ms, then a second and a half to settle.
            let due = (elapsed.as_millis() / 150) as usize;
            while st.done < count && st.done <= due {
                (steps[st.done].1)();
                st.done += 1;
            }
            st.look();
            if st.done < count || elapsed < Duration::from_millis(150 * count as u64 + 1500) {
                return false;
            }
            let following: Vec<&crate::scroll_journal::Frame> = st.frames.iter().filter(|f| f.shot.follow).collect();
            let off: Vec<&&crate::scroll_journal::Frame> = following
                .iter()
                .filter(|f| {
                    let s = &f.shot;
                    (s.place() - 1.0).abs() * (s.content - s.viewport).max(0.0) > 2.0
                })
                .collect();
            let worst = off.iter().map(|f| (f.shot.place() - 1.0).abs() * (f.shot.content - f.shot.viewport).max(0.0)).fold(0.0, f32::max);
            let sample: Vec<String> = off
                .iter()
                .take(6)
                .map(|f| format!("{:.0}ms place {:.3} offset {:.0} content {:.0} [{}]", f.at.as_secs_f64() * 1000.0, f.shot.place(), f.shot.offset, f.shot.content, f.events.join(", ")))
                .collect();
            check(following.len() >= count / 2, &format!("the dashcam kept the frames drawn while {count} updates streamed in ({} frames, {} following)", st.frames.len(), following.len()));
            check(off.is_empty(), &format!("no frame of the following chat is drawn off its end: {} of {} were, up to {worst:.0}px: {sample:?}", off.len(), following.len()));
            let flashes = crate::scroll_journal::flashes_seen() - st.flashes;
            check(flashes == 0, &format!("the dashcam finds no flash ({flashes} found; posted {:?})", &crate::scroll_journal::flashes()[st.posted..]));
            let (visible, numbers) = last_row_visible();
            check(report().follows && visible, &format!("the chat still follows and shows its last row whole: {numbers}"));
            shot(&out, "scroll-flash-01-streamed");
            *st = Dash { pane: st.pane.clone(), flashes: crate::scroll_journal::flashes_seen(), posted: crate::scroll_journal::flashes().len(), ..Dash::default() };
            true
        })),
        // The pane opens other chats and comes back, following: each opens
        // at its end, and no frame between shows any other place.
        ("switches", Box::new(move |app, _, elapsed| {
            const CHATS: [&str; 6] = [TALL, SESSION, SHORT, TALL, SHORT, SESSION];
            let mut st = s3.borrow_mut();
            let due = (elapsed.as_millis() / 1500) as usize;
            while st.done < CHATS.len() && st.done <= due {
                app.engine.borrow_mut().select(CHATS[st.done]);
                crate::pump();
                st.done += 1;
            }
            st.look();
            if st.done < CHATS.len() || elapsed < Duration::from_millis(1500 * CHATS.len() as u64 + 500) {
                return false;
            }
            let following: Vec<&crate::scroll_journal::Frame> = st.frames.iter().filter(|f| f.shot.follow).collect();
            let off: Vec<String> = following
                .iter()
                .filter(|f| (f.shot.place() - 1.0).abs() * (f.shot.content - f.shot.viewport).max(0.0) > 2.0)
                .take(8)
                .map(|f| format!("{:.0}ms place {:.3} offset {:.0} content {:.0} rows {} [{}]", f.at.as_secs_f64() * 1000.0, f.shot.place(), f.shot.offset, f.shot.content, f.shot.rows, f.events.join(", ")))
                .collect();
            check(off.is_empty(), &format!("opening chats while following, no frame is drawn off a chat's end ({} frames following): {off:?}", following.len()));
            let flashes = crate::scroll_journal::flashes_seen() - st.flashes;
            check(flashes == 0, &format!("opening chats, the dashcam finds no flash ({flashes} found; posted {:?})", &crate::scroll_journal::flashes()[st.posted..]));
            let (visible, numbers) = last_row_visible();
            check(report().follows && visible, &format!("the chat opened last follows and shows its last row whole: {numbers}"));
            true
        })),
    ];
    run_stages(stages);
}

/// The unasked-movement rules' detections so far: shift, shake, layout_shift.
fn moved_now() -> [usize; 3] {
    ["shift", "shake", "layout_shift"].map(crate::scroll_journal::moved_seen)
}

/// What `scroll-shake` counted before each part.
#[derive(Default)]
struct Shake {
    pane: String,
    moved: [usize; 3],
    flashes: usize,
    posted: usize,
    done: usize,
    frames: Vec<crate::scroll_journal::Frame>,
    /// The newest frame looked at (kept when the frames are let go).
    seen: Option<Duration>,
}

impl Shake {
    /// Takes the frames drawn since the last look.
    fn look(&mut self) {
        let last = self.seen;
        let new: Vec<_> = crate::scroll_journal::frames(&self.pane).into_iter().filter(|f| last.is_none_or(|at| f.at > at)).collect();
        if let Some(newest) = new.last() {
            self.seen = Some(newest.at);
        }
        self.frames.extend(new);
    }

    /// Prints each step of 2 px or more the frames looked at show (the
    /// two frames, what happened before the second), then forgets them.
    fn steps(&mut self, part: &str) {
        let mut shown = 0;
        for pair in self.frames.windows(2) {
            let Some(step) = crate::unasked::movement(&pair[0], &pair[1]) else { continue };
            if step.dy.abs() < crate::unasked::STEP || shown >= 40 {
                continue;
            }
            shown += 1;
            let line = |f: &crate::scroll_journal::Frame| {
                let s = &f.shot;
                format!("{:.0}ms offset {:.1} content {:.1} view {:.1} follow {} rows {} top {}#{} y {:.1} h {:.1} bottom {} y {:.1} h {:.1}",
                        f.at.as_secs_f64() * 1000.0, s.offset, s.content, s.viewport, s.follow, s.rows, s.top, s.top_index, s.top_y, s.top_h, s.bottom, s.bottom_y, s.bottom_h)
            };
            println!("note {part} step {:.1} share {:.2} counted {}: {} -> {} [{}]", step.dy, step.fraction, step.counted, line(&pair[0]), line(&pair[1]), pair[1].events.join(", "));
        }
        println!("note {part}: {} frames, {shown} steps of 2 px or more", self.frames.len());
        self.frames.clear();
    }
}

/// `--check scroll-shake --out DIR`: unasked movement in a chat following
/// its end (unasked.rs). The last row's height settles up and down
/// (16 updates 180 ms apart) while the chat is pinned to its end: the dashcam
/// finds a shake and posts one `scroll_flash` event with rule `shake`, its
/// reversals and largest step. Then a settled, following chat that only
/// gets rows at its end and a reply that grows finds none of the new
/// rules. Last, whole agent turns stream in (tools come and go, the live
/// reply is replaced by the durable one): what they measure is printed.
pub(super) fn scroll_shake_check(out: String) {
    const SESSION: &str = "shake";
    const ROWS: usize = 60;
    const FLIPS: usize = 16;
    // Apart enough that two updates seldom land in one frame (and cancel),
    // near enough that three steps fall within the shake's 500 ms.
    const FLIP_MS: u64 = 180;
    let state = Rc::new(RefCell::new(Shake::default()));
    let (s1, s2, s3, s4, s5, s6) = (state.clone(), state.clone(), state.clone(), state.clone(), state.clone(), state.clone());
    let settle = |n: usize| -> serde_json::Value { json!([{"id": "shake-settling", "role": "assistant", "text": LOREM.repeat(if n % 2 == 0 { 1 } else { 6 })}]) };
    let quiet: Rc<Vec<(String, Box<dyn Fn()>)>> = Rc::new(
        (0..12)
            .map(|n| -> (String, Box<dyn Fn()>) {
                if n % 3 == 0 {
                    (format!("a row lands at the end ({n})"), Box::new(move || {
                        upsert(SESSION, json!([{"id": format!("shake-quiet-{n}"), "role": if n % 2 == 0 { "user" } else { "assistant" }, "text": LOREM.repeat(1 + n % 3)}]));
                    }))
                } else {
                    (format!("a reply grows ({n})"), Box::new(move || {
                        upsert(SESSION, json!([{"id": format!("shake-growing-{}", n / 3), "role": "assistant", "text": streamed(n % 3 + 1)}]));
                    }))
                }
            })
            .collect(),
    );
    let turns: Rc<Vec<(String, Box<dyn Fn()>)>> = Rc::new((0..2).flat_map(|turn| agent_turn(SESSION, turn)).collect());
    let (quiet_count, turn_count) = (quiet.len(), turns.len());
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            app.engine.borrow_mut().settings_mut().set(crate::scroll_journal::SETTING, true);
            check(crate::scroll_journal::on(), "the scroll dashcam is on (Settings → Diagnostics)");
            check(control("/__control/add-agent", &json!({"session": SESSION, "persona": "Shake"})).is_ok(), "the Host takes a new chat");
            true
        })),
        ("select", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find(SESSION).is_none() {
                return false;
            }
            app.engine.borrow_mut().select(SESSION);
            crate::pump();
            true
        })),
        ("fill", Box::new(|app, _, elapsed| {
            if app.engine.borrow().conversation(SESSION).is_none() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(control("/__control/rich", &json!({"session": SESSION, "count": ROWS})).is_ok(), &format!("the Host fills the chat with {ROWS} varied rows"));
            true
        })),
        ("at the end", Box::new(move |app, window, elapsed| {
            if rows(window).len() < ROWS || elapsed < Duration::from_millis(1500) {
                return false;
            }
            if !(report().follows && report().at_end && last_row_visible().0) {
                app.to_latest();
                return false;
            }
            let mut st = s1.borrow_mut();
            st.pane = app.active_id().to_string();
            st.moved = moved_now();
            st.posted = crate::scroll_journal::flashes().len();
            st.frames.clear();
            true
        })),
        // The last row grows and shrinks every 180 ms, nobody scrolling.
        ("heights settle up and down", Box::new(move |_, _, elapsed| {
            let mut st = s2.borrow_mut();
            st.look();
            let due = (elapsed.as_millis() / u128::from(FLIP_MS)) as usize;
            while st.done < FLIPS && st.done <= due {
                upsert(SESSION, settle(st.done));
                st.done += 1;
            }
            // Then a second: the shake is posted half a second after it is found.
            st.done >= FLIPS && elapsed >= Duration::from_millis(FLIP_MS * FLIPS as u64 + 1000)
        })),
        ("a shake is found and posted", Box::new(move |_, _, _| {
            let mut st = s3.borrow_mut();
            st.look();
            st.steps("scroll-shake fixture");
            let now = moved_now();
            let found: Vec<usize> = (0..3).map(|i| now[i] - st.moved[i]).collect();
            println!("note scroll-shake fixture: found shift {} shake {} layout_shift {}", found[0], found[1], found[2]);
            check(found[1] >= 1, &format!("the dashcam finds a shake while the last row settles up and down ({} found)", found[1]));
            check(found[0] >= 1, &format!("and shifts: rows moving down while the end is pinned ({} found)", found[0]));
            let posted = crate::scroll_journal::flashes()[st.posted..].to_vec();
            let shakes: Vec<&serde_json::Value> = posted.iter().filter(|d| d["rule"] == "shake").collect();
            check(shakes.len() == 1, &format!("one scroll_flash event with rule shake is posted, not one per step ({} posted; rules {:?})", shakes.len(), posted.iter().map(|d| d["rule"].clone()).collect::<Vec<_>>()));
            if let Some(shake) = shakes.first() {
                println!("note scroll-shake fixture event: reversals {} max_step {} score {} frames {}", shake["reversals"], shake["max_step"], shake["score"], shake["frames"].as_array().map_or(0, Vec::len));
                check(shake["reversals"].as_u64().is_some_and(|r| r >= 2), &format!("with its reversals ({})", shake["reversals"]));
                check(shake["max_step"].as_f64().is_some_and(|s| s >= 2.0), &format!("and its largest step ({})", shake["max_step"]));
                check(shake["chat"] == SESSION && shake["window"] == st.pane.as_str(), &format!("for this chat and window ({} {})", shake["chat"], shake["window"]));
                check(shake["screen"].as_i64().is_some_and(|s| s > 0) && shake["build"].as_str().is_some_and(|b| !b.is_empty()), "with the screen's height and the build");
                check(shake["frames"].as_array().is_some_and(|f| !f.is_empty()), "and the frames around it");
            }
            st.done = 0;
            true
        })),
        // Rows at the end and replies that only grow: the follow slide, no more.
        ("a settled chat streams at its end", Box::new(move |_, _, elapsed| {
            let mut st = s4.borrow_mut();
            st.look();
            if st.done == 0 && elapsed < Duration::from_millis(800) {
                return false;
            }
            if st.done == 0 {
                st.moved = moved_now();
                st.flashes = crate::scroll_journal::flashes_seen();
                st.frames.clear();
                st.look();
            }
            let due = ((elapsed.as_millis().saturating_sub(800)) / 150) as usize;
            while st.done < quiet_count && st.done <= due {
                (quiet[st.done].1)();
                st.done += 1;
            }
            st.done >= quiet_count && elapsed >= Duration::from_millis(800 + 150 * quiet_count as u64 + 1500)
        })),
        ("none of the new rules", Box::new(move |_, _, _| {
            let mut st = s5.borrow_mut();
            st.look();
            st.steps("scroll-shake settled streaming");
            let now = moved_now();
            let found: Vec<usize> = (0..3).map(|i| now[i] - st.moved[i]).collect();
            println!("note scroll-shake settled streaming: found shift {} shake {} layout_shift {}", found[0], found[1], found[2]);
            check(found == [0, 0, 0], &format!("a settled, following chat finds no shift, shake or layout shift ({found:?})"));
            check(crate::scroll_journal::flashes_seen() == st.flashes, "nor a flash");
            let (visible, numbers) = last_row_visible();
            check(report().follows && visible, &format!("it still follows and shows its last row whole: {numbers}"));
            shot(&out, "scroll-shake-01-settled");
            st.moved = now;
            st.posted = crate::scroll_journal::flashes().len();
            st.done = 0;
            true
        })),
        // Whole agent turns, measured (not judged): what the app does today.
        ("agent turns stream", Box::new(move |_, _, elapsed| {
            let mut st = s6.borrow_mut();
            st.look();
            let due = (elapsed.as_millis() / 200) as usize;
            while st.done < turn_count && st.done <= due {
                (turns[st.done].1)();
                st.done += 1;
            }
            if st.done < turn_count || elapsed < Duration::from_millis(200 * turn_count as u64 + 1500) {
                return false;
            }
            st.steps("scroll-shake agent turns");
            let now = moved_now();
            let found: Vec<usize> = (0..3).map(|i| now[i] - st.moved[i]).collect();
            let posted: Vec<String> = crate::scroll_journal::flashes()[st.posted..]
                .iter()
                .map(|d| format!("{} step {} reversals {} max_step {} score {}", d["rule"], d["step"], d["reversals"], d["max_step"], d["score"]))
                .collect();
            println!("note scroll-shake agent turns ({turn_count} updates): found shift {} shake {} layout_shift {}; posted {posted:?}", found[0], found[1], found[2]);
            true
        })),
    ];
    run_stages(stages);
}
