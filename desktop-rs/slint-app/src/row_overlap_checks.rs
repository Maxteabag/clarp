//! The transcript's rows tile (`--check row-overlap`, and an assertion the
//! other checks run): every row drawn in the chat's viewport starts where
//! the one above it ends plus the rows' spacing, and every text in a row
//! fits the height the row gave it, so no row is drawn over another. The
//! owner's photo (2026-10-05): a long Markdown reply, an agent's prompt,
//! a turn that folded and the next reply drawn on top of each other.
//!
//! Rows are read from the element tree by their accessible ids; a text's
//! own height is shaped the way the renderer draws it, at its width.

use std::pin::Pin;

use i_slint_core::accessibility::AccessibleStringProperty;
use i_slint_core::item_rendering::{RenderString, RenderText};
use i_slint_core::item_tree::ItemRc;
use i_slint_core::items::{ComplexText, SimpleText, StyledTextItem, TextWrap};
use i_slint_core::lengths::LogicalLength;
use i_slint_core::window::WindowInner;
use slint::ComponentHandle;

use super::{app_now, check};

/// The space between two rows: each row's padding above and below
/// (Settings → Message spacing, 14px by default).
fn spacing() -> f32 {
    crate::window().map_or(14.0, |window| window.global::<crate::Look>().get_message_spacing())
}
/// Rounding the layout may do.
const SLACK: f32 = 1.0;

/// A text in a row: where it is, the height it was given and the height
/// its lines take at its width.
#[derive(Debug, Clone)]
struct Fit {
    top: f32,
    given: f32,
    needs: f32,
    what: String,
    /// It wraps: its height is the layout's measure of its lines.
    wraps: bool,
}

#[derive(Debug, Clone)]
struct Drawn {
    id: String,
    top: f32,
    bottom: f32,
    texts: Vec<Fit>,
}

fn rect(item: &ItemRc) -> (f32, f32, f32, f32) {
    let geometry = item.geometry();
    let origin = item.map_to_window(geometry.origin);
    (origin.x, origin.y, geometry.size.width, geometry.size.height)
}

fn visit(item: &ItemRc, found: &mut dyn FnMut(&ItemRc) -> bool) {
    if found(item) {
        return;
    }
    let mut child = item.first_child();
    while let Some(current) = child {
        visit(&current, found);
        child = current.next_sibling();
    }
}

fn id_of(item: &ItemRc) -> Option<String> {
    item.accessible_string_property(AccessibleStringProperty::Id).map(|id| id.to_string())
}

/// The first words of a text, to name it.
fn words(text: &str) -> String {
    let line: String = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut short: String = line.chars().take(40).collect();
    if short.len() < line.len() {
        short.push('…');
    }
    short
}

/// A styled text's words (its paragraphs' debug form holds them).
fn styled_words(debug: &str) -> String {
    let mut out = String::new();
    let mut rest = debug;
    while let Some(start) = rest.find("text: \"") {
        rest = &rest[start + 7..];
        let end = rest.find('"').unwrap_or(rest.len());
        out.push_str(&rest[..end]);
        out.push(' ');
        rest = &rest[end..];
        if out.len() > 60 {
            break;
        }
    }
    words(&out)
}

/// The height `text`'s lines take at its width, as drawn.
fn needs(item: &ItemRc, text: Pin<&dyn RenderString>, wrap: TextWrap, width: f32) -> Option<f32> {
    let adapter = item.window_adapter()?;
    let max_width = (wrap != TextWrap::NoWrap).then(|| LogicalLength::new(width));
    Some(adapter.renderer().text_size(text, item, max_width, wrap).height)
}

