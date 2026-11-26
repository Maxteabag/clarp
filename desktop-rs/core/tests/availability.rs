//! Whether an agent can answer now (#918, desktop #924): the row's
//! `availability`, its `agent-roster` patch, the Host's `host-updating`
//! until the next snapshot, and the one line the desktop shows: the Host's
//! `text` as sent, with the time part counted here.

use chrono::Utc;
use clarp_core::availability::{Availability, HostUpdating, line, strip_cached};
use clarp_core::json::Object;
use clarp_core::roster::{RowChange, Roster};
use serde_json::{Value, json};

/// 26 Nov 2026 13:40:00 UTC, a Thursday.
const NOW: i64 = 1_795_700_400_000;

fn obj(value: Value) -> Object {
    value.as_object().cloned().unwrap()
}

/// An `availability` as the Host sends it: `text` is its line without the
/// time part.
fn availability(state: &str, text: &str, back_at_ms: Option<i64>, queue_ahead: i64, last_alive_ms: Option<i64>) -> Value {
    json!({"state": state, "text": text, "since_ms": NOW - 5_000, "back_at_ms": back_at_ms,
           "queue_ahead": queue_ahead, "last_alive_ms": last_alive_ms})
}

fn ok() -> Value {
    availability("ok", "", None, 0, None)
}

fn busy(ahead: i64) -> Value {
    availability("busy", &format!("Busy · {ahead} messages ahead of you"), None, ahead, None)
}

fn row(session: &str, extra: Value) -> Value {
    let mut row = json!({"session": session, "agent_id": format!("id-{session}"), "persona": session, "last_activity": 100});
    for (key, value) in extra.as_object().unwrap() {
        row[key] = value.clone();
    }
    row
}

fn roster(rows: Value) -> Roster {
    let mut roster = Roster::new();
    roster.apply_snapshot(&obj(json!({"agents": rows})));
    roster.take_ops();
    roster
}

/// The line at NOW, this machine's clock equal to the Host's.
fn shown(value: Value) -> String {
    line(Availability::from_json(Some(&value)).as_ref(), None, 0, NOW, &Utc)
}

fn updating(text: &str, back_at_ms: Option<i64>) -> HostUpdating {
    HostUpdating::from_event(&obj(json!({"type": "host-updating", "text": text, "since_ms": NOW, "back_at_ms": back_at_ms})))
}

#[test]
fn a_row_reads_its_availability_and_an_older_row_none() {
    let a = Availability::from_json(Some(&availability("busy", "Busy · 25 messages ahead of you", None, 25, Some(NOW - 1_000)))).unwrap();
    assert_eq!(a, Availability { state: "busy".into(), text: "Busy · 25 messages ahead of you".into(), since_ms: NOW - 5_000,
        back_at_ms: None, queue_ahead: 25, last_alive_ms: Some(NOW - 1_000) });
    assert_eq!(Availability::from_json(None), None, "an older Host sends none");
    assert_eq!(Availability::from_json(Some(&json!(null))), None);
    assert_eq!(Availability::from_json(Some(&json!({"state": ""}))), None);
    let event = updating("Host updating", Some(NOW + 40_000));
    assert_eq!(event, HostUpdating { text: "Host updating".into(), since_ms: NOW, back_at_ms: Some(NOW + 40_000) });

    let r = roster(json!([row("rachel", json!({"availability": availability("restarting", "Restarting", Some(NOW + 20_000), 0, None)})), row("old", json!({}))]));
    assert_eq!(r.find("rachel").unwrap().availability.as_ref().map(|a| a.state.as_str()), Some("restarting"));
    assert_eq!(r.find("old").unwrap().availability, None);
    assert_eq!(r.availability_line(r.find("old").unwrap(), NOW, &Utc), "", "nothing new from an older Host");
}

