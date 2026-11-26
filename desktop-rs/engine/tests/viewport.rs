//! The stream following the panes (#905 on #892) against the fake Host's
//! protocol fixtures: a Host with both `events_live_change` and
//! `events_chat_previews` opens `/events?live_change=1&chat_previews=1&
//! live=…&visible=…`, takes changes by `POST /events/{id}/live` and applies
//! them at `live-changed`; background rows move by previews and read no
//! history until a pane shows them. Without both features nothing changes.

mod common;

use std::time::Duration;

use common::{Driver, Host};
use serde_json::{Value, json};

fn events_queries(host: &Host) -> Vec<Value> {
    host.requests("GET", "/events").into_iter().map(|r| r["query"].clone()).collect()
}

fn posts(host: &Host) -> Vec<Value> {
    host.requests("POST", "/events/{id}/live")
}

fn log_reads(host: &Host, session: &str) -> Vec<Value> {
    host.requests("GET", "/log").into_iter().filter(|r| r["query"]["session"] == session).collect()
}

fn live_reads(host: &Host, session: &str) -> usize {
    host.requests("GET", "/live").into_iter().filter(|r| r["query"]["session"] == session).count()
}

fn names(list: &[&str]) -> Vec<String> {
    list.iter().map(|s| (*s).to_owned()).collect()
}

fn followed(d: &Driver) -> Vec<String> {
    d.engine.viewport_effective().map(|(l, _)| l).unwrap_or_default()
}

fn viewport_host(name: &str) -> Host {
    let host = Host::start(name);
    host.control("/__control/live", json!({"on": true}));
    host.control("/__control/viewport", json!({"on": true}));
    host
}

/// Connected, with rachel open and the stream applying it.
fn showing_rachel(host: &Host) -> Driver {
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.select("rachel");
    d.until("rachel shown", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.until("rachel's log", |e| e.conversation("rachel").is_some_and(|c| c.len() == 2));
    d
}

#[test]
fn an_older_host_keeps_the_live_query_and_reopens_for_a_new_set() {
    let host = Host::start("viewport-old");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    assert!(!d.engine.viewport_streaming());
    d.engine.select("rachel");
    d.engine.select("mike");
    d.until("the stream reopened for both", |_| events_queries(&host).iter().any(|q| q.get("live") == Some(&json!("mike,rachel"))));
    for query in events_queries(&host) {
        assert!(query.get("visible").is_none() && query.get("live_change").is_none() && query.get("chat_previews").is_none(), "{query}");
    }
    assert!(posts(&host).is_empty(), "no POST to an older Host");
    // Only one of the two features: still the old way.
    let one = Host::start("viewport-one");
    one.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&one.base);
    d.connect();
    assert!(!d.engine.viewport_streaming(), "both features are required");
}

#[test]
fn the_stream_names_both_sets_and_changes_them_in_place() {
    let host = viewport_host("viewport-sets");
    let mut d = showing_rachel(&host);
    assert!(d.engine.viewport_streaming());
    let first = events_queries(&host)[0].clone();
    assert_eq!(first["live_change"], "1");
    assert_eq!(first["chat_previews"], "1");
    assert!(first.get("visible").is_some(), "visible= is always named, empty for the list only: {first}");
    assert!(live_reads(&host, "rachel") >= 1, "a shown chat reads its live state");
    let streams = events_queries(&host).len();

    // Another chat in the same pane: one change, the stream stays open.
    d.engine.select("mike");
    d.until("mike shown", |d| d.viewport_effective().is_some_and(|(_, v)| v == ["mike"]));
    assert_eq!(followed(&d), names(&["mike", "rachel"]), "rachel stays followed, out of view");
    let last = posts(&host).last().cloned().unwrap();
    assert_eq!(last["body"], json!({"live": ["mike", "rachel"], "visible": ["mike"]}));
    assert!(last["stream_id"].as_str().unwrap().starts_with("stream-"));

    // Two panes, then one again.
    d.engine.with_panes(|p| p.split_active("vertical", "rachel"));
    d.until("both shown", |d| d.viewport_effective().is_some_and(|(_, v)| v == ["mike", "rachel"]));
    let pane = d.engine.panes().active_pane_id().to_owned();
    d.engine.with_panes(|p| p.close_pane(&pane));
    d.until("one shown", |d| d.viewport_effective().is_some_and(|(_, v)| v == ["mike"]));

    // The last chat closed: the list only, `visible: []`, follows kept.
    d.engine.with_panes(|p| p.set_active_session(""));
    d.until("nothing shown", |d| d.viewport_effective().is_some_and(|(l, v)| v.is_empty() && !l.is_empty()));
    assert_eq!(posts(&host).last().unwrap()["body"]["visible"], json!([]));
    assert_eq!(events_queries(&host).len(), streams, "every change on the open stream");
    // A reconnect would open with the screen as it is now.
    assert_eq!(d.engine.viewport_effective().unwrap().1, Vec::<String>::new());
}

