//! The Host out of reach for a moment (`clarp_core::outage`): a restart
//! behind a gateway answering 502, or a dropped stream. The engine keeps
//! what it shows, keeps trying on its own, and says nothing for the grace;
//! after it, the list says `reconnecting` and its rows `offline`, as for any
//! lost stream. The first answer ends it: the stream reopens from its last
//! event id, and the list, the open chats' `/log` and their `/live` are read
//! again so what happened meanwhile shows once, in order.
//!
//! A `host-updating` line the Host sent before it went is left as it is:
//! the rows say it from the first moment, never a gateway's words.

use std::time::Instant;

use clarp_core::conversation::Conversation;
use clarp_core::outage::transient;
use clarp_core::roster::Roster;

use crate::{Engine, Message};

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
    /// The open chats are read again once the Host answers.
    pub(crate) reconcile: bool,
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

    /// Whether the Host is failing to answer now, said or not yet.
    pub fn outage_active(&self) -> bool {
        self.outage.outage.active()
    }

    /// A failure on the way to the Host. Returns whether it stays quiet: the
    /// grace runs (from the first failure), or the outage already shows.
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
            "Engine: the Host has not answered for {} ms; showing it as out of reach ({})",
            self.outage.outage.elapsed(now).as_millis(),
            self.outage.outage.detail()
        );
        self.show_out_of_reach();
    }

    /// The calm treatment: before the Host's `/server-info` ever answered on
    /// this connection, the connection page's `offline` and why; after,
    /// `reconnecting` and every row `offline`. Trying goes on either way.
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
        self.mutate_roster(Roster::mark_transport_unavailable);
        let sessions: Vec<String> = self.conversations.keys().cloned().collect();
        for session in sessions {
            self.with_conversation(&session, Conversation::clear_running_activity);
        }
    }

    /// The Host answered: an outage, if any, ends, and what it hid is read
    /// again. True when there was one.
    pub(crate) fn outage_recovered(&mut self) -> bool {
        let Some(recovery) = self.outage.outage.recovered(Instant::now()) else { return false };
        self.outage.epoch += 1;
        self.outage.retry += 1;
        self.outage.server_info_attempt = 0;
        eprintln!(
            "Engine: the Host answers again after {} ms ({} failures; out of reach {})",
            recovery.after.as_millis(),
            recovery.failures,
            if recovery.shown { "was shown" } else { "was never shown" }
        );
        if self.error_is_transport {
            self.set_error("");
            self.error_is_transport = false;
        }
        true
    }

    /// A different Host, or none: its outage is not this one's.
    pub(crate) fn reset_outage(&mut self) {
        self.outage.outage = clarp_core::outage::Outage::with_grace(self.outage.outage.grace());
        self.outage.epoch += 1;
        self.outage.retry += 1;
        self.outage.server_info_attempt = 0;
        self.outage.reconcile = false;
    }

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
            let delay = clarp_core::endpoint::SseTiming::default().jittered_delay(attempt);
            self.after(delay, Message::ServerInfoRetryDue(self.outage.retry));
            return true;
        }
        if let Some(session) = tag.strip_prefix("log-").and_then(|rest| rest.split_once(':')).map(|(_, s)| s.to_owned()) {
            self.outage_failed(detail);
            self.log_in_flight.remove(&session);
            self.pending_log_mode.remove(&session);
            self.with_conversation(&session, |c| c.set_loading(false));
            self.outage.reconcile = true;
            return true;
        }
        if tag.starts_with("select:") {
            // The Host is told again with the next selection; nothing to show.
            return true;
        }
        false
    }

    pub(crate) fn server_info_retry_due(&mut self, epoch: u64) {
        if epoch != self.outage.retry {
            return;
        }
        self.outage.retry += 1;
        self.api.get("server-info", "/server-info", &[]);
    }

    /// The stream is back (or a read answered while it ran): an outage ends,
    /// and after a drop the open chats catch up.
    pub(crate) fn host_answered(&mut self) {
        self.outage_recovered();
        if std::mem::take(&mut self.outage.reconcile) {
            self.reconcile_open_chats();
        }
    }

    /// Every chat this desktop holds reads what it missed: a delta after its
    /// last row (in order, each row once), the tail where it has none yet;
    /// and live views whose snapshot failed ask for it again.
    fn reconcile_open_chats(&mut self) {
        let mut sessions: Vec<String> = self.conversations.keys().cloned().collect();
        sessions.sort();
        for session in sessions {
            let revision = self.conversations.get(&session).map_or(0, Conversation::latest_revision);
            if revision > 0 {
                self.request_delta(&session);
            } else if session == self.selected || self.live_holds(&session) {
                self.request_tail(&session, false);
            }
        }
        self.live_retry_failed();
    }
}