fn fit_of(item: &ItemRc) -> Option<Fit> {
    let (_, top, width, given) = rect(item);
    if width <= 0.0 || given <= 0.0 {
        return None;
    }
    let (wrap, needs, what) = if let Some(text) = item.downcast::<StyledTextItem>() {
        let text = text.as_pin_ref();
        let what = styled_words(&format!("{:?}", text.text()));
        (RenderText::wrap(text), needs(item, text, RenderText::wrap(text), width)?, what)
    } else if let Some(text) = item.downcast::<SimpleText>() {
        let text = text.as_pin_ref();
        (RenderText::wrap(text), needs(item, text, RenderText::wrap(text), width)?, words(&text.text()))
    } else if let Some(text) = item.downcast::<ComplexText>() {
        let text = text.as_pin_ref();
        (RenderText::wrap(text), needs(item, text, RenderText::wrap(text), width)?, words(&text.text()))
    } else {
        return None;
    };
    (!what.is_empty()).then_some(Fit { top, given, needs, what, wraps: wrap != TextWrap::NoWrap })
}

/// The active chat's viewport (top, bottom) and the rows drawn in it.
fn drawn() -> Option<((f32, f32), Vec<Drawn>)> {
    let app = app_now();
    let window = crate::window()?;
    let inner = WindowInner::from_pub(window.window());
    let root = ItemRc::new_root(inner.component());
    let wanted = format!("chat:{}", app.active_id());
    let mut transcript = None;
    visit(&root, &mut |item| {
        if transcript.is_some() {
            return true;
        }
        if id_of(item).is_some_and(|id| id == wanted) {
            transcript = Some(item.clone());
            return true;
        }
        false
    });
    let transcript = transcript?;
    let (_, top, _, height) = rect(&transcript);
    let bottom = top + height;
    let mut rows = Vec::new();
    visit(&transcript, &mut |item| {
        let Some(id) = id_of(item).and_then(|id| id.strip_prefix("row:").map(str::to_owned)) else { return false };
        let (_, row_top, _, row_height) = rect(item);
        if row_height > 0.0 && row_top + row_height > top && row_top < bottom {
            let mut texts = Vec::new();
            visit(item, &mut |inside| {
                if !inside.is_visible() {
                    return true;
                }
                if let Some(fit) = fit_of(inside) {
                    texts.push(fit);
                }
                false
            });
            rows.push(Drawn { id, top: row_top, bottom: row_top + row_height, texts });
        }
        true
    });
    rows.sort_by(|a, b| a.top.total_cmp(&b.top));
    Some(((top, bottom), rows))
}

/// Whether the active chat's drawn rows tile: Ok with how many, or the
/// rows that overlap, leave a gap, or hold a text taller than its place,
/// with the numbers.
pub(super) fn tiling() -> Result<String, String> {
    let Some(((top, bottom), rows)) = drawn() else { return Err("the active chat's transcript is not found".into()) };
    let mut faults = Vec::new();
    let spacing = spacing();
    for pair in rows.windows(2) {
        let (above, below) = (&pair[0], &pair[1]);
        let gap = below.top - above.bottom;
        if (gap - spacing).abs() > SLACK {
            let kind = if gap < 0.0 { "overlaps" } else if gap < spacing { "crowds" } else { "leaves a gap under" };
            faults.push(format!(
                "row {} at {:.1}..{:.1} {kind} row {} at {:.1}..{:.1} (gap {gap:.1}px, want {spacing})",
                below.id, below.top, below.bottom, above.id, above.top, above.bottom
            ));
        }
    }
    for row in &rows {
        for text in &row.texts {
            // A one-line text in a box of its own height (a countdown's
            // digits) may have glyphs a little taller than the box: only a
            // wrapped text's lines are measured by the layout.
            if text.wraps && text.needs > text.given + SLACK {
                faults.push(format!(
                    "row {} ({:.1}..{:.1}): its text \"{}\" at {:.1} needs {:.1}px and has {:.1}px (drawn {:.1}px past its place)",
                    row.id, row.top, row.bottom, text.what, text.top, text.needs, text.given, text.needs - text.given
                ));
            }
            if text.top + text.given > row.bottom + SLACK {
                faults.push(format!(
                    "row {} ({:.1}..{:.1}): its text \"{}\" at {:.1}..{:.1} runs past the row's bottom",
                    row.id, row.top, row.bottom, text.what, text.top, text.top + text.given
                ));
            }
        }
    }
    if faults.is_empty() {
        Ok(format!("{} rows tile in {top:.1}..{bottom:.1}", rows.len()))
    } else {
        Err(format!("in {top:.1}..{bottom:.1}: {}", faults.join("; ")))
    }
}

