//! `--check stop-receipt`: after Stop the chat says what the Host committed,
//! who stopped it and why (Host contract 61), never what will happen next.
//! The fake Host answers each Stop with one of the contract's cases: a
//! verified agent, a claimed user (an admin token), a paired user, an older
//! Host and a Stop the runtime refused; Ctrl+. stops the first, the Stop
//! command (Ctrl+K) the rest. Another device's or agent's Stop, read from
//! the interrupted state's `user_stop` detail, shows the same line. The line
//! is plain: your own Stop leaves "Stopped" to the transcript and names the
//! queue only when messages wait. A queue the Stop paused says so in the
//! composer and the explorer. On a Host that ends a stopped turn with its
//! own line (`turn_end_reason`) and tells the Stop as the word "Stopped."
//! (`stop_receipt_text`), the turn says it once: its partial answer stays,
//! one quiet "Stopped." closes it, and the composer repeats nothing and
//! sends at once.

use std::time::Duration;

use serde_json::{Value, json};
use slint::Model;
use slint::platform::Key;

use super::{Stage, app_now, check, control, posts, report, run_stages, shot, view};
use crate::headless;

const THEO: &str = "Stopped by Theo: Pebble busy with no process";

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn state(kind: &str, detail: Option<Value>) -> Result<(), String> {
    let mut event = json!({"type": "agent-state", "session": "rachel", "kind": kind, "ts": now()});
    if let Some(detail) = detail {
        event["detail"] = detail;
    }
    control("/__control/event", &event)
}

fn notice() -> String {
    view().stop_notice.to_string()
}

/// Rachel works again (which clears the last line) and the Host is set to
/// answer the next Stop with `respond`.
fn working(name: &'static str, respond: Value) -> Stage {
    let mut asked = false;
    (name, Box::new(move |app, _, _| {
        if !std::mem::replace(&mut asked, true) {
            let sent = control("/__control/stop", &json!({"respond": respond.clone()})).and_then(|()| state("thinking", None));
            check(sent.is_ok(), &format!("{name}: Rachel works and the Host's answer is set: {sent:?}"));
            return false;
        }
        if !app.engine.borrow().roster().find("rachel").is_some_and(|a| a.busy) {
            return false;
        }
        notice().is_empty()
    }))
}

/// Stops Rachel by the Stop command, as Ctrl+K runs it.
fn stop_by_command(name: &'static str) -> Stage {
    (name, Box::new(|_, window, _| {
        check(crate::commands::run(&app_now(), window, "stop-agent"), "the Stop command runs");
        true
    }))
}

/// The Host answered the `stops`th Stop and the composer says `expected`
/// ("" when the transcript's own Stop line says it all).
fn says(name: &'static str, stops: usize, expected: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, _, elapsed| {
        let posted = posts("/stop");
        let settled = if expected.is_empty() { elapsed >= Duration::from_millis(1200) } else { !notice().is_empty() };
        if posted.len() < stops || !settled || elapsed < Duration::from_millis(300) {
            return false;
        }
        check(posted[stops - 1]["body"]["session"] == "rachel", &format!("{name}: Stop is sent for Rachel"));
        check(notice() == expected, &format!("{name}: the chat says {expected:?}: {:?}", notice()));
        check(!notice().to_lowercase().contains("resume"), "and never promises a resume");
        for word in ["claimed", "verified", "no goals paused", "Stopped by you"] {
            check(!notice().contains(word), &format!("{name}: the line is plain, without {word:?}: {:?}", notice()));
        }
        shot(&out, shot_name);
        true
    }))
}

