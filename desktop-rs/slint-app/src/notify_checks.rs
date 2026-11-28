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
        _actions: Vec<String>,
        _hints: std::collections::HashMap<String, zbus::zvariant::OwnedValue>,
        _expire_timeout: i32,
    ) -> u32 {
        tokio::time::sleep(DELAY).await;
        let id = if replaces_id != 0 { replaces_id } else { NEXT_ID.fetch_add(1, Ordering::Relaxed) };
        record(json!({"call": "Notify", "id": id, "replaces_id": replaces_id, "body": body}));
        id
    }

    fn close_notification(&self, id: u32) {
        record(json!({"call": "CloseNotification", "id": id}));
    }

    fn get_capabilities(&self) -> Vec<String> {
        vec!["actions".into(), "body".into(), "body-images".into()]
    }

    fn get_server_information(&self) -> (String, String, String, String) {
        ("check".into(), "clarp".into(), "1".into(), "1.2".into())
    }
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
            send(reply("q-1", "First reply"));
            send(reply("q-2", "Second reply"));
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
            send(reply("q-3", "Third reply"));
            check(control("/__control/event", &json!({"type": "chat-read", "session": "rachel", "agent_id": "a1", "at_ms": 1})).is_ok(), "the chat is read elsewhere");
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
            true
        })),
    ];
    run_stages(stages);
}

/// Where the question's calls begin in the daemon's record.
static QUESTION_FROM: AtomicU32 = AtomicU32::new(0);
