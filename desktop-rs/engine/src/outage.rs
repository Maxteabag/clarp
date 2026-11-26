//! The Host out of reach for a moment (`clarp_core::outage`): a restart
//! behind a gateway answering 502, or a dropped stream. The engine keeps
//! what it shows, keeps trying on its own, and says nothing for the grace;
//! after it, the list says `reconnecting` and its rows `offline`, as for any
//! lost stream. The grace is counted once from the first failure, across
//! every reopening and read retry, until recovery is complete.
//!
//! Recovery is complete only when the stream is back AND what it missed has
//! been read again in one catch-up round: a fresh roster (a snapshot asked
//! after the round began), and for the selected chat and every chat shown in
//! a pane, its `/log` (the delta after its last row, or its tail) and its
//! `/live` snapshot where it has a live view. Only then does the out-of-reach
//! state end. Each round has a generation: a second drop ends the round, and
//! answers to requests from before it (an older snapshot generation, a
//! `/log` sent before the round, a `/live` still outstanding) never complete
//! it. A stream that reopens alone does not end the outage.
//!
//! Following the panes (`viewport.rs`), a reopened stream is not yet
//! caught up even with its reads: until its `stream` event names the sets
//! the Host applies, no chat counts as shown. The round starts there, with
//! the stream's own reconciliation as its reads (the roster once, `GET
//! /live` and `/log` for every chat streamed in full), and follows the sets
//! a `live-changed` applies while it runs. A stream replaced after a refused
//! change ends the round; the new one starts the next.
//!
//! A `/log` read that fails on the way to the Host while the stream is up
//! tries again by itself on a capped, jittered backoff; it does not wait for
//! the next snapshot. `/select` (focus, a write) is not sent again: a failure
//! on the way is only logged, and the next selection tells the Host.
//!
//! A `host-updating` line the Host sent before it went is left as it is:
//! the rows say it from the first moment, never a gateway's words.

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use clarp_core::conversation::Conversation;
use clarp_core::endpoint::SseTiming;
use clarp_core::outage::transient;
use clarp_core::roster::Roster;

use crate::{Engine, Message};

/// One catch-up round after the Host answers again.
pub(crate) struct CatchUp {
    generation: u64,
    started: Instant,
    /// Snapshots of this generation or older were asked before the round.
    snapshot_after: u64,
    roster: bool,
    /// Snapshot tries that failed on the way within the round.
    snapshot_attempt: u32,
    /// `/log` requests numbered above this were sent within the round.
    log_after: u64,
    /// Chats whose `/log` has not caught up in this round.
    logs: HashSet<String>,
    /// Chats whose `/live` has not answered in this round.
    live: HashSet<String>,
}

#[derive(Default)]
pub(crate) struct OutageState {
    pub(crate) outage: clarp_core::outage::Outage,
    /// Bumped when an outage ends or the Host changes: a timer of the one
    /// before does nothing.
    pub(crate) epoch: u64,
    /// `/server-info` tries since it last answered.
    pub(crate) server_info_attempt: u32,
    /// Bumped by every new `/server-info` request: a retry waiting for an
    /// older one does nothing.
    pub(crate) retry: u64,
    /// Bumped by every drop, round start and reset: fences catch-up rounds.
    pub(crate) generation: u64,
    pub(crate) catch_up: Option<CatchUp>,
    /// Every `/log` request's number, and the one each chat waits for.
    pub(crate) log_seq: u64,
    pub(crate) log_sent: HashMap<String, (u64, bool)>,
    /// A chat's `/log` retry after a failure on the way: its tries and the
    /// token of the retry that is due.
    pub(crate) log_retry: HashMap<String, (u32, u64)>,
    pub(crate) log_retry_token: u64,
    /// When the stream came back in the current round (for the log line).
    pub(crate) reopened: Option<Instant>,
}

impl Engine {
    /// The quiet time after the Host stops answering (15 s; tests and
    /// checks shorten it). Takes effect from the next outage.
    pub fn set_outage_grace(&mut self, grace: std::time::Duration) {
        if !self.outage.outage.active() {
            self.outage.outage = clarp_core::outage::Outage::with_grace(grace);
        }
    }

    /// Whether the Host shows as out of reach (an outage past its grace).
    pub fn outage_shown(&self) -> bool {
        self.outage.outage.shown()
    }

