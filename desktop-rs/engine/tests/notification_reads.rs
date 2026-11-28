//! Ordered reads through the desktop's event streams, including a paired Host.
mod common;

use clarp_engine::Change;
use common::{Driver, Host};
use serde_json::{Value, json};
use std::time::Duration;

fn alert(id: &str, seq: u64) -> Value {
    json!({"type":"user-notification", "session":"mike", "alert":true, "unread":true,
           "notification_id":id, "order_epoch":"one", "event_at_ms":42, "event_seq":seq})
}

fn read(kind: &str, seq: u64) -> Value {
    json!({"type":kind, "session":"mike", "order_epoch":"one", "read_through_ms":42, "read_through_seq":seq})
}

// The marker follows every event in this batch on the same SSE stream.
fn deliver(d: &mut Driver, host: &Host, events: &[Value]) -> Vec<(String, String)> {
    d.changes.clear();
    for event in events {
        host.control("/__control/event", event.clone());
    }
    host.control(
        "/__control/event",
        json!({"type":"user-notification", "session":"marker",
        "preview":"end of batch", "alert":true, "unread":false}),
    );
    d.until_within(
        "end of event batch",
        Duration::from_secs(10),
        |_, changes| {
            changes
                .iter()
                .any(|c| matches!(c, Change::Notification(p) if p.body == "end of batch"))
        },
    );
    d.changes
        .iter()
        .filter_map(|c| match c {
            Change::Notification(p) if p.body != "end of batch" => {
                Some((p.session.clone(), p.notification.notification_id.clone()))
            }
            _ => None,
        })
        .collect()
}

#[test]
fn bounded_notification_reads() {
    // SAFETY: this binary has one test; configure the peer before starting threads.
    unsafe { std::env::set_var("CLARP_PEER_TOKEN", "probe-token") };
    let primary = Host::start("read-primary");
    let peer = Host::start_with("read-peer", &["--peer", "--without-notification-events"]);
    peer.control(
        "/__control/add-agent",
        json!({"session":"mike","persona":"Mike"}),
    );
    let mut settings = clarp_core::settings::Settings::in_memory();
    clarp_core::hosts::remember(&mut settings, &peer.base, "Peer Host");
    settings.set(clarp_core::all_hosts::SETTING, true);
    let mut d = Driver::with_settings(&primary.base, settings);
    d.connect();
    d.until("peer connected", |e| {
        e.roster().find("@fake-host-2/mike").is_some()
    });
    d.engine.select("rachel");
    let unread = |d: &Driver, chat: &str| d.engine.roster().find(chat).unwrap().unread;
    let mut b = alert("b", 2);
    b["decision_id"] = "question".into();
    let mut receipt = read("chat-read", 1);
    receipt["server_instance_id"] = "non-authoritative push field".into();
    deliver(&mut d, &primary, &[alert("a", 1), b, receipt]);
    assert!(unread(&d, "mike"));
    d.engine.mark_popup_read("mike", "fake-host-1");
    assert!(unread(&d, "mike"));
    let mut stale = read("agent-focus", 0);
    assert!(deliver(&mut d, &primary, &[stale.clone(), alert("late-a", 1)]).is_empty());
    assert!(d.engine.roster().find("mike").unwrap().focused);
    assert!(unread(&d, "mike"));

    stale["decision_id"] = "question".into();
    stale["read_through_seq"] = 99.into();
    let mut wrong_chat = stale.clone();
    wrong_chat["session"] = "rachel".into();
    deliver(&mut d, &peer, &[stale.clone()]);
    deliver(&mut d, &primary, &[wrong_chat]);
    assert!(unread(&d, "mike"));
    deliver(&mut d, &primary, &[stale]);
    assert!(!unread(&d, "mike"));
    assert!(deliver(&mut d, &primary, &[alert("late-again", 1)]).is_empty());
    assert!(!unread(&d, "mike"));
    assert_eq!(deliver(&mut d, &primary, &[alert("c", 3)]).len(), 1);
    assert!(unread(&d, "mike"));

    let mut restarted = read("chat-read", 99);
    restarted["order_epoch"] = "two".into();
    let mut invalid = read("agent-focus", 99);
    invalid["read_through_seq"] = (-1).into();
    deliver(
        &mut d,
        &primary,
        &[
            restarted,
            invalid,
            json!({"type":"chat-read","session":"mike"}),
        ],
    );
    assert!(unread(&d, "mike"));
    deliver(&mut d, &primary, &[read("agent-focus", 3)]);
    assert!(!unread(&d, "mike"));

    let mut missing = alert("missing", 1);
    missing.as_object_mut().unwrap().remove("event_seq");
    let mut invalid = alert("fraction", 1);
    invalid["event_seq"] = json!(0.5);
    let mut other_epoch = alert("new-generation", 1);
    other_epoch["order_epoch"] = "three".into();
    assert_eq!(
        deliver(
            &mut d,
            &primary,
            &[missing, invalid, other_epoch, read("chat-read", 100)]
        )
        .len(),
        3
    );
    assert!(unread(&d, "mike"));
    assert_eq!(
        deliver(&mut d, &peer, &[read("chat-read", 100), alert("peer-a", 1)]),
        vec![("@fake-host-2/mike".to_owned(), "peer-a".to_owned())]
    );
    assert!(unread(&d, "@fake-host-2/mike"));
}
