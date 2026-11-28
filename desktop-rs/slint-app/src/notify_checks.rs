//! `--check notify-daemon` (with `CLARP_TEST_NOTIFY_DAEMON=1`): popups go
//! over D-Bus to a notification daemon of the check's own on its private
//! bus, which answers Notify only after a while, as a busy desktop does.
//! One Notify per chat is on its way at a time: a second reply of the same
//! chat waits for the first's id and replaces that popup, and a chat read
//! or a question answered elsewhere while a Notify is on its way closes
//! the popup once the daemon has named it.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use serde_json::{Value, json};

use super::{Stage, check, control, run_stages};

/// How long the daemon takes to answer Notify.
const DELAY: Duration = Duration::from_millis(1500);

/// Every call the daemon took, in order: `{call, id, replaces_id, body}`.
static CALLS: Mutex<Vec<Value>> = Mutex::new(Vec::new());
/// What the daemon shows now: id and body, as a desktop would draw them.
static VISIBLE: Mutex<std::collections::BTreeMap<u32, String>> = Mutex::new(std::collections::BTreeMap::new());
static READY: AtomicBool = AtomicBool::new(false);
static NEXT_ID: AtomicU32 = AtomicU32::new(101);
/// The daemon's own connection, kept for the check's lifetime.
static DAEMON: OnceLock<zbus::Connection> = OnceLock::new();

fn record(call: Value) {
    if let Ok(mut calls) = CALLS.lock() {
        calls.push(call);
    }
}

fn calls() -> Vec<Value> {
    CALLS.lock().map(|c| c.clone()).unwrap_or_default()
}

struct Daemon;

#[zbus::interface(name = "org.freedesktop.Notifications")]
impl Daemon {
    #[allow(clippy::too_many_arguments)]
    async fn notify(
        &self,
        _app_name: String,
        replaces_id: u32,
        _app_icon: String,
        _summary: String,
        body: String,
        actions: Vec<String>,
        _hints: std::collections::HashMap<String, zbus::zvariant::OwnedValue>,
        _expire_timeout: i32,
    ) -> u32 {
        tokio::time::sleep(DELAY).await;
        let id = if replaces_id != 0 { replaces_id } else { NEXT_ID.fetch_add(1, Ordering::Relaxed) };
        record(json!({"call": "Notify", "id": id, "replaces_id": replaces_id, "body": body, "actions": actions}));
        if let Ok(mut visible) = VISIBLE.lock() {
            visible.insert(id, body);
        }
        id
    }

    fn close_notification(&self, id: u32) {
        record(json!({"call": "CloseNotification", "id": id}));
        if let Ok(mut visible) = VISIBLE.lock() {
            visible.remove(&id);
        }
    }

    fn get_capabilities(&self) -> Vec<String> {
        vec!["actions".into(), "body".into(), "body-images".into()]
    }

    fn get_server_information(&self) -> (String, String, String, String) {
        ("check".into(), "clarp".into(), "1".into(), "1.2".into())
    }
}

/// The daemon says a button of popup `id` was pressed, as a desktop does.
fn press(id: u32, key: String) {
    crate::platform::runtime::handle().spawn(async move {
        let Some(connection) = DAEMON.get() else { return };
        let sent = connection
            .emit_signal(None::<&str>, "/org/freedesktop/Notifications", "org.freedesktop.Notifications", "ActionInvoked", &(id, key))
            .await;
        if let Err(error) = sent {
            eprintln!("clarp-slint: the check's daemon could not say a button was pressed: {error}");
        }
    });
}

fn start_daemon() {
    crate::platform::runtime::handle().spawn(async {
        let served = async {
            zbus::connection::Builder::session()?
                .name("org.freedesktop.Notifications")?
                .serve_at("/org/freedesktop/Notifications", Daemon)?
                .build()
                .await
        };
        match served.await {
            Ok(connection) => {
                let _ = DAEMON.set(connection);
                READY.store(true, Ordering::Relaxed);
            }
            // The first stage then waits for it until the run fails.
            Err(error) => eprintln!("clarp-slint: the check's notification daemon is not on the bus: {error}"),
        }
    });
}

fn reply(id: &str, preview: &str) -> Value {
    json!({"type": "user-notification", "event": "reply", "alert": true, "unread": true, "notification_id": id,
           "session": "rachel", "agent_id": "a1", "persona": "Rachel", "preview": preview, "message_id": format!("m-{id}")})
}

fn send(event: Value) {
    check(control("/__control/event", &event).is_ok(), &format!("the Host sends {}", event["notification_id"]));
}

