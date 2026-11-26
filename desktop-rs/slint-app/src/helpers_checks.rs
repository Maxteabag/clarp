//! `--check helpers`: a turn's helpers are one card in the chat, never a
//! line of system text. The sub-agents a reply started read "1 helper
//! working · N s" with the time ticking, one line per helper (its task while
//! it runs, its result once done), and "2 helpers finished" once both are.
//! The agent's helper agents at work are one card at the chat's end, each
//! opening to its own chat; a helper's report reads "<helper> reported" and
//! opens to the helper's chat. The card goes when the helpers are done.

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, rows, run_stages, shot};

const AGENTS_CARD: &str = "helpers:agents:rachel";

fn row(id: &str) -> Option<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().find(|r| r.id == id)
}

fn card(id: &str) -> crate::HelpersCard {
    row(id).map(|r| r.helpers).unwrap_or_default()
}

fn title(card: &crate::HelpersCard) -> String {
    let now = crate::window().map_or(0, |w| w.global::<crate::ArtifactBridge>().get_now());
    let elapsed = if card.timed { (now - card.started).max(0) } else { -1 };
    let count = |n: i32| usize::try_from(n).unwrap_or(0);
    clarp_core::helpers_card::title(count(card.running), count(card.total), count(card.failed), i64::from(elapsed))
}

