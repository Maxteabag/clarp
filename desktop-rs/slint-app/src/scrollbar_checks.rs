//! The chat's scrollbar (`--check scrollbar`), measured on screen: a long,
//! varied chat (long Markdown, code, tables, quotes, folded turns, other
//! agents' prompts, decision receipts, tool calls, user bubbles) is read top
//! to bottom and back by wheel and by Page keys, and the scrollbar's thumb
//! is read at every step. After the first full pass the thumb keeps its
//! size, and it moves the way the reader scrolls: never back up while they
//! go down, never down while they go up; at the top it is at the top of its
//! track and at the end at the bottom.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use super::{Stage, check, control, report, rows, run_stages, shot};
use crate::headless;

const SESSION: &str = "steady";
/// Turns of the chat; each is several rows.
const TURNS: usize = 170;

/// The thumb and the track it travels (window coordinates).
#[derive(Debug, Clone, Copy, Default)]
struct Thumb {
    top: f32,
    height: f32,
    track_top: f32,
    track_bottom: f32,
}

impl Thumb {
    /// Where the thumb is along its travel, 0 the top and 1 the end.
    fn place(&self) -> f32 {
        let travel = self.track_bottom - self.track_top - self.height;
        if travel <= 0.0 { 1.0 } else { (self.top - self.track_top) / travel }
    }
}

/// The transcript's viewport (left, top, width, height).
fn viewport() -> Option<(f32, f32, f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let element = ElementQuery::from_root(&window).match_type_name("Transcript").find_first()?;
    let (p, s) = (element.absolute_position(), element.size());
    Some((p.x, p.y, s.width, s.height))
}

/// The chat's scrollbar thumb as drawn: the transcript's own when it has
/// one, else the standard ScrollView's (its track is the bar less the
/// 16 px at each end that it keeps for its arrows).
fn thumb() -> Option<Thumb> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let (left, top, width, height) = viewport()?;
    let inside = |p: slint::LogicalPosition, s: slint::LogicalSize| {
        s.height > s.width && p.x >= left + width - 30.0 && p.x <= left + width + 1.0 && p.y >= top - 1.0 && p.y + s.height <= top + height + 1.0
    };
    let by_id = |id: &'static str| {
        ElementQuery::from_root(&window)
            .match_predicate(move |e| e.id().is_some_and(|i| i == id))
            .find_all()
            .into_iter()
            .map(|e| (e.absolute_position(), e.size()))
            .find(|(p, s)| inside(*p, *s))
    };
    if let (Some((tp, ts)), Some((kp, ks))) = (by_id("Transcript::thumb"), by_id("Transcript::track")) {
        return Some(Thumb { top: tp.y, height: ts.height, track_top: kp.y, track_bottom: kp.y + ks.height });
    }
    let (tp, ts) = by_id("ScrollBar::thumb")?;
    let bar = ElementQuery::from_root(&window)
        .match_type_name("ScrollBar")
        .find_all()
        .into_iter()
        .map(|e| (e.absolute_position(), e.size()))
        .find(|(p, s)| inside(*p, *s))?;
    Some(Thumb { top: tp.y, height: ts.height, track_top: bar.0.y + 16.0, track_bottom: bar.0.y + bar.1.height - 16.0 })
}

fn wheel(delta: f32) {
    if let Some((left, top, width, height)) = viewport() {
        headless::wheel(left + width / 2.0, top + height / 2.0, delta);
    }
}

fn resolved_prompt(i: usize, question: &str, outcome: &str) -> String {
    format!("[Clarp decision resolved]\nDecision ID: d-steady-{i}\nArtifact ID: steady-{i}\nQuestion: {question}\nContext: Two reviews are in.\nReference: \nPayload: {{\"ticket\": \"OPS-{i}\"}}\n{outcome}")
}

const LOREM: &str = "The build runs the unit tests first, then the integration suite against a scratch database, and only then packages the desktop app. ";