#[test]
fn rapid_changes_collapse_to_one_change_in_flight_ending_on_the_last() {
    let host = viewport_host("viewport-rapid");
    let mut d = showing_rachel(&host);
    let before = posts(&host).len();
    // Three screens within one pump: only the last is asked for.
    d.engine.select("mike");
    d.engine.select("rachel");
    d.engine.select("mike");
    d.until("mike shown", |d| d.viewport_effective().is_some_and(|(_, v)| v == ["mike"]));
    d.settle(Duration::from_millis(300));
    let made: Vec<Value> = posts(&host).into_iter().skip(before).collect();
    assert_eq!(made.len(), 1, "one change for the burst: {made:?}");
    assert_eq!(made[0]["body"]["visible"], json!(["mike"]));
}

#[test]
fn a_preview_patches_the_row_without_reading_history_and_showing_the_chat_catches_up() {
    let host = viewport_host("viewport-preview");
    let mut d = showing_rachel(&host);
    // mike was read once, then left for rachel: held, but hidden.
    d.engine.select("mike");
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1) && !e.log_pending("mike"));
    d.engine.select("rachel");
    d.until("rachel shown again", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.settle(Duration::from_millis(200));
    let cursor = d.engine.conversation("mike").unwrap().latest_revision();
    let reads = log_reads(&host, "mike").len();
    let unread = d.engine.roster().find("mike").unwrap().unread;

    // mike works out of view: its rows grow (no transcript-updated reaches a
    // background chat) and the list hears one preview.
    host.control("/__control/upsert", json!({"session": "mike", "turns": [
        {"id": "m-bg", "role": "assistant", "text": "Done in the background"}]}));
    let head = d.engine.conversation("mike").unwrap().latest_revision() + 10;
    host.control("/__control/preview", json!({"session": "mike", "fields": {
        "head_revision": head, "last_message": "Done in the background", "last_message_id": "m-bg",
        "busy": false, "latest_state": "idle", "latest_state_ts": 9_000_000_000_000i64, "status_text": "", "last_activity": 9_000}}));
    d.until("the row moved", |e| e.roster().find("mike").is_some_and(|a| a.last_message == "Done in the background"));
    d.settle(Duration::from_millis(400));
    let row = d.engine.roster().find("mike").unwrap();
    assert_eq!(row.head_revision, head, "the row's head is the preview's");
    assert_eq!(row.unread, unread, "a preview never marks a row read or unread");
    assert_eq!(d.engine.conversation("mike").unwrap().latest_revision(), cursor, "the timeline's cursor is its own");
    assert_eq!(log_reads(&host, "mike").len(), reads, "no /log for a preview or a hidden chat");

    // Shown again: the stream applies it, then the chat reads from its own
    // revision (not the preview's head) and its live state anew.
    let live_before = live_reads(&host, "mike");
    d.engine.select("mike");
    d.until("the background row", |e| e.conversation("mike").is_some_and(|c| c.rows().iter().any(|m| m.id == "m-bg")));
    let read = log_reads(&host, "mike").into_iter().nth(reads).expect("one read on showing it");
    assert_eq!(read["query"]["after_revision"], json!(cursor.to_string()), "a delta from the cached revision: {read}");
    d.until("its live state", |_| live_reads(&host, "mike") > live_before);
}

#[test]
fn a_hidden_chats_delivery_hint_waits_and_is_read_as_a_tail_when_shown() {
    let host = viewport_host("viewport-delivery");
    let mut d = showing_rachel(&host);
    d.engine.select("mike");
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1) && !e.log_pending("mike"));
    d.engine.select("rachel");
    d.until("rachel shown again", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.settle(Duration::from_millis(200));
    let reads = log_reads(&host, "mike").len();
    host.control("/__control/delivery", json!({"session": "mike", "client_msg_id": "c-1", "state": "delivered"}));
    d.settle(Duration::from_millis(400));
    assert_eq!(log_reads(&host, "mike").len(), reads, "kept for when mike is shown");
    d.engine.select("mike");
    d.until("the read on showing", |_| log_reads(&host, "mike").len() > reads);
    let read = log_reads(&host, "mike")[reads].clone();
    assert!(read["query"].get("after_revision").is_none(), "the delivery state is re-read whole (a tail): {read}");
}