fn now_iso(back_secs: i64) -> String {
    (chrono::Utc::now() - chrono::Duration::seconds(back_secs)).to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

fn cells(second_done: bool) -> serde_json::Value {
    let (status, result) = if second_done { ("ok", "Three endpoints need a token") } else { ("running", "") };
    let mut second = vec![json!({"label": "Task", "text": "Read the API docs and list what needs a token"})];
    if second_done {
        second.push(json!({"label": "Result", "text": result}));
    }
    json!([
        {"id": "toolu_1", "kind": "subagents", "title": "Agent finished", "summary": "Map the auth flow", "status": "ok",
         "lines": [{"label": "Task", "text": "Find where tokens are checked"}, {"label": "Result", "text": "Tokens are checked in auth.rs"}]},
        {"id": "toolu_2", "kind": "subagents", "title": "Running agent", "summary": "Read the docs", "status": status, "lines": second},
    ])
}

fn turns(second_done: bool) -> serde_json::Value {
    json!({"session": "rachel", "turns": [
        {"id": "h-ask", "role": "user", "text": "Split the review up", "timestamp": now_iso(8)},
        {"id": "h-work", "role": "assistant", "text": "Two helpers take a part each.", "timestamp": now_iso(6), "display_cells": cells(second_done)},
        {"id": "h-report", "role": "user", "origin": "agent", "sender_name": "probe", "sender_agent_id": "h3", "sender_session": "probe",
         "text": "Done: three files changed\n\nDetails follow.", "timestamp": now_iso(2)},
        {"id": "h-reply", "role": "assistant", "text": "The probe is in; waiting on the other two.", "timestamp": now_iso(1)},
    ]})
}

fn helper(id: &str, session: &str, state: &str, busy: bool, status: &str) -> serde_json::Value {
    let started = chrono::Utc::now().timestamp_millis() - 5_000;
    json!({"agent_id": id, "session": session, "persona": "", "backend": "claude", "role": "helper", "parent_agent_id": "a1",
           "helper_state": state, "busy": busy, "turn_started_at": if busy { started } else { 0 }, "status_text": status,
           "conversation_id": format!("c-{session}")})
}

pub fn helpers_check(out: String) {
    let (out1, out2, out3) = (out.clone(), out.clone(), out);
    let mut opened = false;
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            if !opened {
                app.engine.borrow_mut().select("rachel");
                crate::pump();
                opened = true;
            }
            let held = {
                let engine = app.engine.borrow();
                !engine.log_pending("rachel") && engine.conversation("rachel").is_some_and(|c| c.latest_revision() >= 2 && !c.loading())
            };
            if !held || row("r2").is_none() {
                return false;
            }
            for body in [helper("h1", "slash-helper", "running", true, "Building the card"), helper("h2", "probe-two", "running", true, ""),
                helper("h3", "probe", "reported", false, "")]
            {
                check(control("/__control/add-agent", &body).is_ok(), "the Host takes a helper of Rachel's");
            }
            check(control("/__control/turns", &turns(false)).is_ok(), "the Host takes a turn that started two sub-agents");
            crate::pump();
            true
        })),
        ("cards", Box::new(move |_, _, elapsed| {
            let ready = card("h-work").total == 2 && card(AGENTS_CARD).total == 2 && row("h-report").is_some_and(|r| !r.prompt.helper.is_empty());
            if !ready {
                return elapsed > Duration::from_secs(8) && {
                    check(false, &format!("the cards show: turn {:?}, agents {:?}", card("h-work").total, card(AGENTS_CARD).total));
                    true
                };
            }
            let turn = card("h-work");
            check(turn.running == 1, "one sub-agent still runs");
            check(title(&turn).starts_with("1 helper working · "), &format!("the turn's card counts it and its time: {}", title(&turn)));
            let lines: Vec<(String, String, String)> = turn.helpers.iter().map(|h| (h.name.to_string(), h.state.to_string(), h.line.to_string())).collect();
            check(lines[0] == ("Map the auth flow".into(), "done".into(), "Tokens are checked in auth.rs".into()), &format!("a finished helper folds to its result: {:?}", lines[0]));
            check(lines[1].1 == "running" && lines[1].2.starts_with("Read the API docs"), &format!("a running one shows its task: {:?}", lines[1]));
            check(row("h-work").is_some_and(|r| r.cells.row_count() == 0), "the sub-agents are not also tool cells");
            let agents = card(AGENTS_CARD);
            let chats: Vec<String> = agents.helpers.iter().map(|h| h.chat.to_string()).collect();
            check(chats == ["slash-helper", "probe-two"], &format!("the helper agents at work are one card, each with its chat: {chats:?}"));
            check(agents.helpers.iter().next().is_some_and(|h| h.line == "Building the card"), "a helper shows its own status line");
            check(title(&agents).starts_with("2 helpers working · "), &format!("{}", title(&agents)));
            let last = crate::window().map(|w| rows(&w)).unwrap_or_default().last().map(|r| r.id.to_string()).unwrap_or_default();
            check(last == AGENTS_CARD, &format!("at the chat's end: {last}"));
            let report = row("h-report").map(|r| r.prompt).unwrap_or_default();
            check(report.helper == "probe" && report.line == "Done: three files changed", &format!("a helper's report folds to one line, opening its chat: {report:?}"));
            check(crate::window().map(|w| rows(&w)).unwrap_or_default().iter().all(|r| r.author != "system"), "no helper is a line of system text");
            shot(&out1, "helpers-01-working");
            true
        })),
        ("ticking", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(2200) {
                return false;
            }
            let turn = card("h-work");
            let now = crate::window().map_or(0, |w| w.global::<crate::ArtifactBridge>().get_now());
            check(turn.timed && now - turn.started >= 7, &format!("the time ticks on: {}", title(&turn)));
            if let Some(window) = crate::window() {
                window.global::<crate::HelpersBridge>().invoke_open_chat("slash-helper".into());
            }
            true
        })),
        ("helper chat", Box::new(|app, _, elapsed| {
            if app.engine.borrow().selected_session() != "slash-helper" {
                return elapsed > Duration::from_secs(3) && {
                    check(false, "a helper opens to its chat");
                    true
                };
            }
            check(true, "a helper opens to its chat");
            app.engine.borrow_mut().select("rachel");
            for (session, state) in [("slash-helper", "done"), ("probe-two", "done")] {
                let _ = control("/__control/agent", &json!({"session": session, "set": {"helper_state": state, "busy": false, "turn_started_at": 0}}));
            }
            let _ = control("/__control/turns", &turns(true));
            crate::pump();
            true
        })),
        ("finished", Box::new(move |_, _, elapsed| {
            let done = card("h-work").running == 0 && card("h-work").total == 2 && row(AGENTS_CARD).is_none();
            if !done {
                return elapsed > Duration::from_secs(8) && {
                    check(false, &format!("the helpers finish: turn running {}, agents card {}", card("h-work").running, row(AGENTS_CARD).is_some()));
                    true
                };
            }
            let turn = card("h-work");
            check(title(&turn) == "2 helpers finished", &format!("the turn's card says they finished: {}", title(&turn)));
            check(turn.helpers.iter().nth(1).is_some_and(|h| h.line == "Three endpoints need a token"), "each folded to its result");
            check(true, "the helper agents' card goes once they are done");
            shot(&out2, "helpers-02-finished");
            app_now().engine.borrow_mut().set_reading_theme("paper");
            crate::pump();
            true
        })),
        ("light", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(1200) {
                return false;
            }
            shot(&out3, "helpers-03-finished-light");
            true
        })),
    ];
    run_stages(stages);
}
