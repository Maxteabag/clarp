//! `--check composer-buttons`: the chat's action buttons do what their keys
//! do. Send shows only with something to send and sends it, as Enter does;
//! Stop shows while the agent works and stops it, as Ctrl+. does; scrolled
//! up from the end a jump-to-latest button floats over the chat (no row
//! moves when it comes), a dot on it when a message arrives below while
//! the reader is up, which stays while a reply streams and after it
//! finished below, and it brings the chat back to its end, where both go.
//! Stop says its word beside its icon in the composer's one line. A draft
//! that grows the composer to several lines leaves the window's scale and
//! size as they were. During a turn the strip above the composer lists
//! each waiting message with Send now; a Send now the Host refuses says the
//! Host's answer and the message waits on; Ctrl+Shift+Enter sends the
//! first. The composer's frame shows the context meter, and warns near the
//! account's limit. With more than two waiting the strip is one "N waiting"
//! line with Manage and Send now; Hide queue (Ctrl+K) leaves no strip and
//! Show queue brings it back.
//! Needs the fake Host.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::Model;

use super::{Stage, app_now, check, check_window_size_free, control, posts, report, run_stages, sends, shot, view};
use crate::headless;

fn element(id: &str) -> Option<i_slint_backend_testing::ElementHandle> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
}

fn drawn(id: &str) -> bool {
    element(id).is_some()
}

/// Presses the element as a screen reader or a click would.
fn press(id: &str) -> bool {
    element(id).map(|e| e.invoke_accessible_default_action()).is_some()
}

/// The open chat's box in the window: (x, y, width, height).
fn chat() -> Option<(f32, f32, f32, f32)> {
    let e = element(&format!("chat:{}", app_now().active_id()))?;
    let (at, size) = (e.absolute_position(), e.size());
    Some((at.x, at.y, size.width, size.height))
}

fn jump_id() -> String {
    format!("jump-latest:{}", app_now().active_id())
}

fn news_id() -> String {
    format!("jump-news:{}", app_now().active_id())
}

/// Whether the open pane says there is news below.
fn view_news() -> bool {
    app_now().active_view().is_some_and(|v| v.news)
}

/// `count` turns, alternately the user's and the agent's.
fn long_chat(count: usize) -> Vec<serde_json::Value> {
    (0..count)
        .map(|i| json!({"id": format!("long-{i}"), "role": if i % 2 == 0 { "user" } else { "assistant" },
            "text": format!("Line {i}: a note long enough to fill some of the chat's height on its own.")}))
        .collect()
}

/// The agent's reply below the long chat, `parts` paragraphs long.
fn reply(parts: usize) -> serde_json::Value {
    let text: Vec<String> = (0..parts).map(|i| format!("Part {i}: the reply goes on below the reader, a paragraph at a time, long enough to wrap.")).collect();
    json!({"id": "reply", "role": "assistant", "text": text.join("\n\n")})
}

/// A row's text in the chat as the engine holds it ("" before it arrives).
fn conversation_text(app: &crate::App, id: &str) -> String {
    let engine = app.engine.borrow();
    let Some(conversation) = engine.conversation("rachel") else { return String::new() };
    conversation.index_of(id).and_then(|at| conversation.rows().get(at)).map(|row| row.text.clone()).unwrap_or_default()
}

thread_local! {
    /// The composer's editor, one line high (empty).
    static ONE_LINE: Cell<f32> = const { Cell::new(0.0) };
}

/// A draft that wraps the composer onto several lines.
const LONG_DRAFT: &str = "sent by the button, a draft long enough to wrap the composer onto several lines: token bucket, leaky bucket, sliding window log, sliding window counter, clock skew, race conditions, failure modes, observability and testing, with an example in every section.";