/// The bodies the daemon shows now.
fn visible() -> Vec<String> {
    VISIBLE.lock().map(|v| v.values().cloned().collect()).unwrap_or_default()
}

/// A question card on the fake Host, pending, and its question's event.
fn ask(card: &str, decision: &str, question: &str) -> Value {
    let pending = json!({"artifact_id": card, "type": "question", "status": "active", "session": "rachel", "title": question,
                         "decision": {"decision_id": decision, "status": "pending", "revision": 1, "response_type": "approval"}});
    check(control("/__control/artifact-update", &json!({"add": [pending], "session": "rachel"})).is_ok(), "the Host has a question card");
    json!({"type": "user-notification", "event": "needs_you", "alert": true, "unread": false, "notification_id": format!("decision-{decision}"),
           "session": "rachel", "agent_id": "a1", "persona": "Rachel", "preview": question,
           "decision_id": decision, "artifact_id": card, "response_type": "approval", "revision": 1,
           "options": [{"id": "accepted", "label": "Yes"}, {"id": "rejected", "label": "No"}]})
}

/// The card answered on another device: the Host says it changed.
fn answer_elsewhere(card: &str, decision: &str) {
    let answered = json!({"decision_id": decision, "status": "accepted", "revision": 2, "response_type": "approval"});
    check(control("/__control/artifact-update", &json!({"id": card, "set": {"decision": answered, "status": "completed"}, "session": "rachel"})).is_ok(),
          "the question is answered elsewhere");
}

/// The calls since the `from`th.
fn since(from: usize) -> Vec<Value> {
    calls().into_iter().skip(from).collect()
}