    /// Whether the Host is failing to answer now, said or not yet, or is
    /// back but has not caught up yet.
    pub fn outage_active(&self) -> bool {
        self.outage.outage.active()
    }

    /// Whether a catch-up round runs (the stream is back, reads pending).
    pub fn outage_catching_up(&self) -> bool {
        self.outage.catch_up.as_ref().is_some_and(|c| c.generation == self.outage.generation)
    }

    /// A failure on the way to the Host: the grace starts at the first one.
    pub(crate) fn outage_failed(&mut self, detail: &str) {
        let now = Instant::now();
        if self.outage.outage.failed(now, detail) {
            let grace = self.outage.outage.grace();
            eprintln!("Engine: the Host is not answering ({detail}); trying again quietly for {} ms", grace.as_millis());
            self.after(grace, Message::OutageGraceDue(self.outage.epoch));
        } else if self.outage.outage.shown() {
            // Already said; a reconnect() meanwhile may have set "connecting".
            self.show_out_of_reach();
        }
    }

    pub(crate) fn outage_grace_due(&mut self, epoch: u64) {
        let now = Instant::now();
        if epoch != self.outage.epoch || !self.outage.outage.grace_over(now) {
            return;
        }
        eprintln!(
            "Engine: the Host has not answered (or caught up) for {} ms; showing it as out of reach ({}{})",
            self.outage.outage.elapsed(now).as_millis(),
            self.outage.outage.detail(),
            if self.outage_catching_up() { "; the stream is back, catching up" } else { "" }
        );
        self.show_out_of_reach();
    }

    /// The calm treatment: before the Host's `/server-info` ever answered on
    /// this connection, the connection page's `offline` and why; after,
    /// `reconnecting`, and every row `offline` until a fresh roster came.
    /// Trying goes on either way.
    fn show_out_of_reach(&mut self) {
        if self.connecting || self.connection_state == "offline" {
            let detail = self.outage.outage.detail().to_owned();
            self.set_connecting(false);
            self.set_connection_state("offline");
            self.set_error(&detail);
            self.error_source = "server-info".into();
            self.error_is_transport = true;
            return;
        }
        self.set_connection_state("reconnecting");
        let roster_fresh = self.outage_catching_up() && self.outage.catch_up.as_ref().is_some_and(|c| c.roster);
        if !roster_fresh {
            self.mutate_roster(Roster::mark_transport_unavailable);
        }
        if !self.connected {
            let sessions: Vec<String> = self.conversations.keys().cloned().collect();
            for session in sessions {
                self.with_conversation(&session, Conversation::clear_running_activity);
            }
        }
    }

    /// A different Host, or a new connection: its outage and its rounds are
    /// not this one's (replies to the old endpoint are dropped anyway).
    pub(crate) fn reset_outage(&mut self) {
        self.outage.outage = clarp_core::outage::Outage::with_grace(self.outage.outage.grace());
        self.outage.epoch += 1;
        self.outage.retry += 1;
        self.outage.server_info_attempt = 0;
        self.end_catch_up_round();
        self.outage.log_retry.clear();
        self.outage.log_sent.clear();
    }

    /// reconnect(): requests in flight are dropped, so the round in flight
    /// can never finish; the outage clock (same Host) keeps running.
    pub(crate) fn outage_reconnecting(&mut self) {
        self.outage.retry += 1;
        self.end_catch_up_round();
        self.outage.log_retry.clear();
        self.outage.log_sent.clear();
    }

    fn end_catch_up_round(&mut self) {
        self.outage.generation += 1;
        self.outage.catch_up = None;
        self.outage.reopened = None;
    }

    // ---- the stream ------------------------------------------------------

    /// The stream dropped while meant to run: the round, if any, is over.
    pub(crate) fn outage_stream_dropped(&mut self) {
        self.end_catch_up_round();
        self.outage_failed("event stream dropped");
    }