#[test]
fn a_refused_change_reopens_the_stream_with_the_panes() {
    let host = viewport_host("viewport-refused");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the first stream applies", |e| e.viewport_effective().is_some());
    // A numbered event, so the reopened stream resumes after it.
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    host.control("/__control/event", json!({"type": "agent-roster", "session": "rachel"}));
    d.until("the event", |_| host.requests("GET", "/agents/snapshot").len() > snapshots);
    host.control("/__control/viewport", json!({"on": true, "refuse": true}));
    d.engine.select("mike");
    d.until("the stream reopened for mike", |_| events_queries(&host).iter().any(|q| q.get("visible") == Some(&json!("mike"))));
    d.until("the new stream applies mike", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["mike"]));
    assert!(!posts(&host).is_empty(), "it was asked first");
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1));
    let reopened = host.requests("GET", "/events").last().cloned().unwrap();
    assert!(!reopened["last_event_id"].as_str().unwrap_or_default().is_empty(), "resumed by event id: {reopened}");
}

#[test]
fn a_dropped_stream_comes_back_with_the_screen_and_reads_only_shown_chats() {
    let host = viewport_host("viewport-drop");
    let mut d = showing_rachel(&host);
    d.engine.select("mike");
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1) && !e.log_pending("mike"));
    d.engine.select("rachel");
    d.until("rachel shown again", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.settle(Duration::from_millis(200));
    let mike_reads = log_reads(&host, "mike").len();
    host.control("/__control/outage", json!({"seconds": 0.5}));
    d.until("the drop", |e| !e.connected());
    host.control("/__control/upsert", json!({"session": "mike", "turns": [{"id": "m-gap", "role": "assistant", "text": "while away"}]}));
    host.control("/__control/upsert", json!({"session": "rachel", "turns": [{"id": "r-gap", "role": "assistant", "text": "while away"}]}));
    d.until("live again", |e| e.connected() && e.connection_state() == "live" && !e.outage_active());
    let last = events_queries(&host).last().cloned().unwrap();
    assert_eq!(last["visible"], "rachel", "reopened with the screen: {last}");
    d.until("rachel caught up", |e| e.conversation("rachel").is_some_and(|c| c.rows().iter().any(|m| m.id == "r-gap")));
    d.settle(Duration::from_millis(400));
    assert_eq!(log_reads(&host, "mike").len(), mike_reads, "a hidden chat waits for its pane");
}

#[test]
fn a_refused_change_reconciles_a_chat_that_stays_shown() {
    let host = viewport_host("viewport-refused-kept");
    let mut d = showing_rachel(&host);
    d.engine.with_panes(|p| p.split_active("vertical", "mike"));
    d.until("both shown", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["mike", "rachel"]));
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1) && !e.log_pending("mike"));
    d.settle(Duration::from_millis(300));
    let (live_before, log_before) = (live_reads(&host, "rachel"), log_reads(&host, "rachel").len());
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    let streams = events_queries(&host).len();

    // mike's pane closes and the change is refused: the stream reopens.
    // rachel was shown before and after, yet events between the two
    // streams may be missing for her too.
    host.control("/__control/viewport", json!({"on": true, "refuse": true}));
    let pane = d.engine.panes().active_pane_id().to_owned();
    d.engine.with_panes(|p| p.close_pane(&pane));
    d.until("the new stream applies rachel", |e| events_queries(&host).len() > streams && e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.until("rachel's live state read anew", |_| live_reads(&host, "rachel") > live_before);
    d.until("rachel's history caught up", |_| log_reads(&host, "rachel").len() > log_before);
    d.settle(Duration::from_millis(500));
    assert_eq!(host.requests("GET", "/agents/snapshot").len(), snapshots + 1, "one roster snapshot for the new stream, its nudge included");
    let read = log_reads(&host, "rachel")[log_before].clone();
    assert!(read["query"].get("after_revision").is_some(), "from rachel's own revision: {read}");
}