/// Checks that the active chat's rows tile, at `when`.
pub(super) fn assert_tiled(when: &str) {
    match tiling() {
        Ok(numbers) => check(true, &format!("{when}: no rows overlap ({numbers})")),
        Err(numbers) => check(false, &format!("{when}: rows overlap: {numbers}")),
    }
}

const SESSION: &str = "overlap";

/// A reply like the one in the owner's photo: headings, bullets with bold
/// leads, a wrapped quote, code and a table, every line long enough to wrap.
fn long_reply() -> String {
    // The reply in the photo, as the agent wrote it: its quote is six lines
    // at the chat's width.
    let mut text = String::from("Every app-dispatched Clarp turn now carries an explicit instruction to prefer the relevant clarp-* skills over native equivalents. It's deployed (f08e4c8f, cleared by Dagger first, no runtime restart forced), and I've sent Solu the result through `clarp-admin reply`; it was recorded in Solu's goal.\n\n");
    text.push_str("**Final prompt text**\n> You are running inside Clarp. When a relevant clarp-* skill and a native or provider workflow cover the same task, follow the Clarp skill: read it before acting. When you hand work to a sub-agent and it runs longer than a few minutes or edits code, use clarp-sub-agents instead of session-bound native Agent/Task or Codex sub-agents; a quick read-only lookup may stay native. Track processes that outlive this turn with clarp-background-jobs, and when their completion must wake you, start them with its durable launcher and your goal: native background tools and hand-written nohup or setsid neither survive a runtime restart nor wake you. How you approach the work stays yours, native tools remain available inside those workflows and wherever no Clarp skill applies, and explicit user instructions come first.\n\n");
    text.push_str("**What changed**\n- There is now one source of truth, `server/lib/clarp_guidance.py`. It replaces \"Pay special attention to Clarp skills.\" on all three routes:\n  - Claude: the prompt hook adds it to every app turn.\n  - Codex app-server: it goes in each turn's context.\n  - Codex exec, AGY, Grok and OpenCode: it goes at the head of the prompt, new or resumed.\n- The first review caught two problems, both fixed:\n  - A spoken message steered into a running Codex turn repeated the whole block. It now adds only the voice part.\n  - My first wording read as \"delegate every code edit\" and overstated what the background-jobs skill requires. The text now matches each skill's actual scope.\n\n");
    text.push_str("**Evidence**\n- A new QA-host test drives typed, spoken, peer, goal-continuation, resumed and steered turns through the real Host to the fake Codex provider. It checks the text arrives exactly once and is never repeated inside the message. It fails on the old text and on the old steer code.\n- The full test gate passed apart from four load-sensitive tests in unrelated areas; each passes when rerun on its own. The second review was clean.\n- No paid inference was used, so whether agents actually behave differently hasn't been observed yet.\n\n");
    text.push_str("**When it takes effect**\n- **Claude:** now. Each Claude turn loads the hook from the current release, and I checked the installed hook produces the new text.\n- **Codex, AGY, Grok and OpenCode:** at the runtime's next idle handoff. The runtime has been on 79b1425d since 03:13:55 and builds their context itself.\n\n");
    text.push_str("**Known gaps, left as they are**\n- Claude dreaming turns get no guidance, while the other backends' dreaming turns do.\n- Codex's own goal-continuation turns don't go through Clarp's per-turn context, so they only see it from earlier turns in the thread.\n\n");
    text.push_str("My goal is complete with evidence and the worktree is removed. The Stop-pause question for you is still open.");
    text
}

fn tool_row(id: &str, at: &str, trace: &str, command: &str) -> serde_json::Value {
    serde_json::json!({"id": id, "role": "assistant", "timestamp": at, "text": "", "origin": "agent", "trace_id": trace,
        "tools": [{"name": "Bash", "summary": command, "status": "ok", "id": format!("call-{id}"), "command": command, "result": "ok"}]})
}