    /// The stream is up. Without an outage it is live at once; with one, a
    /// catch-up round starts and the outage ends only when it is done.
    pub(crate) fn outage_stream_connected(&mut self) {
        if !self.outage.outage.active() {
            self.set_connection_state("live");
            if self.error_is_transport {
                self.set_error("");
            }
            // Following the panes, the new stream's `stream` event reads the
            // roster once and reconciles the shown chats.
            if !self.viewport_streaming() {
                self.force_snapshot();
            }
            self.live_reconnected();
            self.live_resubscribed();
            return;
        }
        self.outage.reopened = Some(Instant::now());
        let after = self.outage.outage.elapsed(Instant::now()).as_millis();
        if self.viewport_streaming() {
            // What the Host shows on it is known at its `stream` event.
            eprintln!("Engine: the event stream is back after {after} ms; catching up from its opening before saying so");
            return;
        }
        eprintln!("Engine: the event stream is back after {after} ms; catching up before saying so");
        self.begin_catch_up();
    }

    /// A new stream named itself (`viewport.rs`). In an outage, the round
    /// starts here and makes the stream's reads; true when it did.
    pub(crate) fn outage_stream_named(&mut self) -> bool {
        if !self.outage.outage.active() || !self.connected {
            return false;
        }
        self.begin_catch_up();
        true
    }

    /// The stream was replaced after a refused change, with no drop: a
    /// round under way read for the old one. The outage clock keeps
    /// running; the new stream's opening starts the next round.
    pub(crate) fn outage_stream_replaced(&mut self) {
        if self.outage.catch_up.is_some() {
            self.outage.generation += 1;
            self.outage.catch_up = None;
        }
    }

    // ---- the catch-up round ----------------------------------------------

    /// The chats a round must read: the selected one and every one shown in
    /// a pane, on this engine's Host.
    fn catch_up_sessions(&self) -> Vec<String> {
        if self.viewport_streaming() {
            // Following the panes: those the stream now streams in full.
            return self.full_now().into_iter().collect();
        }
        let mut sessions: Vec<String> = self.live_kept_sessions().into_iter().filter(|s| !clarp_core::all_hosts::is_peer_session(s)).collect();
        sessions.sort();
        sessions
    }

    fn begin_catch_up(&mut self) {
        self.outage.generation += 1;
        let generation = self.outage.generation;
        let snapshot_after = self.snapshot_generation;
        let log_after = self.outage.log_seq;
        self.outage.catch_up = Some(CatchUp {
            generation,
            started: Instant::now(),
            snapshot_after,
            roster: false,
            snapshot_attempt: 0,
            log_after,
            logs: HashSet::new(),
            live: HashSet::new(),
        });
        let required = self.catch_up_sessions();
        if self.viewport_streaming() {
            // The stream's own reconciliation is the round's reads; hidden
            // chats read nothing until a pane shows them.
            for session in &required {
                self.outage.log_retry.remove(session);
            }
            let full = required.iter().cloned().collect();
            self.viewport_reconcile(&full);
            let (logs, live) = self.catch_up_reads(&required);
            if let Some(round) = self.outage.catch_up.as_mut() {
                round.logs = logs;
                round.live = live;
            }
            self.finish_catch_up_if_done();
            return;
        }
        self.force_snapshot();
        // Every chat held reads what it missed; the required ones gate the end.
        let mut held: Vec<String> = self.conversations.keys().cloned().collect();
        held.sort();
        for session in held.iter().chain(required.iter().filter(|s| !held.contains(s))) {
            self.outage.log_retry.remove(session);
            let revision = self.conversations.get(session).map_or(0, Conversation::latest_revision);
            if revision > 0 {
                self.request_delta(session);
            } else if required.contains(session) {
                self.request_tail(session, false);
            }
        }
        let logs: HashSet<String> = required.iter().filter(|s| self.outage.log_sent.contains_key(*s) || self.log_in_flight.contains(*s)).cloned().collect();
        let live = self.live_catch_up(&required);
        if let Some(round) = self.outage.catch_up.as_mut() {
            round.logs = logs;
            round.live = live;
        }
        self.finish_catch_up_if_done();
    }

    /// Of `sessions`, those whose `/log` and those whose `/live` a round
    /// waits for, following the panes: a read on its way or waiting for an
    /// earlier one, and the boundary read of every live view.
    fn catch_up_reads(&self, sessions: &[String]) -> (HashSet<String>, HashSet<String>) {
        let logs = sessions
            .iter()
            .filter(|s| self.outage.log_sent.contains_key(*s) || self.log_in_flight.contains(*s) || self.live.viewport.after_read.contains_key(*s))
            .cloned()
            .collect();
        let live = if self.live.enabled { sessions.iter().filter(|s| self.live.views.contains_key(*s)).cloned().collect() } else { HashSet::new() };
        (logs, live)
    }

