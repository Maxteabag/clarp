//! How far this Host's start has come, and what a request waits for before it is answered
//! (issue 935).
//!
//! A release stops one Host and starts the next. The next one answers its liveness at once
//! (`GET /server-info`, `GET /host/start`), so the apps and the installer know it is there.
//! Every agent is read from what its log committed before its own thread opens it
//! (`Actor::glance`), on a connection that writes nothing: from then until it is open, its row,
//! its conversation (`/log`) and its running turn (`/live`) are answered from that reading, as
//! they stood when the Host before stopped, its queue (`/turn-queue`) too. A plain send of the
//! owner's is kept on disk and answered at once, and its agent takes it when it is open
//! (`accepted`); any other send waits for its agent's opening, in its own mailbox, in the order
//! sends came. The roster waits until
//! every agent's row is its own, read or open: a row of an agent that is neither (no running
//! turn, revision zero) must never stand in for one whose turn runs. The event stream answers
//! at once, as it would after a start; only a call's ring on it waits until the calls are
//! recovered, and holds what comes after it on that stream, in order. Everything else waits
//! until the whole start is done, its recovery of the gallery, the portraits and the calls
//! included, so that no route serves what is not yet recovered. A request that waits is held,
//! not refused.

use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::Response;
use tokio::sync::watch;

use crate::edge::App;

/// How far a start has come, in the order it gets there.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Stage {
    /// The Host is there and says so; nothing it keeps is answered yet.
    Starting,
    /// Every agent's row is its own: read from what its log committed, or open. Its own
    /// janitors are in the roster.
    Rows,
    /// Every agent is open.
    Agents,
    /// The whole start is done.
    Started,
}

impl Stage {
    /// The word `GET /host/start` says it with.
    pub fn word(self) -> &'static str {
        match self {
            // The words of contract 210: the roster that answers before its agents are open
            // is not one of them.
            Self::Starting | Self::Rows => "starting",
            Self::Agents => "agents",
            Self::Started => "started",
        }
    }
}

/// Where this Host's start stands. A Host made without a start of its own (a test's) is
/// started.
pub struct Readiness {
    stage: watch::Sender<Stage>,
    /// Whether the calls are recovered: what a call's ring is judged by. The start recovers them
    /// before it waits for every agent to be open, so this comes apart from the stages.
    calls: watch::Sender<bool>,
}

impl Default for Readiness {
    fn default() -> Self {
        Self {
            stage: watch::Sender::new(Stage::Started),
            calls: watch::Sender::new(true),
        }
    }
}

impl Readiness {
    /// A start that has just begun.
    pub fn starting() -> Self {
        Self {
            stage: watch::Sender::new(Stage::Starting),
            calls: watch::Sender::new(false),
        }
    }

    /// The calls are recovered: a ring is shown to whom they say from now on.
    pub fn recovered_calls(&self) {
        self.calls
            .send_if_modified(|now| !std::mem::replace(now, true));
    }

    /// Resolves once the calls are recovered.
    pub async fn calls_recovered(&self) {
        let mut seen = self.calls.subscribe();
        // The sender lives as long as the Host: an error here is a Host that is going.
        let _ = seen.wait_for(|recovered| *recovered).await;
    }

    /// The start has come this far. It never goes back.
    pub fn reach(&self, stage: Stage) {
        self.stage.send_if_modified(|now| {
            let further = *now < stage;
            if further {
                *now = stage;
            }
            further
        });
    }

    pub fn stage(&self) -> Stage {
        *self.stage.borrow()
    }

    /// Resolves once the start has come at least this far.
    pub async fn reached(&self, stage: Stage) {
        let mut seen = self.stage.subscribe();
        // The sender lives as long as the Host: an error here is a Host that is going.
        let _ = seen.wait_for(|now| *now >= stage).await;
    }
}

/// How far the start must have come before a request to this path is answered.
pub fn needs(path: &str) -> Stage {
    match path {
        "/server-info" | "/host/start" => Stage::Starting,
        // Answered from the named agent's glance while it is opened, then by its own thread. A
        // send waits in that agent's mailbox until it is open; one that belongs to a call
        // waits in its handler until the calls are recovered (`sent_from`).
        "/live" | "/log" | "/send" => Stage::Starting,
        // Its queue too, read from its glance with the sends kept for it (`accepted`).
        "/turn-queue" => Stage::Starting,
        "/agents/snapshot" => Stage::Rows,
        // The stream holds a call's ring until the calls are recovered, and whatever comes after
        // it on that stream behind it; what came before it is sent (`edge::Frame`).
        "/events" => Stage::Starting,
        _ => Stage::Started,
    }
}

/// The door every request comes through: it waits, never refuses, until the start has come
/// as far as the request needs.
pub async fn door(State(app): State<App>, request: Request, next: Next) -> Response {
    let needed = needs(request.uri().path());
    if app.start.stage() < needed {
        app.start.reached(needed).await;
    }
    next.run(request).await
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn liveness_a_named_chat_and_the_stream_at_once_the_roster_once_every_row_is_its_own() {
        for path in [
            "/server-info",
            "/host/start",
            "/live",
            "/log",
            "/send",
            "/turn-queue",
            "/events",
        ] {
            assert_eq!(needs(path), Stage::Starting, "{path}");
        }
        assert_eq!(needs("/agents/snapshot"), Stage::Rows);
        for path in [
            "/gallery",
            "/calls",
            "/janitors",
            "/media",
            "/agents/snapshot/x",
        ] {
            assert_eq!(needs(path), Stage::Started, "{path}");
        }
    }

    #[tokio::test]
    async fn a_start_only_goes_forward_and_wakes_who_waits_for_it() {
        let start = std::sync::Arc::new(Readiness::starting());
        let waiting = {
            let start = start.clone();
            tokio::spawn(async move { start.reached(Stage::Agents).await })
        };
        tokio::task::yield_now().await;
        assert!(!waiting.is_finished());
        start.reach(Stage::Started);
        waiting.await.unwrap();
        start.reach(Stage::Agents);
        assert_eq!(start.stage(), Stage::Started);
        assert_eq!(Readiness::default().stage(), Stage::Started);
        // The calls are recovered apart from the stages, once.
        let calls = std::sync::Arc::new(Readiness::starting());
        let ring = {
            let calls = calls.clone();
            tokio::spawn(async move { calls.calls_recovered().await })
        };
        tokio::task::yield_now().await;
        assert!(!ring.is_finished());
        calls.recovered_calls();
        ring.await.unwrap();
        assert_eq!(calls.stage(), Stage::Starting);
        // The roster's own stage is said as the contract's words have it.
        assert_eq!(Stage::Rows.word(), "starting");
        assert!(Stage::Starting < Stage::Rows && Stage::Rows < Stage::Agents);
    }
}