pub fn buttons_check(out: String) {
    let (out1, out2, out3, out4, out5, out6, out7, out8) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let (out9, out10) = (out.clone(), out);
    let box_at_end: Rc<Cell<Option<(f32, f32, f32, f32)>>> = Rc::new(Cell::new(None));
    let (end1, end2, end3) = (box_at_end.clone(), box_at_end.clone(), box_at_end);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, elapsed| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open || !report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!drawn("composer-send"), "an empty composer shows no Send button");
            check(!drawn("composer-stop"), "an idle agent shows no Stop button");
            check_window_size_free(window, "with the composer one line high");
            ONE_LINE.with(|line| line.set(element("composer-editor").map_or(0.0, |e| e.size().height)));
            headless::type_text(LONG_DRAFT);
            true
        })),
        ("send shown", Box::new(move |_, window, elapsed| {
            if app_now().active_draft() != LONG_DRAFT || elapsed < Duration::from_millis(200) {
                return false;
            }
            // The composer grew to several lines and the window kept its
            // scale and size (#852).
            let (one, now) = (ONE_LINE.with(Cell::get), element("composer-editor").map_or(0.0, |e| e.size().height));
            check(one > 0.0 && now >= one * 1.8, &format!("the long draft grows the composer to several lines: {now} tall, one line {one}"));
            check_window_size_free(window, "with the composer grown to several lines");
            check(drawn("composer-send"), "with text, the composer shows Send");
            shot(&out1, "buttons-01-send");
            check(press("composer-send"), "Send is pressed");
            true
        })),
        ("sent", Box::new(|_, window, elapsed| {
            let sent = sends().iter().any(|s| s["body"]["text"].as_str().is_some_and(|t| t.contains("sent by the button")));
            if !sent {
                check(elapsed < Duration::from_secs(5), "Send sends the text, as Enter does");
                return elapsed >= Duration::from_secs(5);
            }
            check(app_now().active_draft().is_empty(), "and empties the composer");
            check_window_size_free(window, "after a send");
            let set = control("/__control/stop", &json!({"respond": {"ok": true, "terminated": 1}}))
                .and_then(|()| control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "thinking", "busy": true}})));
            check(set.is_ok(), &format!("Rachel works: {set:?}"));
            true
        })),
        ("stop shown", Box::new(move |_, _, elapsed| {
            if !drawn("composer-stop") {
                check(elapsed < Duration::from_secs(5), "while the agent works the composer shows Stop");
                return elapsed >= Duration::from_secs(5);
            }
            // It says Stop in words, in the composer's one line.
            let stop = element("composer-stop").map(|e| e.size()).unwrap_or_default();
            let editor = element("composer-editor").map(|e| e.size()).unwrap_or_default();
            check(stop.width >= stop.height * 2.0, &format!("Stop shows its word beside the icon: {}x{}", stop.width, stop.height));
            check(stop.height <= editor.height + 0.5, &format!("and keeps the composer one line high: Stop {} tall, the line {}", stop.height, editor.height));
            shot(&out2, "buttons-02-stop");
            check(press("composer-stop"), "Stop is pressed");
            true
        })),
        ("stopped", Box::new(|_, _, elapsed| {
            let stops = posts("/stop");
            if stops.is_empty() {
                check(elapsed < Duration::from_secs(5), "Stop stops the agent, as Ctrl+. does");
                return elapsed >= Duration::from_secs(5);
            }
            check(stops[0]["body"]["session"] == "rachel", "the Stop is for Rachel");
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "idle", "busy": false}}));
            check(set.is_ok(), "Rachel is idle again");
            // A long chat to scroll in.
            check(control("/__control/turns", &json!({"session": "rachel", "turns": long_chat(40)})).is_ok(), "the Host holds a long chat");
            headless::press(slint::platform::Key::F5);
            true
        })),
        ("long", Box::new(move |app, _, elapsed| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| c.index_of("long-39").is_some() && !c.loading());
            if !loaded || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(report().at_end, "the chat is at its end");
            check(!drawn(&jump_id()), "at the end there is no jump button");
            let Some((x, y, w, h)) = chat() else { return false };
            end1.set(Some((x, y, w, h)));
            for _ in 0..8 {
                headless::wheel(x + w / 2.0, y + h / 2.0, 120.0);
            }
            true
        })),
        ("scrolled up", Box::new(move |_, _, elapsed| {
            if report().at_end || elapsed < Duration::from_millis(800) {
                if elapsed >= Duration::from_secs(5) {
                    check(false, "the wheel scrolls the chat up from its end");
                    return true;
                }
                return false;
            }
            check(drawn(&jump_id()), "scrolled up, a jump-to-latest button shows");
            check(chat() == end2.get(), &format!("it floats: the chat keeps its place and size: {:?} then {:?}", end2.get(), chat()));
            check(!drawn(&news_id()), "with nothing new below, it shows no news dot");
            // A new message arrives below while the reader is up.
            check(control("/__control/turns", &json!({"session": "rachel", "turns": long_chat(41)})).is_ok(), "the Host adds a message");
            true
        })),
        ("news", Box::new(move |app, _, elapsed| {
            let arrived = app.engine.borrow().conversation("rachel").is_some_and(|c| c.index_of("long-40").is_some());
            if !arrived || !drawn(&news_id()) {
                check(elapsed < Duration::from_secs(5), &format!("a message arriving while scrolled up puts a dot on the jump button (arrived {arrived})"));
                return elapsed >= Duration::from_secs(5);
            }
            check(!report().at_end, "the reader stays where they were");
            check(chat() == end3.get(), "the dot moves no row: the chat keeps its place and size");
            // A reply starts below while the reader is still up.
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "thinking", "busy": true}}))
                .and_then(|()| control("/__control/upsert", &json!({"session": "rachel", "turns": [reply(1)]})));
            check(set.is_ok(), &format!("Rachel starts a reply: {set:?}"));
            true
        })),
        ("streaming", Box::new(|app, _, elapsed| {
            if !app.engine.borrow().conversation("rachel").is_some_and(|c| c.index_of("reply").is_some()) || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(5), "the reply's first words arrive");
                return elapsed >= Duration::from_secs(5);
            }
            check(control("/__control/upsert", &json!({"session": "rachel", "turns": [reply(6)]})).is_ok(), "the reply grows");
            true
        })),
        ("finishing", Box::new(|app, _, elapsed| {
            if !conversation_text(app, "reply").contains("Part 5") || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(5), "the reply grows below");
                return elapsed >= Duration::from_secs(5);
            }
            check(view_news(), "while it streams below, the news stays");
            // It finishes: the agent is idle and the reply settles at its full length.
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "idle", "busy": false}}))
                .and_then(|()| control("/__control/upsert", &json!({"session": "rachel", "turns": [reply(12)]})));
            check(set.is_ok(), &format!("the reply finishes: {set:?}"));
            true
        })),
        ("finished", Box::new(move |app, _, elapsed| {
            let settled = conversation_text(app, "reply").contains("Part 11");
            if !settled {
                let busy = app.engine.borrow().roster().find("rachel").is_some_and(|a| a.busy);
                check(elapsed < Duration::from_secs(5), &format!("the finished reply settles at its full length (busy {busy}, {} chars)", conversation_text(app, "reply").len()));
                return elapsed >= Duration::from_secs(5);
            }
            // Seconds after it finished, the reader still up: the dot stays.
            if elapsed < Duration::from_secs(3) {
                return false;
            }
            check(!report().at_end && !report().follows, "the reader is still up");
            check(drawn(&news_id()) && view_news(), "3 s after a reply below finished, the jump button still shows the dot");
            // A frame in which the chat's end shows without the reader
            // going there (rows handed over or measured again below) is
            // not the reader back at the end: the news stays.
            let (real, pane) = (report(), app_now().active_id());
            if let Some(window) = crate::window() {
                window.invoke_pane_reported(pane.clone().into(), false, true, real.offset, real.transcript_focused, real.composer_focused, real.top);
                check(view_news(), "the end showing for a frame, the reader not following it, leaves the news");
                window.invoke_pane_reported(pane.into(), real.follows, real.at_end, real.offset, real.transcript_focused, real.composer_focused, real.top);
            }
            shot(&out3, "buttons-03-jump");
            check(press(&jump_id()), "the jump button is pressed");
            true
        })),
        ("at the end", Box::new(|_, _, elapsed| {
            if !report().at_end {
                check(elapsed < Duration::from_secs(5), "the jump button brings the chat to its end, as Ctrl+End does");
                return elapsed >= Duration::from_secs(5);
            }
            if elapsed < Duration::from_millis(600) {
                return false;
            }
            check(!drawn(&jump_id()), "and goes at the end");
            check(!drawn(&news_id()) && !view_news(), "and the news is read");
            // A turn runs with two messages waiting behind it, and the Host
            // knows the conversation's context.
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "thinking", "busy": true,
                "queued_turn_count": 2, "context_tokens": 21_400, "context_window": 1_000_000}}));
            check(set.is_ok(), &format!("Rachel works with two messages waiting: {set:?}"));
            true
        })),
        ("queue strip", Box::new(move |_, window, elapsed| {
            if !drawn("queue-send:q1") || !drawn("queue-send:q2") || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(5), "during the turn the strip above the composer lists each waiting message with Send now");
                return elapsed >= Duration::from_secs(5);
            }
            let pane = app_now().active_id();
            check(drawn(&format!("queue-strip:{pane}")), "the strip is the open chat's");
            check(label(&format!("composer-meter:{pane}")) == "context 21K/1M · 2%", &format!("the composer's frame shows the context meter: {:?}", label(&format!("composer-meter:{pane}"))));
            check(!view().meter_warn, "2% of the context is no warning");
            check_window_size_free(window, "with the queue strip shown");
            shot(&out4, "buttons-04-queue-strip");
            // The Host refuses this one: a turn still runs.
            check(control("/__control/fail", &json!({"path": "/turn-queue/q1/send", "status": 409})).is_ok(), "the Host will refuse the next Send now");
            check(press("queue-send:q1"), "Send now is pressed on the first waiting message");
            true
        })),
        ("refused", Box::new(move |_, _, elapsed| {
            let pane = app_now().active_id();
            let refused = format!("queue-refused:{pane}");
            if !drawn(&refused) || elapsed < Duration::from_millis(500) {
                if elapsed >= Duration::from_secs(5) {
                    let engine = app_now().engine.borrow().turn_queue_refusal("rachel");
                    check(false, &format!("a refused Send now says the Host's answer under the strip (pane {:?}, engine {engine:?}, waiting {})", view().waiting_refused, view().waiting.row_count()));
                    shot(&out5, "buttons-05-send-refused");
                    return true;
                }
                return false;
            }
            check(label(&refused) == "agent is still working", &format!("in the Host's words: {:?}", label(&refused)));
            check(drawn("queue-send:q1"), "and the message waits on, still listed after the queue reloaded");
            shot(&out5, "buttons-05-send-refused");
            // Near the account's limit and deep in the context: the meter warns.
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"context_tokens": 850_000,
                "backend_quota": {"state": "near", "provider_id": "claude", "reason": "usage_limit", "window": "five_hour",
                    "used_percentage": 91.0, "resets_at": null, "failover": "none"}}}));
            check(set.is_ok(), "the Host says Claude is near its limit");
            // By key now: Ctrl+Shift+Enter sends the first waiting message.
            app_now().focus_composer();
            headless::press_with(&[slint::platform::Key::Control, slint::platform::Key::Shift], slint::platform::Key::Return);
            true
        })),
        ("sent now", Box::new(move |_, _, elapsed| {
            let pane = app_now().active_id();
            let sent = requests_posted("/turn-queue/q1/send") >= 2;
            if !sent || drawn("queue-send:q1") || !view().meter_warn {
                check(elapsed < Duration::from_secs(5), &format!("Ctrl+Shift+Enter sends the first waiting message now (sent {sent}) and the meter warns ({})", view().meter_warn));
                return elapsed >= Duration::from_secs(5);
            }
            check(drawn("queue-send:q2"), "the other one still waits");
            check(!drawn(&format!("queue-refused:{pane}")), "the refusal is answered by the send");
            let meter = label(&format!("composer-meter:{pane}"));
            check(meter == "Claude 91% used  ·  context 850K/1M · 85%", &format!("the meter names the limit and the context: {meter:?}"));
            shot(&out6, "buttons-06-meter-near");
            check(!app_now().active_draft().contains('\n'), "the key did not put a line in the draft");
            // A Host that offers Steer now (queue_steer).
            check(control("/__control/queue-steer", &json!({"on": true})).is_ok(), "the Host offers Steer now");
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("steer offered", Box::new(move |_, _, elapsed| {
            if !drawn("queue-steer:q2") || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(8), "on a Host with queue_steer a waiting message offers Steer now during the turn");
                return elapsed >= Duration::from_secs(8);
            }
            check(drawn("queue-send:q2"), "with Send now (stop the turn, run it now) beside it");
            shot(&out7, "buttons-07-steer-now");
            check(control("/__control/fail", &json!({"path": "/turn-queue/q2/steer", "status": 409})).is_ok(), "the Host will refuse the steer");
            check(press("queue-steer:q2"), "Steer now is pressed");
            true
        })),
        ("steer refused", Box::new(|_, _, elapsed| {
            let refused = format!("queue-refused:{}", app_now().active_id());
            if !drawn(&refused) {
                check(elapsed < Duration::from_secs(5), "a refused steer says the Host's words");
                return elapsed >= Duration::from_secs(5);
            }
            check(label(&refused) == "the running turn cannot be cut", &format!("plainly: {:?}", label(&refused)));
            check(!drawn("queue-steer:q2") && drawn("queue-send:q2"), "and the message still waits, offering Stop and send instead of Steer now");
            check(press("queue-send:q2"), "Send now (take over) is pressed");
            true
        })),
        ("taken over", Box::new(|_, _, elapsed| {
            let taken = requests_posted("/turn-queue/q2/take-over") == 1;
            if !taken || drawn("queue-send:q2") {
                check(elapsed < Duration::from_secs(5), &format!("Send now on a steering Host takes the turn over (posted {taken})"));
                return elapsed >= Duration::from_secs(5);
            }
            check(requests_posted("/turn-queue/q2/send") == 0, "not the plain send");
            // Three wait now: the strip folds to one line.
            let items = json!([{"queue_id": "q3", "text": "third"}, {"queue_id": "q4", "text": "fourth"}, {"queue_id": "q5", "text": "fifth"}]);
            let set = control("/__control/turn-queue", &json!({"session": "rachel", "items": items}))
                .and_then(|()| control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 3}})));
            check(set.is_ok(), &format!("three messages wait: {set:?}"));
            true
        })),
        ("folded", Box::new(move |_, window, elapsed| {
            let folded = format!("queue-folded:{}", app_now().active_id());
            if !drawn(&folded) || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(5), "with more than two waiting the strip is one 'N waiting' line");
                return elapsed >= Duration::from_secs(5);
            }
            check(label(&folded).starts_with("3 waiting"), &format!("it says how many: {:?}", label(&folded)));
            check(!drawn("queue-item:q3") && !drawn("queue-item:q4") && !drawn("queue-item:q5"), "no row for each message");
            check(drawn(&format!("queue-manage:{}", app_now().active_id())), "Manage stays on the line");
            check(drawn("queue-send:q3"), "and Send now for the first");
            check_window_size_free(window, "with the strip folded");
            check(crate::queue_strip::shown(&app_now().engine.borrow()), "the queue shows by default");
            true
        })),
        // The saved frame is the last one drawn: the folded line is drawn
        // by now, then Hide queue (Ctrl+K) takes the strip away.
        ("folded shot", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            shot(&out8, "buttons-08-queue-folded");
            crate::commands::run(&app_now(), window, "toggle-queue");
            true
        })),
        ("hidden", Box::new(|app, window, elapsed| {
            let strip = format!("queue-strip:{}", app_now().active_id());
            if crate::queue_strip::shown(&app.engine.borrow()) || drawn(&strip) {
                check(elapsed < Duration::from_secs(3), "Hide queue leaves no strip at all");
                return elapsed >= Duration::from_secs(3);
            }
            check(app.engine.borrow().queue_count("rachel") == 3, "the messages still wait");
            crate::commands::run(&app_now(), window, "toggle-queue");
            true
        })),
        ("shown again", Box::new(|app, _, elapsed| {
            let folded = format!("queue-folded:{}", app_now().active_id());
            if !drawn(&folded) {
                check(elapsed < Duration::from_secs(3), "Show queue brings the strip back");
                return elapsed >= Duration::from_secs(3);
            }
            check(crate::queue_strip::shown(&app.engine.borrow()), "the setting is on again");
            // A Host that lets Send steer while the agent works (`send_while_busy`, #920).
            let set = control("/__control/send-while-busy", &json!({"on": true}))
                .and_then(|()| control("/__control/turn-queue", &json!({"session": "rachel", "items": [{"queue_id": "q6", "text": "use the other file", "kind": "waiting", "origin": "user"}]})))
                .and_then(|()| control("/__control/agent", &json!({"session": "rachel", "set": {"latest_state": "thinking", "busy": true, "queued_turn_count": 1}})));
            check(set.is_ok(), &format!("the Host offers Send while working, one message waits: {set:?}"));
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("send chip", Box::new(move |_, _, elapsed| {
            let pane = app_now().active_id();
            let act = format!("send-chip-act:{pane}");
            if !drawn(&act) || elapsed < Duration::from_millis(400) {
                check(elapsed < Duration::from_secs(8), "while the agent works the chip above the message field says what waits, with Steer now");
                return elapsed >= Duration::from_secs(8);
            }
            check(label(&format!("send-chip:{pane}")) == "1 waiting", &format!("the chip counts it: {:?}", label(&format!("send-chip:{pane}"))));
            check(label(&act) == "Steer now", &format!("and offers Steer now: {:?}", label(&act)));
            check(!drawn("queue-send:q6"), "the queue strip stays away while the chip says what waits");
            shot(&out9, "buttons-09-send-chip");
            check(press(&act), "Steer now on the chip is pressed");
            true
        })),
        ("chip steered", Box::new(|_, _, elapsed| {
            if requests_posted("/turn-queue/q6/steer") != 1 {
                check(elapsed < Duration::from_secs(5), "the chip's Steer now reads the message into the turn");
                return elapsed >= Duration::from_secs(5);
            }
            let set = control(
                "/__control/send-while-busy",
                &json!({"fallback": {"from": "steer", "to": "take_over", "reason": "not_steerable", "text": "This agent can't take a message mid-turn: stopped and sent"}}),
            );
            check(set.is_ok(), "the Host will fall back on the next send");
            check(app_now().engine.borrow_mut().send_composer("rachel", "one more thing", false), "a message is sent");
            true
        })),
        ("chip says the fallback", Box::new(move |_, _, elapsed| {
            let pane = app_now().active_id();
            let notice = format!("send-chip-notice:{pane}");
            if !drawn(&notice) {
                check(elapsed < Duration::from_secs(5), "the chip shows what the Host did instead");
                return elapsed >= Duration::from_secs(5);
            }
            let sent = sends().last().cloned().unwrap_or_default();
            check(sent["body"]["while_busy"] == "steer", &format!("the send carried the setting: {:?}", sent["body"]["while_busy"]));
            check(label(&notice) == "This agent can't take a message mid-turn: stopped and sent", &format!("in the Host's words: {:?}", label(&notice)));
            shot(&out10, "buttons-10-send-chip-fallback");
            true
        })),
        ("chip notice goes", Box::new(|_, _, elapsed| {
            let notice = format!("send-chip-notice:{}", app_now().active_id());
            if drawn(&notice) {
                check(elapsed < Duration::from_secs(8), "the Host's line goes after a few seconds");
                return elapsed >= Duration::from_secs(8);
            }
            true
        })),
    ];
    run_stages(stages);
}

/// An element's accessible label ("" when not drawn).
fn label(id: &str) -> String {
    element(id).and_then(|e| e.accessible_label()).map(|l| l.to_string()).unwrap_or_default()
}

/// How many POSTs to `path` the Host has had (refused ones too).
fn requests_posted(path: &str) -> usize {
    posts(path).len()
}