    /// A `live-changed` applied new sets while a round runs: a chat no
    /// longer streamed in full is not waited for (its reads wait for its
    /// pane), and one that `reconciled` there is.
    pub(crate) fn catch_up_sets_changed(&mut self, full: &std::collections::BTreeSet<String>, reconciled: &[String]) {
        if !self.outage_catching_up() {
            return;
        }
        let (logs, live) = self.catch_up_reads(reconciled);
        if let Some(round) = self.outage.catch_up.as_mut() {
            round.logs.retain(|s| full.contains(s));
            round.live.retain(|s| full.contains(s));
            round.logs.extend(logs);
            round.live.extend(live);
        }
        self.finish_catch_up_if_done();
    }

    /// A snapshot of `generation` was applied.
    pub(crate) fn catch_up_snapshot(&mut self, generation: u64) {
        if !self.outage_catching_up() {
            self.begin_after_read();
            return;
        }
        if let Some(round) = self.outage.catch_up.as_mut()
            && generation > round.snapshot_after
        {
            round.roster = true;
        }
        self.finish_catch_up_if_done();
    }

    /// The round's snapshot failed on the way to the Host: it is asked again
    /// on the stream's capped backoff, not the roster's slower schedule
    /// (whichever is due first runs; the other finds its token spent).
    pub(crate) fn catch_up_snapshot_failed(&mut self, status: u16) {
        if !transient(status) || !self.outage_catching_up() {
            return;
        }
        let Some(round) = self.outage.catch_up.as_mut() else { return };
        let attempt = round.snapshot_attempt;
        round.snapshot_attempt += 1;
        self.after(SseTiming::default().jittered_delay(attempt), Message::SnapshotRetryDue(self.snapshot_retry));
    }

    /// A `/log` request for `session` is sent: it gets the next number.
    pub(crate) fn log_request_sent(&mut self, session: &str, older: bool) {
        self.outage.log_seq += 1;
        self.outage.log_sent.insert(session.to_owned(), (self.outage.log_seq, older));
    }

    /// A `/log` answer for `session` was applied and nothing more is queued
    /// for it.
    pub(crate) fn catch_up_log(&mut self, session: &str, answered: Option<(u64, bool)>) {
        self.outage.log_retry.remove(session);
        if !self.outage_catching_up() {
            self.begin_after_read();
            return;
        }
        let settled = !self.log_in_flight.contains(session) && !self.pending_log_mode.contains_key(session);
        if let (Some(round), Some((seq, older))) = (self.outage.catch_up.as_mut(), answered)
            && settled
            && !older
            && seq > round.log_after
        {
            round.logs.remove(session);
        }
        self.finish_catch_up_if_done();
    }

    /// A `/live` answer for `session` came and none is outstanding for it.
    pub(crate) fn catch_up_live(&mut self, session: &str) {
        if let Some(round) = self.outage.catch_up.as_mut()
            && round.generation == self.outage.generation
        {
            round.live.remove(session);
            self.finish_catch_up_if_done();
        }
    }

    /// A `/live` read failed and none is outstanding: on the way to the Host
    /// it is asked again after a backoff while the round runs; a Host
    /// refusing it (an older Host without the route) has nothing more to
    /// read, so the round does not wait for it.
    pub(crate) fn catch_up_live_failed(&mut self, session: &str, status: u16) {
        if !self.outage_catching_up() || !self.outage.catch_up.as_ref().is_some_and(|r| r.live.contains(session)) {
            return;
        }
        if transient(status) {
            self.outage_failed("GET /live failed on the way to the Host");
            let delay = SseTiming::default().jittered_delay(2);
            self.after(delay, Message::LiveRetryDue { session: session.to_owned(), generation: self.outage.generation });
        } else {
            self.catch_up_live(session);
        }
    }

    /// A read answered while the stream runs, in an outage with no round:
    /// the Host answers again (an outage of reads), so a round starts.
    fn begin_after_read(&mut self) {
        // Following the panes, not before the stream named its sets.
        let named = !self.viewport_streaming() || self.live.viewport.stream_id.is_some();
        if self.outage.outage.active() && self.connected && self.sse.connected() && named {
            self.begin_catch_up();
        }
    }

