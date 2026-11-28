//! `--check turn-errors` (docs/turn-errors.md, Rust Host contract 217): the
//! contract fixture's failure rows (`tests/fixtures/turn-errors.json`) draw
//! as cards in Norwegian. Three equal invalid_request failures with the
//! user's messages between them are one card with "3×" at the newest, only
//! the chat's newest failure offers a retry, Details opens with the raw
//! words, a retry posts `/turn-retry` and shows itself done, Copy fills the
//! clipboard, and the live failed turn shows one card until its notice lands.

use std::time::Duration;

use serde_json::{Value, json};

use super::{Stage, app_now, check, control, posts, run_stages, shot};

const SESSION: &str = "rachel";

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../tests/fixtures/turn-errors.json")).expect("the fixture is JSON")
}

/// The fixture's `/log` rows through the one whose id is `last`.
fn rows_through(last: &str) -> Vec<Value> {
    let rows = fixture()["log_rows"].as_array().cloned().unwrap_or_default();
    let end = rows.iter().position(|r| r["id"] == last).map_or(rows.len(), |i| i + 1);
    rows[..end].to_vec()
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

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

fn label(id: &str) -> String {
    element(id).and_then(|e| e.accessible_label()).map(|l| l.to_string()).unwrap_or_default()
}

fn press(id: &str) -> bool {
    element(id).map(|e| e.invoke_accessible_default_action()).is_some()
}

fn shown_ids() -> Vec<String> {
    crate::window().map(|w| super::rows(&w).iter().map(|r| r.id.to_string()).collect()).unwrap_or_default()
}

fn has_row(id: &str) -> bool {
    shown_ids().iter().any(|i| i == id)
}

/// Whether card `card`'s button `action` is drawn done (its check).
fn action_done(card: &str, action: &str) -> bool {
    use slint::Model;
    let Some(window) = crate::window() else { return false };
    super::rows(&window).iter().find(|r| r.id == card).is_some_and(|r| r.error.actions.iter().any(|a| a.id == action && a.done))
}

/// Replaces Rachel's history with `rows`.
fn load(rows: Vec<Value>) -> bool {
    control("/__control/turns", &json!({"session": SESSION, "turns": rows})).is_ok()
}

pub fn turn_errors_check(out: String) {
    let (o1, o2, o3, o4, o5, o6) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    crate::turn_error_view::set_norwegian(true);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            if app.engine.borrow().selected_session() != SESSION {
                app.engine.borrow_mut().select(SESSION);
            }
            app.engine.borrow().conversation(SESSION).is_some_and(|c| !c.rows().is_empty())
        })),
        ("three failures", Box::new(|_, _, _| {
            check(load(rows_through("marker-u3")), "Rachel's history is the fixture's first three failures");
            true
        })),
        ("one card for three", Box::new(move |_, _, elapsed| {
            if !drawn("error-card:marker-u3") && elapsed < Duration::from_secs(8) {
                return false;
            }
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            shot(&o1, "turn-errors-01-collapsed");
            check(drawn("error-card:marker-u3"), "the newest invalid_request failure is a card");
            check(label("error-count:marker-u3") == "3×", &format!("it counts three: {:?}", label("error-count:marker-u3")));
            check(!has_row("marker-u1") && !has_row("marker-u2"), &format!("the older two are hidden: {:?}", shown_ids()));
            check(has_row("u1") && has_row("u2") && has_row("u3"), "the user's messages stay");
            check(has_row("q1"), "the Host's other notice stays as it was");
            let sentence = label("error-card:marker-u3");
            check(sentence == "Codex avviste samtalehistorikken. Start en ny økt med samme samtale og prøv igjen.", &format!("the card says it in Norwegian: {sentence:?}"));
            check(label("error-action:fresh_retry:marker-u3") == "Ny økt, prøv igjen", "its first button is the fresh retry, in Norwegian");
            check(drawn("error-action:switch_model:marker-u3") && drawn("error-action:copy_details:marker-u3"), "then Switch model and Copy");
            check(drawn("error-details:marker-u3") && !drawn("error-detail:marker-u3"), "Details is there, closed");
            check(press("error-details:marker-u3"), "Details opens");
            true
        })),
        ("details open", Box::new(move |_, _, elapsed| {
            if !drawn("error-detail:marker-u3") && elapsed < Duration::from_secs(5) {
                return false;
            }
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            shot(&o2, "turn-errors-02-details-open");
            check(drawn("error-detail:marker-u3"), "opened, Details shows the provider's words");
            check(drawn("error-meta:marker-u3"), "and where it happened (backend · model · trace)");
            check(load(rows_through("marker-u6")), "the rest of the fixture lands");
            true
        })),
        ("every card", Box::new(move |_, _, elapsed| {
            if !drawn("error-card:marker-u6") && elapsed < Duration::from_secs(8) {
                return false;
            }
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            shot(&o3, "turn-errors-03-usage-auth-too-long");
            for id in ["marker-u4", "marker-u5", "marker-u6"] {
                check(has_row(id), &format!("{id} is a card of its own"));
            }
            check(!drawn("error-action:fresh_retry:marker-u3"), "an older card no longer offers its retry");
            check(drawn("error-action:compact_retry:marker-u6"), "the newest failure offers Compact and continue");
            check(!drawn("error-action:retry_at:marker-u4"), "an older usage limit offers no scheduled retry");
            check(!drawn("error-action:sign_in:marker-u5"), "the desktop has no sign-in of its own: left out");
            check(press("error-action:compact_retry:marker-u6"), "Compact and continue is pressed");
            true
        })),
        ("retry posted", Box::new(move |_, _, elapsed| {
            let posted = posts("/turn-retry");
            let done = action_done("marker-u6", "compact_retry");
            if (posted.is_empty() || !done) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(posted.last().is_some_and(|p| p["body"] == json!({"session": SESSION, "message_id": "u6", "compact": true})), &format!("the Host is asked to compact and send u6 again: {posted:?}"));
            check(done, "the button shows it done");
            check(drawn("error-note:marker-u6"), "with a note saying what happens");
            shot(&o4, "turn-errors-04-compacting");
            check(press("error-action:copy_details:marker-u5"), "Copy for AI on the auth card");
            true
        })),
        ("copied", Box::new(move |_, _, elapsed| {
            let path = std::env::var("CLARP_TEST_CLIPBOARD").unwrap_or_default();
            let copied = std::fs::read_to_string(&path).unwrap_or_default();
            if !copied.contains("Code: auth") && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(copied == fixture()["log_rows"][10]["error"]["copy_text"].as_str().unwrap_or("?"), &format!("the clipboard holds the Host's prompt: {copied:?}"));
            check(label("error-note:marker-u5") == "Kopiert.", &format!("the card says it was copied: {:?}", label("error-note:marker-u5")));
            shot(&o5, "turn-errors-05-copied");
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("live", Box::new(|app, _, _| {
            let lseq = app.engine.borrow().live_view(SESSION).and_then(|v| v.lseq());
            if !(app.engine.borrow().live_items() && lseq.is_some()) {
                return false;
            }
            let mut op = fixture()["live_turn_op"].clone();
            let at = now_ms();
            op["turn"]["started_at_ms"] = json!(at - 14_000);
            op["turn"]["ended_at_ms"] = json!(at);
            op["turn"]["turn_id"] = json!("7");
            check(control("/__control/upsert", &json!({"session": SESSION, "turns": [{"id": "u7", "role": "user", "origin": "user", "text": "Go on", "trace_id": "7"}]})).is_ok(), "the user writes again");
            check(control("/__control/live-event", &json!({"session": SESSION, "event": {"ops": [op]}})).is_ok(), "the live turn fails, explained");
            true
        })),
        ("live card", Box::new(move |_, _, elapsed| {
            let live = format!("{}7", clarp_core::turn_error::LIVE_PREFIX);
            if !drawn(&format!("error-card:{live}")) && elapsed < Duration::from_secs(8) {
                return false;
            }
            check(drawn(&format!("error-card:{live}")), &format!("the live failed turn is a card: {:?}", shown_ids()));
            check(drawn(&format!("error-action:retry:{live}")), "the newest failure offers Try again");
            check(!drawn("error-action:compact_retry:marker-u6"), "the one before no longer does");
            shot(&o6, "turn-errors-06-live");
            let notice = json!({"id": "marker-u7", "role": "assistant", "origin": "system", "failure_kind": "provider_busy", "trace_id": "7",
                "text": "Turn failed.\n\n529 Overloaded", "error": fixture()["live_turn_op"]["turn"]["error"]});
            check(control("/__control/upsert", &json!({"session": SESSION, "turns": [notice]})).is_ok(), "its notice lands in /log");
            true
        })),
        ("live once", Box::new(move |_, _, elapsed| {
            let live = format!("error-card:{}7", clarp_core::turn_error::LIVE_PREFIX);
            if (drawn(&live) || !drawn("error-card:marker-u7")) && elapsed < Duration::from_secs(8) {
                return false;
            }
            check(drawn("error-card:marker-u7") && !drawn(&live), &format!("the failure shows once, as its notice: {:?}", shown_ids()));
            true
        })),
    ];
    run_stages(stages);
}
