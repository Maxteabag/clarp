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

/// The space between two rows: each row's padding above and below.
const SPACING: f32 = 14.0;
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
    let (needs, what) = if let Some(text) = item.downcast::<StyledTextItem>() {
        let text = text.as_pin_ref();
        let what = styled_words(&format!("{:?}", text.text()));
        (needs(item, text, RenderText::wrap(text), width)?, what)
    } else if let Some(text) = item.downcast::<SimpleText>() {
        let text = text.as_pin_ref();
        (needs(item, text, RenderText::wrap(text), width)?, words(&text.text()))
    } else if let Some(text) = item.downcast::<ComplexText>() {
        let text = text.as_pin_ref();
        (needs(item, text, RenderText::wrap(text), width)?, words(&text.text()))
    } else {
        return None;
    };
    (!what.is_empty()).then_some(Fit { top, given, needs, what })
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
    for pair in rows.windows(2) {
        let (above, below) = (&pair[0], &pair[1]);
        let gap = below.top - above.bottom;
        if (gap - SPACING).abs() > SLACK {
            let kind = if gap < 0.0 { "overlaps" } else if gap < SPACING { "crowds" } else { "leaves a gap under" };
            faults.push(format!(
                "row {} at {:.1}..{:.1} {kind} row {} at {:.1}..{:.1} (gap {gap:.1}px, want {SPACING})",
                below.id, below.top, below.bottom, above.id, above.top, above.bottom
            ));
        }
    }
    for row in &rows {
        for text in &row.texts {
            if text.needs > text.given + SLACK {
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
    let mut text = String::from("My first instinct was to delegate every code edit, so the routing note now matches each skill's actual scope.\n\n");
    text.push_str("## Evidence\n\n");
    text.push_str("- A new QA-host test drives typed, spoken, peer, goal-continuation and resumed prompts through a fake Codex provider. It checks the text arrives exactly once and in order, on the new text and on the old steer code.\n");
    text.push_str("- The full test gate passed apart from four load-sensitive dashboard projections that pass alone; a second review was clean.\n");
    text.push_str("- No paid inference was used, so whether agents actually follow the new guidance is unmeasured.\n\n");
    text.push_str("## When it takes effect\n\n");
    text.push_str("- **Claude:** now. Each Claude turn loads the hook from the installed skills directory and reads the new text.\n");
    text.push_str("- **Codex, AGY, Grok and OpenCode:** at the runtime's next start, which reads the instructions and builds their context itself.\n\n");
    text.push_str("### Known gaps, left as they are for now because neither blocks the rollout\n\n");
    text.push_str("- Claude dreaming turns get no guidance, while the other backends' dreaming turns do, through their own prompt.\n");
    text.push_str("- Codex's own goal-continuation turns don't go through Clarp's prompt path, so they keep the old text until the thread restarts.\n\n");
    text.push_str("> A quoted note from the review that is long enough to wrap over two or three lines at the chat's width, the way the earlier readability report found one measured a line short.\n\n");
    text.push_str("```sh\nmake test\npytest tests/unit/test_dashboard_projection.py -k projection\n```\n\n");
    text.push_str("| Backend | When | Note |\n|---|---|---|\n| Claude | now | the hook reads the installed skill text on every turn |\n| Codex | next start | builds its own context from the instructions file |\n\n");
    text.push_str("My goal is complete with evidence, and the worktree is removed.");
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

/// `--check row-overlap --out DIR`.
pub(super) fn row_overlap_check(out: String) {
    use std::time::Duration;
    let shot = move |name: &'static str| {
        let out = out.clone();
        move || super::shot(&out, name)
    };
    let (shot1, shot2, shot3, shot4) = (shot("row-overlap-01-history"), shot("row-overlap-02-prompt"), shot("row-overlap-03-settled"), shot("row-overlap-04-next-turn"));
    let stages: Vec<super::Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(super::control("/__control/add-agent", &serde_json::json!({"session": SESSION, "persona": "Overlap"})).is_ok(), "the Host takes a new chat");
            check(super::control("/__control/turn-summary", &serde_json::json!({"on": true})).is_ok(), "the Host turns log_turn_summary on");
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
        moment("the next turn starts and the last one folds into history", 1200, || has_row("c-prompt"), move |_, _| {
            shot4();
        }),
    ];
    super::run_stages(stages);
}
