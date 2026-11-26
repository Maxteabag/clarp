//! What Send does while the agent works against the fake Host (feature
//! `send_while_busy`, #920): the setting read at connect, changed and
//! followed on its event; every user send carries it (Ctrl+Enter waits for
//! the turn); the Host's fallback line shows in the chip; the chip counts
//! the waiting messages while the agent works. An older Host: nothing read,
//! nothing sent, no chip.

mod common;

use clarp_core::send_while_busy::{ChipAction, WhileBusy};
use common::{Driver, Host};
use serde_json::json;

#[test]
fn the_setting_is_read_changed_followed_and_carried_by_every_send() {
    let host = Host::start("send-busy");
    host.control("/__control/send-while-busy", json!({"on": true, "while_busy": "after_turn"}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the setting", |e| e.send_while_busy_offered() && e.while_busy() == WhileBusy::AfterTurn);
    assert_eq!(host.requests("GET", "/send-settings").len(), 1);

    // Changed here: posted, kept.
    d.engine.set_while_busy(WhileBusy::Steer);
    d.until("the write", |_| !host.requests("POST", "/send-settings").is_empty());
    assert_eq!(host.requests("POST", "/send-settings")[0]["body"], json!({"while_busy": "steer"}));

    // Changed in another app: the event carries it.
    host.control("/__control/event", json!({"type": "send-settings-updated", "while_busy": "take_over"}));
    d.until("the event's setting", |e| e.while_busy() == WhileBusy::TakeOver);

    // A send carries the setting; Ctrl+Enter waits for the turn.
    assert!(d.engine.send_composer("rachel", "now please", false));
    assert!(d.engine.send_composer("rachel", "after this turn", true));
    d.until("both sends", |_| host.requests("POST", "/send").len() >= 2);
    let sends = host.requests("POST", "/send");
    assert_eq!(sends[0]["body"]["while_busy"], "take_over");
    assert_eq!(sends[1]["body"]["while_busy"], "after_turn");
    assert_eq!(sends[1]["body"]["queue_if_busy"], true);
}

#[test]
fn the_hosts_fallback_shows_in_the_chip_and_the_chip_counts_what_waits() {
    let host = Host::start("send-busy-chip");
    host.control("/__control/send-while-busy", json!({"on": true}));
    host.control("/__control/queue-steer", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the setting", |e| e.send_while_busy_offered());

    // Idle with messages waiting: no chip.
    d.engine.load_turn_queue("rachel");
    d.until("the queue", |e| e.turn_queue("rachel").len() == 2);
    assert_eq!(d.engine.send_chip("rachel"), None);

    // The Host could not steer: its line shows for the chat.
    host.control(
        "/__control/send-while-busy",
        json!({"fallback": {"from": "steer", "to": "take_over", "reason": "not_steerable", "text": "This agent can't take a message mid-turn: stopped and sent"}}),
    );
    assert!(d.engine.send_composer("rachel", "stop and listen", false));
    d.until("the fallback line", |e| e.send_notice("rachel") == "This agent can't take a message mid-turn: stopped and sent");
    assert_eq!(host.requests("POST", "/send")[0]["body"]["while_busy"], "steer");

    // Working with two waiting: "2 waiting · Steer now".
    host.control("/__control/agent", json!({"session": "rachel", "set": {"busy": true, "latest_state": "thinking", "queued_turn_count": 2}}));
    d.until("the agent works", |e| e.roster().find("rachel").is_some_and(|a| a.busy));
    d.engine.load_turn_queue("rachel");
    d.until("the chip", |e| e.send_chip("rachel").is_some_and(|c| c.action.is_some()));
    let chip = d.engine.send_chip("rachel").unwrap();
    assert_eq!(chip.count, 2);
    assert_eq!(chip.action, Some(ChipAction::Steer("q1".into())));
}

#[test]
fn an_older_host_reads_nothing_and_sends_as_before() {
    let host = Host::start("send-busy-off");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.settle(std::time::Duration::from_millis(300));
    assert!(!d.engine.send_while_busy_offered());
    d.engine.set_while_busy(WhileBusy::TakeOver);
    assert!(d.engine.send_composer("rachel", "plain", false));
    d.until("the send", |_| !host.requests("POST", "/send").is_empty());
    assert!(host.requests("POST", "/send")[0]["body"].get("while_busy").is_none());
    assert!(host.requests("GET", "/send-settings").is_empty() && host.requests("POST", "/send-settings").is_empty());
    host.control("/__control/agent", json!({"session": "rachel", "set": {"busy": true}}));
    d.engine.load_turn_queue("rachel");
    d.until("the queue", |e| e.turn_queue("rachel").len() == 2);
    assert_eq!(d.engine.send_chip("rachel"), None);
}