/// Turn `i` of a long chat whose rows differ a lot in kind and height.
fn turn(i: usize, turns: &mut Vec<Value>) {
    let trace = format!("t-{i}");
    let at = format!("2026-10-05T{:02}:{:02}:00Z", 6 + i / 60, i % 60);
    let id = |name: &str| format!("{SESSION}-{i}-{name}");
    let size = 1 + (i * 7) % 5;
    let user = |text: String| json!({"id": id("u"), "role": "user", "timestamp": at, "text": text, "origin": "user", "trace_id": trace});
    let reply = |text: String| json!({"id": id("a"), "role": "assistant", "timestamp": at, "text": text, "origin": "agent", "trace_id": trace, "phase": "final"});
    match i % 8 {
        0 => {
            turns.push(user(format!("Question {i}: what changed in the build?")));
            let items: String = (0..2 + size).map(|n| format!("- **Part {n}:** {}\n", &LOREM[..40 + n * 12])).collect();
            turns.push(reply(format!("## Summary {i}\n\n{}\n\n{items}\n### Next\n\n{}", LOREM.repeat(size), LOREM.repeat(1 + size / 2))));
        }
        1 => {
            turns.push(user(format!("Show me the code for {i}.")));
            let code: Vec<String> = (0..4 + size * 4).map(|n| format!("    let value_{n} = compute({n}, \"step\");")).collect();
            turns.push(reply(format!("Code {i}:\n\n```rust\nfn main() {{\n{}\n}}\n```\n\nThat is all.", code.join("\n"))));
        }
        2 => {
            turns.push(user(format!("Long request {i}. {}", LOREM.repeat(1 + size))));
            let table: String = (0..2 + size * 2).map(|n| format!("| step {n} | {} | {}s |\n", if n % 2 == 1 { "done" } else { "running" }, n * 3)).collect();
            turns.push(reply(format!("Table {i}:\n\n| step | state | time |\n|---|---|---|\n{table}")));
        }
        3 => {
            turns.push(user(format!("Quote it for {i}.")));
            turns.push(reply(format!("> {}\n\n> Keep the main checkout untouched.\n\nAnd {}", LOREM.repeat(size), LOREM)));
        }
        4 => {
            // A turn that works: tool calls and a note fold behind "Worked for".
            turns.push(user(format!("Run the tests for {i}.")));
            for n in 0..1 + size % 3 {
                turns.push(json!({"id": id(&format!("tool{n}")), "role": "assistant", "timestamp": at, "text": "", "origin": "agent", "trace_id": trace,
                    "tools": [{"name": "Bash", "summary": format!("cargo test -p part{n}"), "status": "ok", "id": format!("call-{i}-{n}"), "command": format!("cargo test -p part{n}"),
                               "result": (0..4 + n).map(|m| format!("test case_{m} ... ok")).collect::<Vec<_>>().join("\n")}]}));
            }
            turns.push(json!({"id": id("note"), "role": "assistant", "timestamp": at, "text": "Tests pass; writing it up.", "origin": "agent", "trace_id": trace, "phase": "commentary"}));
            let mut done = reply(format!("Ran the tests for {i}: all green. {}", LOREM.repeat(size / 2)));
            done["turn"] = json!({"turn_id": trace, "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 9_000, "worked_ms": 8_000, "tool_count": 1 + size % 3});
            turns.push(done);
        }
        5 => {
            // Another agent's prompt, folded to one line.
            turns.push(json!({"id": id("u"), "role": "user", "timestamp": at, "origin": "agent", "sender_name": "Dagger", "sender_agent_id": "dagger-id", "trace_id": trace,
                "text": format!("Go ahead with step {i}: it matches the review.\n\n{}", LOREM.repeat(size))}));
            turns.push(reply(format!("Done {i}.")));
        }
        6 => {
            // A decision the user resolved: a receipt in their column.
            let outcome = if i % 16 == 6 { "The user chose: accepted. Approval applies only to the described action." } else { "The user chose: rejected. Do not perform the protected action." };
            turns.push(json!({"id": id("u"), "role": "user", "timestamp": at, "origin": "automation", "trace_id": trace,
                "text": resolved_prompt(i, &format!("Deploy build {i} to staging? {}", &LOREM[..20 + size * 15]), outcome)}));
            turns.push(reply(format!("Noted {i}.")));
        }
        _ => {
            turns.push(user(format!("Follow-up {i}: {}", &LOREM[..30 + size * 20])));
            turns.push(reply(format!("### Mixed {i}\n\n{}\n\n> A short quote.\n\n```sh\ncargo build\ncargo test\n```\n\n1. one\n2. two\n3. {}", LOREM.repeat(size), LOREM)));
        }
    }
}

fn chat() -> Value {
    let mut turns = Vec::new();
    for i in 0..TURNS {
        turn(i, &mut turns);
    }
    json!({"session": SESSION, "turns": turns})
}

/// One sample: the thumb, the list's content height and offset.
#[derive(Debug, Clone, Copy)]
struct Sample {
    thumb: Thumb,
    offset: f32,
}

#[derive(Default)]
struct Pass {
    samples: Vec<Sample>,
    last_offset: Option<f32>,
    still: usize,
    acts: usize,
    done: bool,
    since: Option<Instant>,
}

/// A pass through the chat: act (a wheel notch, a key), wait until the
/// offset holds for two polls, sample the thumb, and again, until the
/// offset no longer moves. `down`: the direction the reader goes.
fn pass(state: Rc<RefCell<Pass>>, label: &'static str, down: bool, by_key: bool, first: bool) -> Vec<Stage> {
    let mut stages: Vec<Stage> = Vec::new();
    // A stage times out after 15 s: the pass spans several.
    for part in 0..10 {
        let state = state.clone();
        stages.push((label, Box::new(move |_, _, elapsed| {
            let mut st = state.borrow_mut();
            if st.done {
                if part == 9 {
                    verdict(label, &st.samples, down, first);
                    *st = Pass::default();
                }
                return true;
            }
            if elapsed > Duration::from_secs(12) {
                return true;
            }
            let offset = report().offset;
            if st.last_offset.is_some_and(|o| (o - offset).abs() < 0.5) {
                st.still += 1;
            } else {
                st.still = 0;
            }
            st.last_offset = Some(offset);
            if st.still < 2 {
                return false;
            }
            if let Some(thumb) = thumb() {
                st.samples.push(Sample { thumb, offset });
            }
            let moved = st.samples.len() >= 2 && (st.samples[st.samples.len() - 2].offset - offset).abs() >= 0.5;
            if st.acts > 0 && !moved && st.still >= 4 || st.acts > 400 {
                st.done = true;
                return false;
            }
            if st.since.is_none() {
                st.since = Some(Instant::now());
            }
            if moved || st.acts == 0 || st.still == 2 {
                st.acts += 1;
                match (by_key, down) {
                    (true, true) => headless::press(slint::platform::Key::PageDown),
                    (true, false) => headless::press(slint::platform::Key::PageUp),
                    (false, true) => wheel(-600.0),
                    (false, false) => wheel(600.0),
                }
            }
            false
        })));
    }
    stages
}

fn verdict(label: &str, samples: &[Sample], down: bool, first: bool) {
    let heights: Vec<f32> = samples.iter().map(|s| s.thumb.height).collect();
    let (min, max) = heights.iter().fold((f32::MAX, f32::MIN), |(a, b), h| (a.min(*h), b.max(*h)));
    let changes = heights.windows(2).filter(|w| (w[0] - w[1]).abs() > 1.0).count();
    let backwards: Vec<String> = samples
        .windows(2)
        .enumerate()
        .filter(|(_, w)| if down { w[1].thumb.top < w[0].thumb.top - 1.0 } else { w[1].thumb.top > w[0].thumb.top + 1.0 })
        .map(|(n, w)| format!("step {}: {:.1} → {:.1}", n + 1, w[0].thumb.top, w[1].thumb.top))
        .collect();
    let trace: Vec<String> = samples.iter().step_by((samples.len() / 12).max(1)).map(|s| format!("{:.0}/{:.0}", s.thumb.top, s.thumb.height)).collect();
    println!("perf scrollbar {label}: {} steps, thumb height {min:.1}..{max:.1}px, changed {changes} times, {} steps backwards; top/height: {}", samples.len(), backwards.len(), trace.join(" "));
    check(samples.len() >= 10, &format!("{label}: the pass took {} steps", samples.len()));
    if !first {
        check(max - min <= 3.0, &format!("{label}: the thumb keeps its size (height {min:.1}..{max:.1}px, changed {changes} times in {} steps)", samples.len()));
    }
    check(backwards.is_empty(), &format!("{label}: the thumb moves with the reader, never back ({} steps backwards{}{})",
        backwards.len(), if backwards.is_empty() { "" } else { ": " }, backwards.iter().take(6).cloned().collect::<Vec<_>>().join(", ")));
    if let Some(last) = samples.last() {
        let place = last.thumb.place();
        let ok = if down { (last.thumb.top + last.thumb.height - last.thumb.track_bottom).abs() <= 1.5 } else { (last.thumb.top - last.thumb.track_top).abs() <= 1.5 };
        check(ok, &format!("{label}: at the {} the thumb is at the {} of its track (place {place:.3}, thumb {:.1}..{:.1}, track {:.1}..{:.1})",
            if down { "end" } else { "top" }, if down { "bottom" } else { "top" },
            last.thumb.top, last.thumb.top + last.thumb.height, last.thumb.track_top, last.thumb.track_bottom));
    }
}

/// `--check scrollbar --out DIR`.
pub(super) fn scrollbar_check(out: String) {
    let state = Rc::new(RefCell::new(Pass::default()));
    let (out1, out2) = (out.clone(), out);
    let count = Rc::new(RefCell::new(0usize));
    let mut stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/add-agent", &json!({"session": SESSION, "persona": "Steady"})).is_ok(), "the Host takes a new chat");
            true
        })),
        ("fill", Box::new({
            let count = count.clone();
            move |app, _, _| {
                if app.engine.borrow().roster().find(SESSION).is_none() {
                    return false;
                }
                let body = chat();
                *count.borrow_mut() = body["turns"].as_array().map_or(0, Vec::len);
                check(control("/__control/turns", &body).is_ok(), &format!("the Host takes a long chat of {} rows", count.borrow()));
                app.engine.borrow_mut().select(SESSION);
                crate::pump();
                true
            }
        })),
        ("opened", Box::new({
            let count = count.clone();
            move |_, window, elapsed| {
                let shown = rows(window).len();
                if shown < 300 || elapsed < Duration::from_millis(1500) {
                    return false;
                }
                check(report().follows && report().at_end, &format!("the chat opens at its end ({shown} rows shown of {} turns' rows)", count.borrow()));
                let thumb = thumb();
                check(thumb.is_some(), &format!("the chat shows a scrollbar thumb: {thumb:?}"));
                shot(&out1, "scrollbar-01-opened");
                true
            }
        })),
    ];
    stages.extend(pass(state.clone(), "first pass up by wheel", false, false, true));
    stages.extend(pass(state.clone(), "down by wheel", true, false, false));
    stages.push(("focus", Box::new(|app, _, _| {
        app.focus_transcript();
        true
    })));
    stages.extend(pass(state.clone(), "up by Page Up", false, true, false));
    stages.extend(pass(state.clone(), "down by Page Down", true, true, false));
    stages.push(("end", Box::new(move |_, _, elapsed| {
        if elapsed < Duration::from_millis(500) {
            return false;
        }
        let (visible, numbers) = super::scroll_checks::last_row_visible();
        check(visible, &format!("Page Down reaches the true end: {numbers}"));
        shot(&out2, "scrollbar-02-end");
        true
    })));
    run_stages(stages);
}
