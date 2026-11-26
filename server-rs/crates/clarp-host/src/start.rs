//! How far this Host's start has come, and what a request waits for before it is answered
//! (issue 935).
//!
//! A release stops one Host and starts the next. The next one answers its liveness at once
//! (`GET /server-info`, `GET /host/start`), so the apps and the installer know it is there.
//! What is asked of one named agent (`/log`, `/live`, `/send`) is asked of that agent's own
//! thread, which opens the agent before it answers anything: such a request waits for that agent
//! alone, however many others are still being opened. The roster and the event stream speak of
//! every agent, and wait until every agent is open: an agent that is still being opened has no
//! row of its own yet (no running turn, revision zero), and a row like that must never stand in
//! for one whose turn runs. Everything else waits until the whole start is done, its recovery of
//! the gallery, the portraits and the calls included, so that no route serves what is not yet
//! recovered. A request that waits is held, not refused.

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
    /// Every agent is open: its row is its own.
    Agents,
    /// The whole start is done.
    Started,
}

impl Stage {
    /// The word `GET /host/start` says it with.
    pub fn word(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Agents => "agents",
            Self::Started => "started",
        }
    }
}

/// Where this Host's start stands. A Host made without a start of its own (a test's) is
/// started.
pub struct Readiness {
    stage: watch::Sender<Stage>,
}

impl Default for Readiness {
    fn default() -> Self {
        Self {
            stage: watch::Sender::new(Stage::Started),
        }
    }
}

impl Readiness {
    /// A start that has just begun.
    pub fn starting() -> Self {
        Self {
            stage: watch::Sender::new(Stage::Starting),
        }
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
        // Answered by the named agent's own thread, once that agent is open.
        "/live" | "/log" | "/send" => Stage::Starting,
        "/agents/snapshot" | "/events" => Stage::Agents,
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
    fn liveness_and_a_named_chat_at_once_the_roster_once_every_agent_is_open_the_rest_started() {
        for path in ["/server-info", "/host/start", "/live", "/log", "/send"] {
            assert_eq!(needs(path), Stage::Starting, "{path}");
        }
        for path in ["/agents/snapshot", "/events"] {
            assert_eq!(needs(path), Stage::Agents, "{path}");
        }
        for path in ["/gallery", "/calls", "/janitors", "/media", "/agents/snapshot/x"] {
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
    }
}