#[test]
fn an_availability_event_patches_the_row_and_the_one_row_read_replaces_it() {
    let mut r = roster(json!([row("rachel", json!({"model": "opus", "availability": ok()}))]));
    assert_eq!(r.availability_line(r.find("rachel").unwrap(), NOW, &Utc), "", "ok shows nothing");
    assert_eq!(r.patch_row("rachel", &obj(json!({"availability": busy(3)}))), RowChange::Applied);
    let agent = r.find("rachel").unwrap();
    assert_eq!(agent.availability.as_ref().unwrap().queue_ahead, 3);
    assert_eq!(agent.model, "opus", "a field the event does not name keeps its value");
    assert_eq!(r.availability_line(agent, NOW, &Utc), "Busy · 3 messages ahead of you");

    let one = obj(row("rachel", json!({"availability": availability("usage_limit", "Usage limit", None, 0, None)})));
    assert_eq!(r.replace_row(&one), RowChange::Applied);
    assert_eq!(r.availability_line(r.find("rachel").unwrap(), NOW, &Utc), "Usage limit");
}

#[test]
fn the_host_updating_stands_on_every_row_until_the_next_snapshot() {
    let rows = json!([row("rachel", json!({"availability": busy(25)})), row("mike", json!({"availability": ok()}))]);
    let mut r = roster(rows.clone());
    r.set_host_updating(updating("Host updating", Some(NOW + 40_000)));
    for session in ["rachel", "mike"] {
        assert_eq!(r.availability_line(r.find(session).unwrap(), NOW, &Utc), "Host updating · back in ~40 s", "{session}");
    }
    // A patch meanwhile keeps it: the Host going down says it, not the agent.
    r.patch_row("rachel", &obj(json!({"availability": ok()})));
    assert_eq!(r.availability_line(r.find("rachel").unwrap(), NOW, &Utc), "Host updating · back in ~40 s");
    // While the connection is down the line still shows.
    r.mark_transport_unavailable();
    assert_eq!(r.availability_line(r.find("mike").unwrap(), NOW, &Utc), "Host updating · back in ~40 s");
    // Back up: the snapshot clears it.
    r.apply_snapshot(&obj(json!({"agents": rows})));
    assert_eq!(r.find("mike").unwrap().host_updating, None);
    assert_eq!(r.availability_line(r.find("rachel").unwrap(), NOW, &Utc), "Busy · 25 messages ahead of you");
    assert_eq!(r.availability_line(r.find("mike").unwrap(), NOW, &Utc), "");
}

#[test]
fn the_host_updating_comes_before_the_agents_own_state() {
    let restarting = Availability::from_json(Some(&availability("restarting", "Restarting", Some(NOW + 20_000), 0, None))).unwrap();
    assert_eq!(line(Some(&restarting), Some(&updating("Host updating", None)), 0, NOW, &Utc), "Host updating");
    assert_eq!(line(Some(&restarting), None, 0, NOW, &Utc), "Restarting · back in ~20 s");
    assert_eq!(line(Some(&restarting), Some(&updating("", None)), 0, NOW, &Utc), "Restarting · back in ~20 s", "an updating without words says nothing");
    assert_eq!(line(None, None, 0, NOW, &Utc), "");
}

#[test]
fn an_unreachable_row_drops_the_agents_own_state() {
    let mut r = roster(json!([row("rachel", json!({"availability": busy(25)}))]));
    r.mark_transport_unavailable();
    assert_eq!(r.availability_line(r.find("rachel").unwrap(), NOW, &Utc), "");
}