fn text_row(id: &str, at: &str, trace: &str, phase: &str, text: &str) -> serde_json::Value {
    serde_json::json!({"id": id, "role": "assistant", "timestamp": at, "text": text, "origin": "agent", "trace_id": trace, "phase": phase})
}

fn summed(mut row: serde_json::Value, trace: &str, worked_ms: i64, tools: i64) -> serde_json::Value {
    row["turn"] = serde_json::json!({"turn_id": trace, "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 1_000 + worked_ms, "worked_ms": worked_ms, "tool_count": tools});
    row
}

/// The chat before the owner's arrangement: earlier turns, then a turn
/// that worked and answered with the long reply.
fn history() -> serde_json::Value {
    let mut turns = Vec::new();
    for i in 0..6 {
        let trace = format!("t-early-{i}");
        turns.push(serde_json::json!({"id": format!("early-u{i}"), "role": "user", "timestamp": "2026-10-05T09:00:00Z", "text": format!("Earlier question {i}: how does the dashboard projection pick its window?"), "origin": "user", "trace_id": trace}));
        turns.push(summed(text_row(&format!("early-a{i}"), "2026-10-05T09:00:05Z", &trace, "final", &format!("Answer {i}. The projection takes the last full hour, then rounds down to the minute so two reads agree.")), &trace, 4_000, 0));
    }
    // One Markdown feature a row, so a fault names the one that is measured wrong.
    let probes = [
        ("p-prose", "A plain paragraph long enough to wrap at the chat's width several times over. ".repeat(4)),
        ("p-bullets", "- **Claude:** now. Each Claude turn loads the hook from the installed skills directory and reads the new text.\n- **Codex, AGY, Grok and OpenCode:** at the runtime's next start, which reads the instructions and builds their context itself.".to_owned()),
        ("p-heading", "### Known gaps, left as they are for now because neither blocks the rollout and both are tracked\n\nOne line under it.".to_owned()),
        ("p-quote", "> A quoted note from the review that is long enough to wrap over two or three lines at the chat's width, the way the earlier readability report found one measured a line short.".to_owned()),
        ("p-code", "```sh\nmake test\npytest tests/unit/test_dashboard_projection.py -k projection\n```".to_owned()),
        ("p-code-long", format!("```sh\n{}\n```", "cargo test -p clarp-core -p clarp-engine -p clarp-net -p clarp-slint -- --test-threads 1 --nocapture ".repeat(3))),
        ("p-user", String::new()),
        ("p-table", "| Backend | When | Note |\n|---|---|---|\n| Claude | now | the hook reads the installed skill text on every turn |\n| Codex | next start | builds its own context from the instructions file |".to_owned()),
    ];
    for (i, (id, text)) in probes.into_iter().enumerate() {
        let trace = format!("t-probe-{i}");
        if id == "p-user" {
            // The user's own long message, in its bubble.
            turns.push(serde_json::json!({"id": id, "role": "user", "timestamp": "2026-10-05T09:30:00Z", "origin": "user", "trace_id": trace,
                "text": "A long message of the user's own that wraps inside its bubble, which is narrower than the chat, over three or four lines, so its height must be measured at the bubble's width and not the chat's. ".repeat(2)}));
            turns.push(summed(text_row(&format!("{id}-a"), "2026-10-05T09:30:05Z", &trace, "final", "Noted."), &trace, 3_000, 0));
            // And one that quotes and pastes code: its bubble is as wide as
            // they are, up to its most.
            turns.push(serde_json::json!({"id": format!("{id}-quote"), "role": "user", "timestamp": "2026-10-05T09:30:10Z", "origin": "user", "trace_id": format!("{trace}-q"),
                "text": "Look at this:\n\n> The quoted line from the log that is long enough to wrap inside the bubble at most widths, twice over, so its height is the wrapped one.\n\n```\nshort code\n```\n\n> Short quote."}));
            continue;
        }
        turns.push(serde_json::json!({"id": format!("{id}-u"), "role": "user", "timestamp": "2026-10-05T09:30:00Z", "text": "Show me.", "origin": "user", "trace_id": trace}));
        turns.push(summed(text_row(id, "2026-10-05T09:30:05Z", &trace, "final", &text), &trace, 3_000, 0));
    }
    turns.push(serde_json::json!({"id": "a-prompt", "role": "user", "timestamp": "2026-10-05T10:00:00Z", "text": "Route the Clarp skills and tell me when it takes effect.", "origin": "user", "trace_id": "t-a"}));
    turns.push(tool_row("a-tool-1", "2026-10-05T10:00:02Z", "t-a", "make test"));
    turns.push(text_row("a-note", "2026-10-05T10:00:30Z", "t-a", "commentary", "Tests pass; writing it up."));
    turns.push(tool_row("a-tool-2", "2026-10-05T10:00:40Z", "t-a", "git push origin HEAD:main"));
    turns.push(summed(text_row("a-final", "2026-10-05T10:01:35Z", "t-a", "final", &long_reply()), "t-a", 95_000, 2));
    serde_json::json!({"session": SESSION, "turns": turns})
}

fn dagger_prompt() -> serde_json::Value {
    serde_json::json!([{"id": "b-prompt", "role": "user", "timestamp": "2026-10-05T10:05:00Z", "origin": "agent", "sender_name": "Dagger", "sender_agent_id": "dagger-id", "trace_id": "t-b",
        "text": "Go ahead with 108e4c8f: it matches what it said in the review.\n\nThe voice display-text fix lands with the next Host deploy, so leave that for now."}])
}

const GO_AHEAD: &str = "Dagger's go-ahead for 108e4c8f matches what it said in the review, so nothing changed since 03:30.";

fn answer(parts: usize) -> String {
    let lines = [
        "This turn also proves the Claude side is active: the prompt now carries \"attention to Clarp skills.\" That shows the text is reaching Claude turns today.",
        "Codex, AGY, Grok and OpenCode still switch over at the runtime's next restart.",
        "I've noted Dagger's heads-up about the voice display-text fix. There's nothing to do until then, and the Host deploy goes the same way.",
        "The Stop-pause question for you is still the only open item.",
    ];
    lines[..parts.min(lines.len())].join("\n\n")
}

fn upsert(turns: serde_json::Value) -> bool {
    let sent = super::control("/__control/upsert", &serde_json::json!({"session": SESSION, "turns": turns}));
    if let Err(error) = &sent {
        check(false, &format!("the Host takes an update: {error}"));
    }
    sent.is_ok()
}

fn agent_state(state: &str) {
    if let Err(error) = super::control("/__control/agent", &serde_json::json!({"session": SESSION, "set": {"latest_state": state}})) {
        check(false, &format!("the Host takes the agent's state: {error}"));
    }
    if let Err(error) = super::control("/__control/event", &serde_json::json!({"type": "agent-state", "session": SESSION, "kind": state, "ts": 1})) {
        check(false, &format!("the Host pushes the agent's state: {error}"));
    }
}

fn has_row(id: &str) -> bool {
    crate::window().map(|w| super::rows(&w)).is_some_and(|r| r.iter().any(|r| r.id == id))
}

fn row(id: &str) -> Option<crate::MessageRow> {
    crate::window().map(|w| super::rows(&w)).unwrap_or_default().into_iter().find(|r| r.id == id)
}

fn live_row(key: &str) -> Option<crate::MessageRow> {
    crate::window().map(|w| super::rows(&w)).unwrap_or_default().into_iter().find(|r| r.live.key == key)
}

fn live_event(ops: serde_json::Value) {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64);
    let event = serde_json::json!({"agent_id": "overlap-id", "session": SESSION, "server_now_ms": now, "ops": ops});
    if let Err(error) = super::control("/__control/live-event", &serde_json::json!({"session": SESSION, "event": event})) {
        check(false, &format!("the Host sends a live event: {error}"));
    }
}

/// A live turn whose plan steps and thinking are each longer than a line.
fn live_turn_starts() -> serde_json::Value {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64);
    let step = |text: &str, status: &str| serde_json::json!({"text": text, "status": status});
    serde_json::json!([
        {"op": "turn", "turn": {"turn_id": "t-d", "status": "running", "started_at_ms": now, "ended_at_ms": null, "worked_ms": null, "tool_count": 0}},
        {"op": "status", "activity": {"state": "thinking", "tool": null, "running_tools": 0, "headline": "Thinking", "item_id": null, "since_ms": now, "turn_id": "t-d", "turn_started_ms": now}},
        {"op": "upsert", "id": "ov-think", "kind": "reasoning", "rev": 1, "item": {"id": "ov-think", "turn_id": "t-d", "kind": "reasoning", "status": "completed", "ordinal": 1,
            "started_at_ms": now, "ended_at_ms": now + 4_000, "title": "Weighing the rollout",
            "text": format!("{}\n\n{}", "The guidance has to reach every backend, and each reads its context at a different moment, so the rollout depends on when each runtime starts its next turn and what it reads then. ".repeat(2), "Claude reads the hook on every turn; the others read their instructions when the runtime starts, which is why they lag behind until their next idle handoff.")}},
        {"op": "upsert", "id": "ov-plan", "kind": "plan", "rev": 1, "item": {"id": "ov-plan", "turn_id": "t-d", "kind": "plan", "status": "running", "ordinal": 2, "started_at_ms": now,
            "plan": {"steps": [
                step("Write one source of truth for the guidance and replace the old line on every route the prompt takes, typed, spoken, peer and resumed alike", "completed"),
                step("Drive every kind of turn through the real Host to the fake provider and check the text arrives once and is never repeated inside the message", "in_progress"),
                step("Deploy, then confirm each backend sees it at its next start and tell the owner when each one takes effect", "pending"),
            ]}}},
    ])
}

