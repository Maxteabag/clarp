//! `--check artifacts --out DIR`: every artifact type is embedded in the
//! chat under the reply it was made in, shows what the iOS card shows and
//! does what it offers. Each type's stages load a chat of that type's
//! fixtures from the fake Host (`/__control/artifact-chat`) and check the
//! card's model fields and the interaction; each takes a screenshot.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model};

use super::{Stage, check, control, rows, run_stages, shot};
use crate::{ArtifactBridge, ArtifactItem};

/// Every card in the open chat, in transcript order, with its row's id.
fn cards(window: &crate::AppWindow) -> Vec<(String, ArtifactItem)> {
    rows(window).iter().flat_map(|row| row.artifacts.iter().map(|a| (row.id.to_string(), a)).collect::<Vec<_>>()).collect()
}

fn card(window: &crate::AppWindow, id: &str) -> Option<ArtifactItem> {
    cards(window).into_iter().map(|(_, a)| a).find(|a| a.id == id)
}

/// The fixture artifact as the Host listed it.
fn fixture(app: &crate::App, id: &str) -> Value {
    app.engine.borrow().update_artifacts().iter().find(|a| a["artifact_id"] == id).cloned().unwrap_or(Value::Null)
}

/// A stage that gives Rachel a chat of `types`' fixtures and opens it.
fn load_chat(types: &'static [&'static str]) -> Stage {
    ("load", Box::new(move |app, _, _| {
        if app.engine.borrow().connection_state() != "live" {
            return false;
        }
        let loaded = control("/__control/artifact-chat", &json!({"session": "rachel", "types": types}));
        check(loaded.is_ok(), &format!("the Host takes a chat of {types:?} artifacts {}", loaded.err().unwrap_or_default()));
        app.engine.borrow_mut().select("rachel");
        crate::pump();
        true
    }))
}

/// Waits until every id shows as a card and the chat has settled.
fn placed(window: &crate::AppWindow, ids: &[&str], elapsed: Duration) -> bool {
    elapsed >= Duration::from_millis(400) && ids.iter().all(|id| card(window, id).is_some())
}

// ---- countdown

fn countdown_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let first: Rc<RefCell<(i32, String)>> = Rc::default();
    let first2 = first.clone();
    let ticks = Rc::new(Cell::new(0));
    vec![
        load_chat(&["countdown"]),
        ("countdown cards", Box::new(move |app, window, elapsed| {
            if !placed(window, &["cd-launch", "cd-reached", "cd-past", "cd-cancelled", "cd-broken"], elapsed) {
                return false;
            }
            let bridge = window.global::<ArtifactBridge>();
            let now = bridge.get_now();
            let launch = card(window, "cd-launch").expect("launch");
            let placed_under = cards(window).iter().find(|(_, a)| a.id == "cd-launch").map(|(row, _)| row.clone()).unwrap_or_default();
            check(placed_under == "made-cd-launch", &format!("a countdown sits under the reply it was made in: {placed_under}"));
            check(launch.kind == "countdown" && launch.label == "COUNTDOWN" && launch.badge.is_empty(), &format!("it is labelled a countdown, no badge while active: {:?} {:?}", launch.label, launch.badge));
            check(launch.summary == "Freeze starts an hour before.", "it shows its summary");
            // The target's own date and time (it carries its offset), and its zone.
            let target = fixture(app, "cd-launch")["target_at"].as_str().unwrap_or_default().to_owned();
            let expected = chrono::DateTime::parse_from_rfc3339(&target).map(|t| format!("{} · Europe/Oslo", t.format("%b %-d, %Y at %H:%M"))).unwrap_or_default();
            check(launch.countdown_set && launch.countdown_line == expected.as_str(), &format!("the target line: {:?} (want {expected:?})", launch.countdown_line));
            let phase = bridge.invoke_countdown_phase(launch.countdown_at, now);
            let clock = bridge.invoke_countdown_clock(launch.countdown_at, now);
            check(phase == "Remaining" && clock.starts_with("1d 02:03:0"), &format!("a day and two hours ahead: {phase} {clock}"));
            let reached = card(window, "cd-reached").expect("reached");
            let (phase, clock) = (bridge.invoke_countdown_phase(reached.countdown_at, now), bridge.invoke_countdown_clock(reached.countdown_at, now));
            check(reached.countdown_set && phase == "Target reached" && clock == "Now", &format!("within a minute after the target: {phase} {clock}"));
            let past = card(window, "cd-past").expect("past");
            let (phase, clock) = (bridge.invoke_countdown_phase(past.countdown_at, now), bridge.invoke_countdown_clock(past.countdown_at, now));
            check(past.countdown_set && phase == "Since target" && clock.starts_with("02:05:"), &format!("two hours after: {phase} {clock}"));
            let cancelled = card(window, "cd-cancelled").expect("cancelled");
            check(cancelled.badge == "Cancelled" && cancelled.countdown_note == "Cancelled" && !cancelled.countdown_set, &format!("a cancelled one says so and has no clock: {:?} {:?}", cancelled.badge, cancelled.countdown_note));
            let broken = card(window, "cd-broken").expect("broken");
            check(!broken.countdown_set && broken.countdown_note == "Countdown unavailable", &format!("an unreadable target: {:?}", broken.countdown_note));
            *first.borrow_mut() = (now, bridge.invoke_countdown_clock(launch.countdown_at, now).to_string());
            true
        })),
        ("countdown ticks", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ArtifactBridge>();
            let (then, clock) = first2.borrow().clone();
            let now = bridge.get_now();
            if now <= then {
                ticks.set(ticks.get() + 1);
                // Two seconds and no tick: the check fails below.
                if elapsed < Duration::from_millis(2000) {
                    return false;
                }
            }
            let launch = card(window, "cd-launch").expect("launch");
            let later = bridge.invoke_countdown_clock(launch.countdown_at, now);
            check(now > then && later.as_str() != clock, &format!("the clock ticks: {clock} then {later} (now {then} then {now})"));
            shot(&out, "artifacts-01-countdown");
            true
        })),
    ]
}

pub(super) fn artifacts_check(out: String) {
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(countdown_stages(&out));
    run_stages(stages);
}