#[test]
fn the_hosts_words_as_sent_with_the_time_part_at_a_fixed_clock() {
    clarp_core::time_format::set_clock_24h(true);
    assert_eq!(shown(availability("restarting", "Restarting", Some(NOW + 20_000), 0, None)), "Restarting · back in ~20 s");
    assert_eq!(shown(availability("restarting", "Restarting", Some(NOW + 17_200), 0, None)), "Restarting · back in ~20 s", "rounded up to 5 s");
    assert_eq!(shown(availability("restarting", "Restarting", Some(NOW + 150_000), 0, None)), "Restarting · back in ~3 min");
    assert_eq!(shown(availability("restarting", "Restarting", None, 0, None)), "Restarting");
    assert_eq!(shown(availability("restarting", "Restarting", Some(NOW - 1_000), 0, None)), "Restarting", "an ETA passed says no count");
    assert_eq!(shown(availability("switching_account", "Switching account (usage limit)", None, 0, None)), "Switching account (usage limit)");
    // Over ten minutes off, the clock time; 14:05 UTC is 25 minutes on.
    assert_eq!(shown(availability("usage_limit", "Usage limit", Some(NOW + 25 * 60_000), 0, None)), "Usage limit · back at 14:05");
    assert_eq!(shown(availability("usage_limit", "Usage limit", Some(NOW + 24 * 3_600_000), 0, None)), "Usage limit · back at Fri 13:40", "another day names it");
    assert_eq!(shown(availability("usage_limit", "Usage limit", None, 0, None)), "Usage limit");
    assert_eq!(shown(busy(25)), "Busy · 25 messages ahead of you");
    assert_eq!(shown(availability("busy", "Busy", None, 0, Some(NOW - 1_000))), "Busy", "busy's last sign of life adds nothing");
    assert_eq!(shown(availability("unresponsive", "Not responding", None, 0, Some(NOW - 4 * 60_000 - 10_000))), "Not responding · last sign of life 4 min ago");
    assert_eq!(shown(availability("unresponsive", "Not responding", None, 0, None)), "Not responding");
    assert_eq!(shown(ok()), "");
    assert_eq!(shown(availability("ok", "Fine", None, 0, None)), "", "ok shows nothing whatever it says");
    assert_eq!(shown(availability("restarting", "", Some(NOW + 20_000), 0, None)), "", "no words, no line");
    assert_eq!(shown(availability("a_state_from_later", "Napping", None, 0, None)), "Napping", "a new state shows the Host's words");
    assert_eq!(line(None, Some(&updating("Host updating", Some(NOW + 40_000))), 0, NOW, &Utc), "Host updating · back in ~40 s");
}

#[test]
fn the_count_follows_the_hosts_clock() {
    // The Host runs 30 s ahead of this machine: its back_at is 20 s from
    // the Host's now, not 50 s from this machine's.
    let offset = 30_000;
    let a = Availability::from_json(Some(&availability("restarting", "Restarting", Some(NOW + offset + 20_000), 0, None))).unwrap();
    assert_eq!(line(Some(&a), None, offset, NOW, &Utc), "Restarting · back in ~20 s");
    assert_eq!(line(Some(&a), None, 0, NOW, &Utc), "Restarting · back in ~50 s", "without the offset it would read late");
    // A clock time is shown on this machine's clock.
    clarp_core::time_format::set_clock_24h(true);
    let limit = Availability::from_json(Some(&availability("usage_limit", "Usage limit", Some(NOW + offset + 25 * 60_000), 0, None))).unwrap();
    assert_eq!(line(Some(&limit), None, offset, NOW, &Utc), "Usage limit · back at 14:05");

    // The roster stamps the offset on every row it holds and later reads.
    let mut r = roster(json!([row("rachel", json!({"availability": availability("restarting", "Restarting", Some(NOW + offset + 20_000), 0, None)}))]));
    r.set_clock_offset(offset);
    assert_eq!(r.availability_line(r.find("rachel").unwrap(), NOW, &Utc), "Restarting · back in ~20 s");
    r.patch_row("rachel", &obj(json!({"model": "opus"})));
    assert_eq!(r.find("rachel").unwrap().host_clock_offset_ms, offset);
}

#[test]
fn a_cached_snapshot_loses_its_availability() {
    let mut snapshot = obj(json!({"agents": [row("rachel", json!({"availability": busy(2)}))]}));
    strip_cached(&mut snapshot);
    assert!(snapshot["agents"][0].get("availability").is_none());
    assert_eq!(snapshot["agents"][0]["session"], "rachel");
}