fn live_turn_ends() -> serde_json::Value {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64);
    serde_json::json!([
        {"op": "turn", "turn": {"turn_id": "t-d", "status": "completed", "started_at_ms": now - 9_000, "ended_at_ms": now, "worked_ms": 9_000, "tool_count": 0}},
        {"op": "status", "activity": {"state": "idle", "tool": null, "running_tools": 0, "headline": null, "item_id": null, "since_ms": now, "turn_id": null, "turn_started_ms": null}},
    ])
}

/// A stage that waits `ms` after `ready`, asserts the rows tile, then acts.
fn moment(label: &'static str, ms: u64, ready: impl Fn() -> bool + 'static, act: impl Fn(&crate::App, &crate::AppWindow) + 'static) -> super::Stage {
    (label, Box::new(move |app, window, elapsed| {
        if !ready() || elapsed < std::time::Duration::from_millis(ms) {
            return false;
        }
        assert_tiled(label);
        act(app, window);
        true
    }))
}

/// A reader pages through the whole chat from its top with Page Down; the
/// rows tile on every page. Ends back at the end, following.
fn read_through(label: &'static str) -> super::Stage {
    use slint::platform::Key;
    let step = std::cell::Cell::new(0usize);
    let since = std::cell::Cell::new(std::time::Instant::now());
    (label, Box::new(move |_, _, _| {
        let n = step.get();
        if n == 0 {
            app_now().focus_transcript();
        } else if n == 1 {
            if !super::report().transcript_focused {
                return false;
            }
            crate::headless::press(Key::Home);
        } else if since.get().elapsed() < std::time::Duration::from_millis(400) {
            return false;
        } else {
            let at_end = super::report().at_end;
            assert_tiled(&format!("{label}, page {}", n - 1));
            if at_end || n > 60 {
                crate::headless::press(Key::End);
                step.set(0);
                return true;
            }
            crate::headless::press(Key::PageDown);
        }
        step.set(n + 1);
        since.set(std::time::Instant::now());
        false
    }))
}