    fn finish_catch_up_if_done(&mut self) {
        let named = !self.viewport_streaming() || self.live.viewport.stream_id.is_some();
        let done = self.connected
            && named
            && self.outage.catch_up.as_ref().is_some_and(|r| r.generation == self.outage.generation && r.roster && r.logs.is_empty() && r.live.is_empty());
        if !done {
            return;
        }
        let round = self.outage.catch_up.take().expect("a round");
        let now = Instant::now();
        let Some(recovery) = self.outage.outage.recovered(now) else { return };
        self.outage.epoch += 1;
        self.outage.retry += 1;
        self.outage.server_info_attempt = 0;
        let reopened = self.outage.reopened.take().map_or_else(|| "the stream never dropped".to_owned(), |at| format!("the stream back {} ms before", now.duration_since(at).as_millis()));
        eprintln!(
            "Engine: the Host has answered and caught up after {} ms ({} failures; out of reach {}; {}; round {} ms)",
            recovery.after.as_millis(),
            recovery.failures,
            if recovery.shown { "was shown" } else { "was never shown" },
            reopened,
            now.duration_since(round.started).as_millis()
        );
        self.set_connection_state("live");
        if self.error_is_transport {
            self.set_error("");
            self.error_is_transport = false;
        }
    }

    // ---- failed reads ------------------------------------------------------

    /// A request that only reads failed on the way to the Host. Returns true
    /// when it was handled quietly; writes never come here (they say they
    /// failed, and are not sent again by themselves).
    pub(crate) fn outage_read_failed(&mut self, tag: &str, detail: &str, status: u16) -> bool {
        if !transient(status) {
            return false;
        }
        if tag == "server-info" {
            self.outage_failed(detail);
            let attempt = self.outage.server_info_attempt;
            self.outage.server_info_attempt += 1;
            let delay = SseTiming::default().jittered_delay(attempt);
            self.after(delay, Message::ServerInfoRetryDue(self.outage.retry));
            return true;
        }
        if let Some(session) = tag.strip_prefix("log-").and_then(|rest| rest.split_once(':')).map(|(_, s)| s.to_owned()) {
            self.outage_failed(detail);
            self.log_in_flight.remove(&session);
            self.pending_log_mode.remove(&session);
            self.outage.log_sent.remove(&session);
            self.with_conversation(&session, |c| c.set_loading(false));
            self.schedule_log_retry(&session);
            return true;
        }
        if tag.starts_with("select:") {
            // Focus is a write: not sent again; the next selection says it.
            eprintln!("Engine: {tag} did not reach the Host ({detail}); not sent again");
            return true;
        }
        false
    }

    /// The chat's `/log` is read again after a capped, jittered backoff
    /// while the stream runs; with the stream down the catch-up round reads
    /// it once the stream is back.
    fn schedule_log_retry(&mut self, session: &str) {
        if !self.connected {
            return;
        }
        let attempt = self.outage.log_retry.get(session).map_or(0, |(a, _)| *a);
        self.outage.log_retry_token += 1;
        let token = self.outage.log_retry_token;
        self.outage.log_retry.insert(session.to_owned(), (attempt + 1, token));
        let delay = SseTiming::default().jittered_delay(attempt);
        self.after(delay, Message::LogRetryDue { session: session.to_owned(), token });
    }

    pub(crate) fn log_retry_due(&mut self, session: &str, token: u64) {
        if self.outage.log_retry.get(session).map(|(_, t)| *t) != Some(token) || self.log_in_flight.contains(session) {
            return;
        }
        if self.live.viewport.after_read.contains_key(session) {
            // A stream's reconciliation waited for the read that failed: it
            // is this retry (one read, in the stronger mode).
            self.reconcile_log(session, crate::viewport::Read::Delta);
            return;
        }
        let required = self.catch_up_sessions().iter().any(|s| s == session);
        let revision = self.conversations.get(session).map_or(0, Conversation::latest_revision);
        if revision > 0 {
            self.request_delta(session);
        } else if required || self.conversations.contains_key(session) {
            self.request_tail(session, false);
        }
    }

    pub(crate) fn server_info_retry_due(&mut self, attempt: u64) {
        if attempt != self.outage.retry {
            return;
        }
        self.outage.retry += 1;
        self.api.get("server-info", "/server-info", &[]);
    }
}