#[test]
fn panes_changed_while_disconnected_are_reconciled_from_the_new_stream_only() {
    let host = viewport_host("viewport-offline-panes");
    let mut d = showing_rachel(&host);
    d.engine.select("mike");
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1) && !e.log_pending("mike"));
    d.engine.select("rachel");
    d.until("rachel shown again", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.settle(Duration::from_millis(300));

    host.control("/__control/outage", json!({"seconds": 1.0}));
    d.until("the drop", |e| !e.connected());
    let (rachel_live, rachel_log) = (live_reads(&host, "rachel"), log_reads(&host, "rachel").len());
    let (mike_live, mike_log) = (live_reads(&host, "mike"), log_reads(&host, "mike").len());
    // While the stream is down the screen moves from rachel to mike.
    d.engine.select("mike");
    d.until("live again", |e| e.connected() && e.connection_state() == "live" && !e.outage_active());
    d.until("the new stream applies mike", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["mike"]));
    assert_eq!(events_queries(&host).last().unwrap()["visible"], "mike", "opened with the screen as it is now");
    d.until("mike's live state", |_| live_reads(&host, "mike") > mike_live);
    d.until("mike's history", |_| log_reads(&host, "mike").len() > mike_log);
    d.settle(Duration::from_millis(500));
    assert_eq!(live_reads(&host, "rachel"), rachel_live, "no /live from the screen before the drop");
    assert_eq!(log_reads(&host, "rachel").len(), rachel_log, "no /log for a chat hidden meanwhile");
}

#[test]
fn a_new_empty_workspace_stays_empty_through_snapshots_and_a_reconnect() {
    let host = viewport_host("viewport-empty-workspace");
    let mut d = showing_rachel(&host);
    let first_workspace = d.engine.panes().active_workspace().to_owned();

    // A new workspace (Ctrl+T): no chat, the list only.
    d.engine.with_panes(|p| p.create_workspace("Workspace 2"));
    assert_eq!(d.engine.selected_session(), "");
    d.until("nothing shown", |e| e.viewport_effective().is_some_and(|(_, v)| v.is_empty()));
    assert_eq!(posts(&host).last().unwrap()["body"]["visible"], json!([]));
    let selects = host.requests("POST", "/select").len();
    let snapshots = host.requests("GET", "/agents/snapshot").len();

    // A roster change brings a snapshot: the user's empty workspace stays.
    host.control("/__control/add-agent", json!({"session": "nina", "persona": "Nina"}));
    d.until("the snapshot with nina", |e| e.roster().find("nina").is_some());
    d.until("a roster snapshot", |_| host.requests("GET", "/agents/snapshot").len() > snapshots);
    d.settle(Duration::from_millis(400));
    assert_eq!(d.engine.selected_session(), "", "no chat chosen for the user");
    assert_eq!(d.engine.panes().active_session(), "");
    assert_eq!(host.requests("POST", "/select").len(), selects, "no Host focus moved");
    assert_eq!(posts(&host).last().unwrap()["body"]["visible"], json!([]));
    assert_eq!(d.engine.viewport_effective().unwrap().1, Vec::<String>::new());

    // A reconnect, with its own snapshot: still empty.
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    host.control("/__control/outage", json!({"seconds": 0.5}));
    d.until("the drop", |e| !e.connected());
    d.until("live again", |e| e.connected() && e.connection_state() == "live" && !e.outage_active());
    d.until("the reconnect's snapshot", |_| host.requests("GET", "/agents/snapshot").len() > snapshots);
    d.settle(Duration::from_millis(400));
    assert_eq!(d.engine.selected_session(), "");
    assert_eq!(d.engine.panes().active_session(), "");
    assert_eq!(events_queries(&host).last().unwrap()["visible"], "", "reopened with the list only");
    assert_eq!(host.requests("POST", "/select").len(), selects);

    // The user picks a chat: it opens in the new workspace.
    d.engine.select("mike");
    d.until("mike shown", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["mike"]));
    assert_eq!(d.engine.panes().active_session(), "mike");

    // Back to the first workspace: its chat is the selection again, and a
    // snapshot after that keeps it.
    d.engine.with_panes(|p| p.switch_workspace(&first_workspace));
    assert_eq!(d.engine.selected_session(), "rachel");
    d.until("rachel shown", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    host.control("/__control/add-agent", json!({"session": "omar", "persona": "Omar"}));
    d.until("the snapshot with omar", |e| e.roster().find("omar").is_some());
    d.settle(Duration::from_millis(300));
    assert_eq!(d.engine.selected_session(), "rachel");
}