/// `--check row-overlap --out DIR`.
pub(super) fn row_overlap_check(out: String) {
    use std::time::Duration;
    let shot = move |name: &'static str| {
        let out = out.clone();
        move || super::shot(&out, name)
    };
    let (shot1, shot2, shot3, shot4) = (shot("row-overlap-01-history"), shot("row-overlap-02-prompt"), shot("row-overlap-03-settled"), shot("row-overlap-04-next-turn"));
    let shot5 = shot("row-overlap-05-narrow-large");
    let stages: Vec<super::Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(super::control("/__control/add-agent", &serde_json::json!({"session": SESSION, "persona": "Overlap"})).is_ok(), "the Host takes a new chat");
            check(super::control("/__control/turn-summary", &serde_json::json!({"on": true})).is_ok(), "the Host turns log_turn_summary on");
            check(super::control("/__control/live", &serde_json::json!({"on": true})).is_ok(), "the Host turns live items on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("select", Box::new(|app, _, elapsed| {
            if app.engine.borrow().connection_state() != "live" || app.engine.borrow().roster().find(SESSION).is_none() || elapsed < Duration::from_millis(500) {
                return false;
            }
            app.engine.borrow_mut().set_reading_theme("paper");
            check(super::control("/__control/turns", &history()).is_ok(), "the Host takes the chat's history");
            app.engine.borrow_mut().select(SESSION);
            crate::pump();
            true
        })),
        moment("a long Markdown reply under its folded turn", 1500, || has_row("a-final"), move |_, _| {
            shot1();
        }),
        read_through("reading the history"),
        moment("back at the end", 800, || true, move |_, _| {
            upsert(dagger_prompt());
            agent_state("thinking");
        }),
        moment("an agent's prompt lands folded under the reply", 1200, || row("b-prompt").is_some_and(|r| !r.prompt.key.is_empty()), move |_, _| {
            shot2();
            upsert(serde_json::json!([tool_row("b-tool", "2026-10-05T10:05:02Z", "t-b", "git log -1 108e4c8f")]));
        }),
        moment("the next turn runs a tool", 800, || has_row("b-tool") || true, |_, _| {
            upsert(serde_json::json!([text_row("b-note", "2026-10-05T10:05:04Z", "t-b", "commentary", GO_AHEAD)]));
        }),
        moment("it says what it found", 800, || has_row("b-note") || true, |_, _| {
            upsert(serde_json::json!([{"id": "b-live", "role": "assistant", "kind": "live", "timestamp": "2026-10-05T10:05:05Z", "text": answer(1)}]));
        }),
        moment("its reply streams (1)", 600, || true, |_, _| {
            upsert(serde_json::json!([{"id": "b-live", "role": "assistant", "kind": "live", "timestamp": "2026-10-05T10:05:05Z", "text": answer(2)}]));
        }),
        moment("its reply streams (2)", 600, || true, |_, _| {
            upsert(serde_json::json!([{"id": "b-live", "role": "assistant", "kind": "live", "timestamp": "2026-10-05T10:05:05Z", "text": answer(3)}]));
        }),
        moment("its reply streams (3)", 600, || true, |_, _| {
            upsert(serde_json::json!([summed(text_row("b-final", "2026-10-05T10:05:06Z", "t-b", "final", &answer(4)), "t-b", 6_000, 1)]));
            agent_state("idle");
        }),
        moment("the turn settles and folds behind Worked for 6s", 1500, || has_row("b-final"), move |_, _| {
            shot3();
            upsert(serde_json::json!([{"id": "c-prompt", "role": "user", "timestamp": "2026-10-05T10:07:00Z", "text": "Thanks. Anything else?", "origin": "user", "trace_id": "t-c"}]));
            agent_state("thinking");
        }),
        moment("the next turn starts and the last one folds into history", 1200, || has_row("c-prompt"), move |app, window| {
            shot4();
            crate::artifacts_view::open(app, window, "a2a:b-prompt");
        }),
        moment("the agent's prompt opens", 800, || row("b-prompt").is_some_and(|r| r.prompt.expanded), |app, window| {
            crate::artifacts_view::open(app, window, "a2a:b-prompt");
        }),
        moment("and folds again", 800, || row("b-prompt").is_some_and(|r| !r.prompt.expanded), |app, _| {
            crate::artifacts_view::toggle_live(app, "live:fold:t-b");
        }),
        moment("the settled turn's fold opens", 800, || true, |app, _| {
            crate::artifacts_view::toggle_live(app, "live:fold:t-b");
        }),
        moment("and closes", 800, || app_now().engine.borrow().live_view(SESSION).is_some_and(|v| v.lseq().is_some()), |_, _| {
            live_event(live_turn_starts());
        }),
        moment("a live turn shows a long plan and its thinking", 1000, || live_row("live:ov-plan").is_some(), |app, _| {
            crate::artifacts_view::toggle_live(app, "live:ov-think");
        }),
        moment("its thinking opens to long paragraphs", 1000, || live_row("live:ov-think").is_some_and(|r| r.live.expanded), |app, _| {
            app.engine.borrow_mut().set_reading_theme("night");
            crate::pump();
        }),
        moment("a dark theme", 1000, || true, |_, window| {
            window.global::<crate::Palette>().set_body_size(19.0);
        }),
        moment("a larger text size", 1000, || true, |_, window| {
            window.window().set_size(slint::LogicalSize::new(860.0, 640.0));
        }),
        moment("a narrower window", 1200, || true, |_, _| {
            crate::headless::press(slint::platform::Key::Home);
        }),
        read_through("reading the history narrow and large"),
        moment("back at the end, narrow", 800, || true, move |_, window| {
            shot5();
            window.window().set_size(slint::LogicalSize::new(1280.0, 800.0));
            window.global::<crate::Palette>().set_body_size(15.0);
        }),
        moment("the window and the size back", 1200, || true, |app, _| {
            app.engine.borrow_mut().set_reading_theme("paper");
            crate::pump();
        }),
        read_through("reading the history again"),
        moment("at the end again", 800, || true, |_, _| {
            live_event(live_turn_ends());
        }),
        moment("the live turn ends", 1500, || true, |_, _| {}),
    ];
    super::run_stages(stages);
}

