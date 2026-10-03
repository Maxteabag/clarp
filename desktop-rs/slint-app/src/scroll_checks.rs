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
        // 8. Following: new rows and a growing streaming reply stay in view.
        ("follows a stream", Box::new({
            let state = s();
            move |_, _, _| {
                let mut st = state.borrow_mut();
                let step = st.step;
                // Each update: wait for it to land, then measure.
                if step > 0 {
                    let landed = if step <= streams { holds("stream-2", &streamed(step - 1)) } else { has_row(&st.target) };
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
                    upsert("scroll", json!([{"id": "stream-2", "role": "assistant", "text": streamed(next - 1)}]));
                } else if next == streams + 1 {
                    upsert("scroll", json!([{"id": "scroll-user-2", "role": "user", "text": "Thanks, and the docs?"},
                                            {"id": "scroll-reply-2", "role": "assistant", "text": LOREM.repeat(4)}]));
                    st.target = "scroll-reply-2".into();
                } else {
                    st.step = 0;
                    return true;
                }
                false
            }
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
    ];
    run_stages(stages);
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
            if following {
                check(after.at_end && after.follows, &format!("{label}: a reader at the end stays at the end: before {}; after {} ({})", before.line(), after.line(), after.drawn));
            } else {
                check(after.last >= before.first, &format!("{label}: a reader one notch up stays within a viewport: before {}; after {} ({})", before.line(), after.line(), after.drawn));
            }
            check(st.above == 0, &format!("{label}: never more than a viewport up meanwhile ({} polls wholly above; highest place rows #{}..#{}, before #{}..#{})", st.above, lowest.first, lowest.last, before.first, before.last));
            let journal: Vec<String> = crate::scroll_journal::logged().split_off(st.journal);
            if following {
                check(!journal.iter().any(|c| c == "follow"), &format!("{label}: following, the scroll journal logs no pins (logged {journal:?})"));
            }
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
    run_stages(stages);
}