pub fn notify_daemon_check(_out: String) {
    let mut started = false;
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, window, elapsed| {
            if !started {
                start_daemon();
                started = true;
            }
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !READY.load(Ordering::Relaxed) || elapsed < Duration::from_millis(1500) {
                return false;
            }
            window.invoke_chat_chosen("mike".into());
            // Two replies of one chat before the daemon has answered the first.
            let mut first = reply("q-1", "First reply");
            first["order_epoch"] = "one".into();
            first["event_at_ms"] = 1.into();
            first["event_seq"] = 1.into();
            let mut second = reply("q-2", "Second reply");
            second["order_epoch"] = "one".into();
            second["event_at_ms"] = 1.into();
            second["event_seq"] = 2.into();
            send(first);
            send(second);
            send(json!({"type":"chat-read","session":"rachel","order_epoch":"one","read_through_ms":1,"read_through_seq":1}));
            true
        })),
        ("chained", Box::new(|_, _window, elapsed| {
            let notified: Vec<Value> = calls().into_iter().filter(|c| c["call"] == "Notify").collect();
            if notified.len() < 2 || elapsed < DELAY * 3 {
                return false;
            }
            check(notified.len() == 2, &format!("two replies of a chat make two Notify calls, one after the other: {notified:?}"));
            check(notified[0]["replaces_id"] == 0 && notified[1]["replaces_id"] == notified[0]["id"],
                  &format!("the second replaces the first's popup, by the id the daemon gave it: {notified:?}"));
            check(notified[1]["body"] == "Second reply", "and shows the newer reply");
            // A reply, then the chat read on another device before the daemon answers.
            send(json!({"type":"agent-focus","session":"rachel","order_epoch":"one","read_through_ms":1,"read_through_seq":1}));
            true
        })),
        ("stale focus read", Box::new(|app, _, elapsed| {
            if !app.engine.borrow().roster().find("rachel").is_some_and(|a| a.focused) || elapsed < DELAY {
                return false;
            }
            check(visible().iter().any(|body| body == "Second reply"), "the later reply stays visible after the stale focus read");
            check(app.engine.borrow().roster().find("rachel").is_some_and(|a| a.unread), "the later reply stays unread");
            let mut third = reply("q-3", "Third reply");
            third["order_epoch"] = "one".into();
            third["event_at_ms"] = 1.into();
            third["event_seq"] = 3.into();
            send(third);
            check(control("/__control/event", &json!({"type": "chat-read", "session": "rachel", "agent_id": "a1", "order_epoch":"one","read_through_ms":1,"read_through_seq":3})).is_ok(), "the chat is read elsewhere");
            true
        })),
        ("read meanwhile", Box::new(|_, _window, elapsed| {
            let all = calls();
            let Some(third) = all.iter().position(|c| c["call"] == "Notify" && c["body"] == "Third reply") else { return false };
            if elapsed < DELAY * 2 {
                return false;
            }
            let id = all[third]["id"].clone();
            let closed_after = all[third + 1..].iter().any(|c| c["call"] == "CloseNotification" && c["id"] == id);
            check(closed_after, &format!("a chat read while its Notify was on its way is closed once the daemon names it: {all:?}"));
            // A question, answered on another device before the daemon answers.
            let card = json!({"artifact_id": "q-late", "type": "question", "status": "active", "session": "rachel", "title": "Ship tonight?",
                              "decision": {"decision_id": "d-late", "status": "pending", "revision": 1, "response_type": "approval"}});
            check(control("/__control/artifact-update", &json!({"add": [card], "session": "rachel"})).is_ok(), "the Host has a question card");
            QUESTION_FROM.store(all.len() as u32, Ordering::Relaxed);
            send(json!({"type": "user-notification", "event": "needs_you", "alert": true, "unread": false, "notification_id": "decision-d-late",
                        "session": "rachel", "agent_id": "a1", "persona": "Rachel", "preview": "Ship tonight?",
                        "decision_id": "d-late", "artifact_id": "q-late", "response_type": "approval", "revision": 1,
                        "options": [{"id": "accepted", "label": "Ship"}, {"id": "rejected", "label": "Wait"}]}));
            let answered = json!({"decision_id": "d-late", "status": "accepted", "revision": 2, "response_type": "approval"});
            check(control("/__control/artifact-update", &json!({"id": "q-late", "set": {"decision": answered, "status": "completed"}, "session": "rachel"})).is_ok(),
                  "the question is answered elsewhere");
            true
        })),
        ("answered meanwhile", Box::new(|_, _window, elapsed| {
            let later = since(QUESTION_FROM.load(Ordering::Relaxed) as usize);
            let Some(asked) = later.iter().position(|c| c["call"] == "Notify" && c["body"] == "Ship tonight?") else { return false };
            if elapsed < DELAY * 2 {
                return false;
            }
            let id = later[asked]["id"].clone();
            check(later[asked + 1..].iter().any(|c| c["call"] == "CloseNotification" && c["id"] == id),
                  &format!("a question answered while its Notify was on its way is closed once the daemon names it: {later:?}"));
            // A question on its way, a reply of the chat behind it, then the
            // question answered elsewhere: only the question goes.
            QUESTION_FROM.store(calls().len() as u32, Ordering::Relaxed);
            send(ask("q-a1", "d-a1", "Approve A1?"));
            send(reply("q-b1", "Reply B1"));
            answer_elsewhere("q-a1", "d-a1");
            true
        })),
        ("reply behind a closed question", Box::new(|_, _window, elapsed| {
            let later = since(QUESTION_FROM.load(Ordering::Relaxed) as usize);
            let Some(b) = later.iter().position(|c| c["call"] == "Notify" && c["body"] == "Reply B1") else { return false };
            if elapsed < DELAY * 3 {
                return false;
            }
            let a = later.iter().position(|c| c["call"] == "Notify" && c["body"] == "Approve A1?");
            let a_id = a.map(|a| later[a]["id"].clone()).unwrap_or_default();
            check(a.is_some_and(|a| a < b) && later[b]["replaces_id"] == a_id, &format!("the reply waits for the question's id and replaces it: {later:?}"));
            let shown = visible();
            check(shown.iter().any(|v| v == "Reply B1") && !shown.iter().any(|v| v == "Approve A1?"), &format!("the reply stays, the question is gone: {shown:?}"));
            check(!later[b + 1..].iter().any(|c| c["call"] == "CloseNotification" && c["id"] == later[b]["id"]), "nothing closes the reply's popup");
            // A question shown; a reply of the chat replacing it; then the
            // question answered elsewhere while that replacement is on its way.
            QUESTION_FROM.store(calls().len() as u32, Ordering::Relaxed);
            send(ask("q-a2", "d-a2", "Approve A2?"));
            true
        })),
        ("question shown", Box::new(|_, _window, _| {
            let later = since(QUESTION_FROM.load(Ordering::Relaxed) as usize);
            if !later.iter().any(|c| c["call"] == "Notify" && c["body"] == "Approve A2?") {
                return false;
            }
            send(reply("q-b2", "Reply B2"));
            answer_elsewhere("q-a2", "d-a2");
            true
        })),
        ("reply owns the id", Box::new(|_, _window, elapsed| {
            let later = since(QUESTION_FROM.load(Ordering::Relaxed) as usize);
            let Some(b) = later.iter().position(|c| c["call"] == "Notify" && c["body"] == "Reply B2") else { return false };
            if elapsed < DELAY * 3 {
                return false;
            }
            let a = later.iter().position(|c| c["call"] == "Notify" && c["body"] == "Approve A2?").unwrap_or(0);
            let id = later[a]["id"].clone();
            check(later[b]["replaces_id"] == id, &format!("the reply replaces the shown question's popup: {later:?}"));
            check(!later[a + 1..].iter().any(|c| c["call"] == "CloseNotification" && c["id"] == id),
                  &format!("answering the replaced question closes nothing: the id is the reply's: {later:?}"));
            let shown = visible();
            check(shown.iter().any(|v| v == "Reply B2") && !shown.iter().any(|v| v == "Approve A2?"), &format!("the reply stays, the question is gone: {shown:?}"));
            // A question shown, a newer question replacing it, and Yes pressed
            // on the older popup while that replacement is on its way.
            QUESTION_FROM.store(calls().len() as u32, Ordering::Relaxed);
            send(ask("q-a3", "d-a3", "Approve A3?"));
            true
        })),
        ("older question shown", Box::new(|_, _window, elapsed| {
            let later = since(QUESTION_FROM.load(Ordering::Relaxed) as usize);
            let Some(a) = later.iter().find(|c| c["call"] == "Notify" && c["body"] == "Approve A3?") else { return false };
            // The app has the daemon's answer, and so the popup's buttons.
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            PRESSED_ID.store(a["id"].as_u64().unwrap_or(0) as u32, Ordering::Relaxed);
            send(ask("q-b3", "d-b3", "Approve B3?"));
            true
        })),
        ("pressed during the replacement", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            let Some(key) = approval_key("Approve A3?") else { return false };
            press(PRESSED_ID.load(Ordering::Relaxed), key);
            true
        })),
        ("ambiguous click ignored", Box::new(|_, _window, elapsed| {
            if elapsed < DELAY * 2 {
                return false;
            }
            check(super::posts("/decisions/d-a3/resolve").is_empty(), "a click during replacement does not answer the older card");
            check(super::posts("/decisions/d-b3/resolve").is_empty(), "a click during replacement does not answer the newer card");
            let Some(key) = approval_key("Approve A3?") else { return false };
            press(PRESSED_ID.load(Ordering::Relaxed), key);
            true
        })),
        ("late action from the old popup ignored", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            check(super::posts("/decisions/d-a3/resolve").is_empty(), "the late button did not answer the old question");
            check(super::posts("/decisions/d-b3/resolve").is_empty(), "the late button did not answer the replacement");
            let Some(key) = approval_key("Approve B3?") else { return false };
            press(PRESSED_ID.load(Ordering::Relaxed), key);
            true
        })),
        ("settled replacement answers its own question", Box::new(|_, _window, _| {
            let newer = super::posts("/decisions/d-b3/resolve");
            if newer.is_empty() {
                return false;
            }
            check(newer.len() == 1 && newer[0]["body"]["choice"] == "accepted", "the settled popup answers only its own question");
            check(super::posts("/decisions/d-a3/resolve").is_empty(), "the older question was never answered");
            true
        })),
    ];
    run_stages(stages);
}