pub fn stop_receipt_check(out: String) {
    let (out_detail, out_refused, out_queue, out_turn) = (out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !loaded || !report().composer_focused {
                return false;
            }
            let added = control("/__control/add-agent", &json!({"session": "theo-97e5", "persona": "Theo"}));
            check(added.is_ok(), &format!("Theo joins the roster: {added:?}"));
            true
        })),
        ("theo", Box::new(|app, _, _| app.engine.borrow().roster().find("theo-97e5").is_some())),
        // ---- a verified agent stopped it, by Ctrl+.
        working("verified agent working", json!({"ok": true, "terminated": 1, "stop_actor": "agent:theo-97e5", "stop_actor_verified": true,
            "stop_reason": "Pebble busy with no process", "queue_paused": true, "goals_paused": 1})),
        ("ctrl+.", Box::new(|_, _, _| {
            headless::press_with(&[Key::Control], ".");
            true
        })),
        says("verified agent", 1, "Stopped by Theo: Pebble busy with no process · 1 goal paused", out.clone(), "stop-01-verified-agent"),
        // ---- an admin token: the actor is the caller's claim; goals unknown
        working("claimed user working", json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": false,
            "stop_reason": "", "queue_paused": true})),
        stop_by_command("claimed user stop"),
        // Nothing waits: the transcript's "Stopped" line says it all.
        says("claimed user", 2, "", out.clone(), "stop-02-claimed-user"),
        // ---- a paired device: verified, and no goal was paused
        working("paired user working", json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true,
            "stop_reason": "", "queue_paused": true, "goals_paused": 0})),
        // Two messages wait: the line says the queue is paused, and only that.
        ("messages waiting", Box::new({
            let mut asked = false;
            move |app: &crate::App, _: &crate::AppWindow, _: Duration| {
            if !std::mem::replace(&mut asked, true) {
                let set = control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 2}}));
                check(set.is_ok(), &format!("two messages wait for Rachel: {set:?}"));
                return false;
            }
            app.engine.borrow().roster().find("rachel").is_some_and(|a| a.queued_turn_count == 2)
        }})),
        stop_by_command("paired user stop"),
        says("paired user", 3, "Queue paused", out.clone(), "stop-03-paired-user"),
        // ---- an older Host (contract 60): every newer field unknown
        working("older host working", json!({"ok": true, "terminated": 1})),
        stop_by_command("older host stop"),
        says("older host", 4, "Stopped · effects unknown", out.clone(), "stop-04-older-host"),
        // ---- another device or agent stopped it: the state's detail
        working("detail working", json!({"ok": true})),
        ("detail", Box::new(|_, _, _| {
            let detail = json!({"source": "user_stop", "message": "Turn stopped", "stop_actor": "agent:theo-97e5",
                "stop_actor_verified": true, "stop_reason": "Pebble busy with no process"});
            let sent = state("interrupted", Some(detail));
            check(sent.is_ok(), &format!("Theo stops Rachel from elsewhere: {sent:?}"));
            true
        })),
        ("detail line", Box::new(move |_, _, elapsed| {
            if notice().is_empty() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(posts("/stop").len() == 4, "this desktop sent no Stop");
            check(notice() == THEO, &format!("the chat names who stopped it and why: {:?}", notice()));
            shot(&out_detail, "stop-05-user-stop-detail");
            true
        })),
        // ---- the runtime refused: no line, only the error
        working("refused working", json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": true, "stop_reason": ""})),
        stop_by_command("refused stop"),
        ("refused", Box::new(move |app, window, elapsed| {
            let error = app.engine.borrow().error().to_owned();
            if posts("/stop").len() < 5 || error.is_empty() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(error == "Stop did not take effect", &format!("a refused Stop is only an error: {error:?}"));
            check(notice().is_empty(), &format!("and no line: {:?}", notice()));
            check(window.get_error() == "Stop did not take effect", &format!("the window says so: {:?}", window.get_error()));
            shot(&out_refused, "stop-06-runtime-refused");
            true
        })),
        // ---- the paused queue, as the composer and the explorer say it
        ("queue paused", Box::new(|_, _, _| {
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 2, "queue_paused": true}}));
            check(set.is_ok(), &format!("two messages wait in Rachel's paused queue: {set:?}"));
            true
        })),
        ("queue shown", Box::new(move |_, window, elapsed| {
            let pane = view();
            let row = window.get_chats().iter().find(|c| c.session == "rachel");
            if pane.queued != 2 || !pane.queue_paused || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(row.is_some_and(|r| r.queued == 2 && r.queue_paused), "the explorer marks Rachel's queue paused");
            true
        })),
        // The queued lines fade in (RevealText): the shot waits for them.
        ("queue drawn", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_secs(2) {
                return false;
            }
            shot(&out_queue, "stop-07-queue-paused");
            true
        })),
        // ---- the turn says it once: a partial answer, then "Stopped."
        ("turn tells it", Box::new(|app, _, _| {
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 0, "queue_paused": false}}))
                .and_then(|()| control("/__control/turn-reason", &json!({"on": true})))
                .and_then(|()| control("/__control/stop-text", &json!({"on": true})));
            check(set.is_ok(), &format!("the Host ends turns with a reason and tells a Stop in a word: {set:?}"));
            let turns = json!({"session": "rachel", "turns": [
                {"id": "s-u", "role": "user", "text": "Compare the three build hosts", "origin": "user", "trace_id": "s-t"},
                {"id": "s-a", "role": "assistant", "text": "The first host builds fastest, about 4 minutes for a clean build. The second", "phase": "final", "trace_id": "s-t"},
            ]});
            check(control("/__control/upsert", &turns).is_ok(), "Rachel has answered half way");
            app.engine.borrow_mut().reconnect();
            true
        })),
        working("partial working", json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true,
            "queue_paused": false, "goals_paused": 0, "text": "Stopped."})),
        ("partial answer", Box::new(|app, window, _| {
            let drawn = super::rows(window).iter().any(|r| r.id == "s-a");
            app.engine.borrow().turn_end_reason("rachel") && drawn
        })),
        stop_by_command("partial stop"),
        ("turn ends", Box::new(|_, _, elapsed| {
            // Until the turn's end row comes, the composer says it.
            if posts("/stop").len() < 6 || notice() != "Stopped." || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(8), &format!("before the turn's end row, the composer says Stopped.: {:?}", notice()));
                return elapsed >= Duration::from_secs(8);
            }
            let end = json!({"session": "rachel", "turns": [
                {"id": "s-a", "role": "assistant", "text": "The first host builds fastest, about 4 minutes for a clean build. The second", "phase": "final", "trace_id": "s-t",
                 "turn": {"turn_id": "s-t", "status": "interrupted", "started_at_ms": 1_000, "ended_at_ms": 5_000, "worked_ms": 4_000, "tool_count": 0, "reason": "Stopped."}},
            ]});
            check(control("/__control/upsert", &end).is_ok(), "the Host closes the stopped turn with its reason");
            check(state("interrupted", Some(json!({"source": "user_stop", "stop_actor": "user", "stop_text": "Stopped."}))).is_ok(), "and says the agent stopped");
            true
        })),
        ("said once", Box::new(move |app, window, elapsed| {
            let ids: Vec<String> = super::rows(window).iter().map(|r| r.id.to_string()).collect();
            let line = clarp_core::turn_reason::key("s-t");
            let drawn = ids.iter().any(|i| i == "s-a") && ids.iter().any(|i| *i == line);
            if !drawn || !notice().is_empty() || elapsed < Duration::from_millis(600) {
                check(elapsed < Duration::from_secs(8), &format!("the stopped turn keeps its answer and closes with its line: {ids:?}, composer {:?}", notice()));
                return elapsed >= Duration::from_secs(8);
            }
            let at = |id: &str| ids.iter().position(|i| i == id);
            check(at("s-a") < at(line.as_str()), &format!("the partial answer stays, the line under it: {ids:?}"));
            check(super::rows(window).iter().any(|r| r.id == line.as_str() && r.author == "system"), "the line is one quiet system line");
            check(notice().is_empty(), &format!("the composer does not say it again: {:?}", notice()));
            let receipt = app.engine.borrow().stop_notice("rachel");
            check(receipt == "Stopped.", &format!("the Stop's own line is still the Host's word, only not repeated: {receipt:?}"));
            check(!app.engine.borrow().roster().find("rachel").is_some_and(|a| a.busy), "the turn is finished");
            check(view().can_send, "and the next message sends at once");
            shot(&out_turn, "stop-08-said-on-the-turn");
            true
        })),
    ];
    run_stages(stages);
}
