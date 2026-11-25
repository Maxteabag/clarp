//! `--check observe`: watches one chat on whatever Host CLARP_BASE_URL names
//! (a real one included) and records what the desktop shows: every
//! `CLARP_OBSERVE_EVERY_MS` (default 3000) a line with the live state and a
//! screenshot, for `CLARP_OBSERVE_SECONDS` (default 60). Read-only apart from
//! opening the chat named by `CLARP_OBSERVE_SESSION`.
//!
//! With `CLARP_OBSERVE_ROWS=1` it also prints one JSON line per tick (up to
//! 5 Hz with `CLARP_OBSERVE_EVERY_MS=200`): the rows drawn, in screen order,
//! with their kind, id, full text, y and height, and the scroll
//! (`{"t","session","scroll":{"top","h","ch"},"rows":[{"k","id","t","y","h"}]}`;
//! `top` px scrolled from the content's top, `h` the chat's visible height,
//! `ch` the content height, all px).
//! `CLARP_OBSERVE_SHOTS=0` leaves the screenshots out. Meant for disposable
//! Hosts with synthetic prompts: the line carries the messages' text.

use std::cell::Cell;
use std::time::{Duration, Instant};

use slint::ComponentHandle;

use super::{check, finish, shot};

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

pub fn observe_check(out: String) {
    let session = std::env::var("CLARP_OBSERVE_SESSION").unwrap_or_default();
    let every = Duration::from_millis(env_u64("CLARP_OBSERVE_EVERY_MS", 3000));
    let total = Duration::from_secs(env_u64("CLARP_OBSERVE_SECONDS", 60));
    let started = Instant::now();
    let opened = Cell::new(false);
    let shots = Cell::new(0u32);
    let last = Cell::new(Instant::now() - every);
    let rows_wanted = std::env::var("CLARP_OBSERVE_ROWS").is_ok_and(|v| v == "1");
    let shots_wanted = std::env::var("CLARP_OBSERVE_SHOTS").map_or(true, |v| v != "0");
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(200), move || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        if !opened.get() {
            if !app.engine.borrow().connected() || app.engine.borrow().roster().find(&session).is_none() {
                if started.elapsed() > Duration::from_secs(30) {
                    check(false, &format!("connected and {session} in the roster"));
                    finish();
                }
                return;
            }
            app.engine.borrow_mut().select(&session);
            crate::pump();
            opened.set(true);
            return;
        }
        if last.get().elapsed() < every {
            return;
        }
        last.set(Instant::now());
        let engine = app.engine.borrow();
        let view = engine.live_view(&session);
        let (status, busy, key) = crate::live_view::status(&engine, &session);
        let rows = engine.conversation(&session).map_or(0, |c| c.rows().len());
        let activity = view.map(|v| v.activity().get("state").and_then(|s| s.as_str()).unwrap_or("").to_owned()).unwrap_or_default();
        println!(
            "observe t={:>5.1}s live_items={} active={} lseq={:?} items={} activity={:?} status={:?} busy={} key={:?} log_rows={} transcript_rows={} working={}",
            started.elapsed().as_secs_f32(),
            engine.live_items(),
            engine.live_active(&session),
            view.and_then(|v| v.lseq()),
            view.map_or(0, |v| v.items().len()),
            activity,
            status,
            busy,
            key,
            rows,
            app.active_messages().map_or(0, |m| slint::Model::row_count(&*m)),
            crate::cells_view::working(&engine, &session),
        );
        drop(engine);
        if rows_wanted {
            println!("{}", rows_line(&app, &session));
        }
        if shots_wanted {
            let n = shots.get() + 1;
            shots.set(n);
            shot(&out, &format!("observe-{n:03}"));
        }
        window.window().request_redraw();
        if started.elapsed() > total {
            check(true, "observed");
            finish();
        }
    });
    super::TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

/// What kind of row the chat draws: user, assistant, live, tool, work, card,
/// notice or running.
fn row_kind(row: &crate::MessageRow) -> String {
    if !row.receipt.key.is_empty() {
        return "card".into();
    }
    if !row.prompt.key.is_empty() {
        return "user".into();
    }
    match row.author.as_str() {
        "live" => match row.live.kind.as_str() {
            "fold" if row.live.status == "running" => "running".into(),
            "fold" => "work".into(),
            "tool" | "explore" => "tool".into(),
            "message" => "live".into(),
            other => other.into(),
        },
        "assistant" if !row.live.key.is_empty() => "live".into(),
        "activity" => "tool".into(),
        "system" => "notice".into(),
        other => other.into(),
    }
}

/// One JSON line: the rows the open chat draws, in screen order, with where
/// each sits on screen (px from the top of the chat's view) and the scroll.
fn rows_line(app: &crate::App, session: &str) -> String {
    use slint::Model;
    let wall = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64);
    let pane = app.active_id().to_string();
    let starts: Vec<f32> = app.active_view().map(|v| v.before.iter().collect()).unwrap_or_default();
    // The drawn scroll in px (the pane's own geometry is a share of the window).
    let (offset, content, viewport) = crate::scroll_journal::last_view(&pane)
        .unwrap_or_else(|| (super::report().offset, starts.last().copied().unwrap_or(0.0), 0.0));
    let rows: Vec<serde_json::Value> = app
        .active_messages()
        .map(|model| {
            model
                .iter()
                .enumerate()
                .map(|(i, row)| {
                    let start = starts.get(i).copied().unwrap_or(0.0);
                    let height = starts.get(i + 1).map_or(0.0, |next| next - start);
                    let text = crate::select_text::message_text(app, &pane, &row.id).unwrap_or_else(|| row.live.text.to_string());
                    serde_json::json!({"k": row_kind(&row), "id": row.id.to_string(), "t": text, "y": start + offset, "h": height})
                })
                .collect()
        })
        .unwrap_or_default();
    serde_json::json!({"t": wall, "session": session, "scroll": {"top": -offset, "h": viewport, "ch": content}, "rows": rows}).to_string()
}