/// The older question's popup id, whose button the daemon presses.
static PRESSED_ID: AtomicU32 = AtomicU32::new(0);

/// Where the question's calls begin in the daemon's record.
static QUESTION_FROM: AtomicU32 = AtomicU32::new(0);

fn approval_key(body: &str) -> Option<String> {
    let calls = since(QUESTION_FROM.load(Ordering::Relaxed) as usize);
    let call = calls.iter().find(|call| call["call"] == "Notify" && call["body"] == body)?;
    call["actions"].as_array()?.chunks_exact(2).find_map(|pair| {
        let key = pair[0].as_str()?;
        let (_, action) = key.split_once(':')?;
        (action == "choice:accepted").then(|| key.to_owned())
    })
}

/// Two real UI processes share the Host, data directory, notification daemon
/// and session bus. Only their normal SSE receiver drives notification state.
pub fn restart_check() {
    fn journal() -> Vec<Value> {
        let path = std::env::var("CLARP_RESTART_LOG").unwrap();
        std::fs::read_to_string(path).unwrap_or_default().lines().map(|s| serde_json::from_str(s).unwrap()).collect()
    }
    fn press_fixture(key: &str) {
        let path = std::path::PathBuf::from(std::env::var_os("CLARP_RESTART_LOG").unwrap());
        std::fs::write(path.with_extension(key), []).unwrap();
    }
    fn ordered(text: &str, seq: u64) -> Value {
        json!({"type":"user-notification","session":"mike","alert":true,"unread":true,
               "preview":text,"notification_id":text,"decision_id":text,"order_epoch":"one","event_at_ms":51,"event_seq":seq})
    }
    fn read(seq: u64) -> Value {
        json!({"type":"chat-read","session":"mike","order_epoch":"one","read_through_ms":51,"read_through_seq":seq})
    }
    let first = std::env::var("CLARP_CHECK_PASS").as_deref() == Ok("first");
    let mut stages: Vec<Stage> = vec![("connected", Box::new(move |app, window, _| {
        if app.engine.borrow().connection_state() != "live" || app.engine.borrow().server_id().is_empty() || app.engine.borrow().roster().find("mike").is_none() {
            return false;
        }
        window.invoke_chat_chosen("rachel".into());
        if first {
            send(ordered("covered first", 2));
        } else {
            check(app.engine.borrow().roster().find("mike").unwrap().unread, "the larger sequence is still unread on restart");
            send(ordered("covered first", 2));
            send(read(2));
            send(read(1));
            let mut wrong = read(9);
            wrong["order_epoch"] = "other".into();
            send(wrong);
            let mut foreign = read(9);
            foreign["session"] = "rachel".into();
            send(foreign);
            send(json!({"type":"agent-focus","session":"mike"}));
        }
        true
    }))];
    if first {
        stages.push(("first alert delivered", Box::new(|_, _, _| {
            if !journal().iter().any(|r| r["body"] == "covered first") { return false; }
            send(read(2));
            send(ordered("before restart", 3));
            true
        })));
        stages.push(("daemon displayed", Box::new(|_, _, _| journal().iter().any(|r| r["body"] == "before restart"))));
    } else {
        stages.push(("replayed events processed", Box::new(|app, _, _| {
            let engine = app.engine.borrow();
            let mike = engine.roster().find("mike").unwrap();
            if !mike.focused { return false; }
            check(mike.unread, "stale, foreign and missing-order reads preserve unread");
            send(json!({"type":"chat-read","session":"mike","decision_id":"before restart"}));
            true
        })));
        stages.push(("restored decision cleared", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("mike").unwrap().unread { return false; }
            send(ordered("after restart", 4));
            true
        })));
        stages.push(("daemon reused ID", Box::new(|app, _, elapsed| {
            let rows = journal();
            if !rows.iter().any(|r| r["body"] == "after restart") || elapsed < Duration::from_millis(500) { return false; }
            check(rows.iter().filter(|r| r["body"] == "covered first").count() == 1, "the covered alert never displays again");
            check(rows.iter().all(|r| r.get("close").is_none() || r["close"] == "covered first"), "no close by a saved daemon ID");
            check(rows.iter().find(|r| r["body"] == "after restart").unwrap()["replaces"] == 0, "the new app requests a fresh popup");
            press_fixture("stale");
            check(app.engine.borrow().roster().find("mike").unwrap().unread, "the later alert keeps its unread mark");
            true
        })));
        stages.push(("old native action delivered", Box::new(|app, _, elapsed| {
            if !journal().iter().any(|r| r["pressed"] == "stale") || elapsed < Duration::from_millis(500) { return false; }
            check(super::posts("/chat-read").is_empty() && app.engine.borrow().roster().find("mike").unwrap().unread,
                  "the old action cannot act on the reused ID");
            check(control("/__control/chat-read-receipt", &json!({"order_epoch":"one","read_through_ms":51,"read_through_seq":4})).is_ok(),
                  "the Host will return its boundary before the SSE echo");
            press_fixture("current");
            true
        })));
        stages.push(("current popup read", Box::new(|app, _, _| {
            if !journal().iter().any(|r| r["close"] == "after restart") { return false; }
            check(!app.engine.borrow().roster().find("mike").unwrap().unread, "the covering read clears the current evidence");
            check(super::posts("/chat-read").last().is_some_and(|r| r["body"] == json!({"session":"mike"})), "Mark read posts only the session");
            send(read(4));
            send(json!({"type":"chat-unread","session":"mike","unread":true,"preview":"quiet failure"}));
            send(read(99));
            send(json!({"type":"agent-focus","session":"rachel"}));
            true
        })));
        stages.push(("unordered evidence survives", Box::new(|app, _, _| {
            let engine = app.engine.borrow();
            if !engine.roster().find("rachel").is_some_and(|a| a.focused) { return false; }
            check(engine.roster().find("mike").unwrap().unread, "chat-unread survives a read without comparable order");
            check(journal().iter().all(|r| r["body"] != "quiet failure"), "chat-unread never pops up");
            true
        })));
    }
    run_stages(stages);
}