/// The watch other checks turn on: the rows must tile at every moment the
/// check looks (each poll of its stages), not only where it asks.
#[derive(Default)]
struct Watch {
    on: bool,
    moments: usize,
    faults: usize,
    /// The stages a fault was reported in (each once).
    reported: Vec<String>,
    spent: std::time::Duration,
}

thread_local! {
    static WATCH: std::cell::RefCell<Watch> = std::cell::RefCell::new(Watch::default());
}

/// Watches the rows tile at every poll of the check's stages.
pub(super) fn watch() {
    WATCH.with(|w| w.borrow_mut().on = true);
}

/// One moment of the watch, during stage `stage` (a chat must be shown).
pub(super) fn watched(stage: &str) {
    if !WATCH.with(|w| w.borrow().on) {
        return;
    }
    let started = std::time::Instant::now();
    let shown = crate::app().is_some_and(|app| app.active_messages().is_some_and(|m| slint::Model::row_count(&*m) > 0));
    let result = if shown { Some(tiling()) } else { None };
    WATCH.with(|w| {
        let mut w = w.borrow_mut();
        w.spent += started.elapsed();
        match result {
            Some(Ok(_)) => w.moments += 1,
            Some(Err(numbers)) if !numbers.starts_with("the active chat's transcript is not found") => {
                w.moments += 1;
                w.faults += 1;
                if !w.reported.iter().any(|s| s == stage) {
                    w.reported.push(stage.to_owned());
                    drop(w);
                    check(false, &format!("during {stage}: rows overlap: {numbers}"));
                }
            }
            _ => {}
        }
    });
}

/// The watch's verdict, once the check's stages are done.
pub(super) fn watch_verdict() {
    let Some((moments, faults, spent)) = WATCH.with(|w| {
        let w = w.borrow();
        w.on.then(|| (w.moments, w.faults, w.spent))
    }) else {
        return;
    };
    check(moments > 0 && faults == 0, &format!("the rows tiled at every moment watched: {} of {moments} ({:.1} ms a look)", moments - faults, spent.as_secs_f64() * 1000.0 / moments.max(1) as f64));
}