#[test]
fn a_lost_chat_is_replaced_after_an_empty_workspace_was_left() {
    let host = viewport_host("viewport-empty-then-lost");
    let mut d = showing_rachel(&host);
    let first_workspace = d.engine.panes().active_workspace().to_owned();
    d.engine.with_panes(|p| p.create_workspace("Workspace 2"));
    d.until("nothing shown", |e| e.viewport_effective().is_some_and(|(_, v)| v.is_empty()));
    // Leaving the empty workspace ends the choice: a chat that goes away
    // is replaced by another, as before.
    d.engine.with_panes(|p| p.switch_workspace(&first_workspace));
    assert_eq!(d.engine.selected_session(), "rachel");
    d.engine.archive_agent("rachel");
    d.until("a chat chosen in place of the archived one", |e| !e.selected_session().is_empty() && e.selected_session() != "rachel");
}

#[test]
fn an_outage_ends_only_after_the_new_stream_named_its_sets_and_its_shown_chats_caught_up() {
    let host = viewport_host("viewport-outage");
    let mut d = showing_rachel(&host);
    d.engine.set_outage_grace(Duration::from_millis(1500));
    // mike read once, then hidden: held and followed, but not shown.
    d.engine.select("mike");
    d.until("mike's log", |e| e.conversation("mike").is_some_and(|c| c.len() >= 1) && !e.log_pending("mike"));
    d.engine.select("rachel");
    d.until("rachel shown again", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));
    d.settle(Duration::from_millis(300));

    // The next streams hold their opening: connected, but not yet named.
    host.control("/__control/viewport", json!({"on": true, "hold": true}));
    host.control("/__control/outage", json!({"seconds": 0.3}));
    d.until("the drop", |e| !e.connected());
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    let (rachel_live, rachel_log) = (live_reads(&host, "rachel"), log_reads(&host, "rachel").len());
    let (mike_live, mike_log) = (live_reads(&host, "mike"), log_reads(&host, "mike").len());
    host.control("/__control/upsert", json!({"session": "rachel", "turns": [{"id": "r-gap", "role": "assistant", "text": "while away"}]}));
    host.control("/__control/upsert", json!({"session": "mike", "turns": [{"id": "m-gap", "role": "assistant", "text": "while away"}]}));
    d.until("the stream is back", |e| e.connected());
    // No round, and no read from the sets before the drop, until the
    // stream names what it shows; the grace runs out meanwhile.
    d.until("out of reach while the stream has not named itself", |e| e.connection_state() == "reconnecting");
    assert!(d.engine.connected() && d.engine.outage_active() && !d.engine.outage_catching_up());
    assert_eq!(host.requests("GET", "/agents/snapshot").len(), snapshots, "no roster before the stream's opening");
    assert_eq!(live_reads(&host, "rachel"), rachel_live, "no /live before the stream's opening");
    assert_eq!(log_reads(&host, "rachel").len(), rachel_log, "no /log before the stream's opening");

    // The opening comes while rachel's first read fails on the way: the
    // round reads it again by itself, and only then is the Host live.
    host.control("/__control/fail", json!({"path": "/log", "status": 502, "count": 1}));
    host.control("/__control/viewport", json!({"on": true, "hold": false}));
    d.until_within("caught up and live", Duration::from_secs(15), |e, _| {
        if e.connection_state() == "live" {
            assert!(e.conversation("rachel").is_some_and(|c| c.rows().iter().any(|m| m.id == "r-gap")), "rachel caught up before it said live");
        }
        e.connection_state() == "live" && !e.outage_active()
    });
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    assert_ne!(d.engine.roster().display_state("rachel").as_deref(), Some("offline"));
    d.settle(Duration::from_millis(500));
    assert_eq!(host.requests("GET", "/agents/snapshot").len(), snapshots + 1, "one roster read for the round and the stream's nudge");
    assert_eq!(live_reads(&host, "rachel"), rachel_live + 1, "one /live at the boundary");
    let reads: Vec<Value> = log_reads(&host, "rachel").into_iter().skip(rachel_log).collect();
    assert_eq!(reads.len(), 2, "the read that failed and its one retry: {reads:?}");
    assert!(reads.iter().all(|r| r["query"].get("after_revision").is_some()), "from rachel's own revision: {reads:?}");
    assert_eq!(live_reads(&host, "mike"), mike_live, "a hidden chat reads no /live");
    assert_eq!(log_reads(&host, "mike").len(), mike_log, "a hidden chat reads no /log");
}
