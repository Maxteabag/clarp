//! What this Host's own turns used: the tally, and what an app's usage screen is told.
//!
//! When a turn ends, its agent's thread says here what the tool said the turn took in and
//! gave out ([`Tally::ended`]). It is kept with the fleet, each turn under a name of its
//! own, so that a turn that is told twice (a Host that starts again tells the last turn of
//! each agent once more) is counted once; what is older than the longest window is let go.
//! The use belongs to the Host and not to an agent: an agent that is removed takes none of
//! it along.
//!
//! `GET /backend-usage` answers with the sums ([`clarp_kernel::usage`], the Python Host's
//! rule) for each tool this Host runs. That is what was spent through this Host. How much
//! of an account is left is another thing. This Host asks no provider for it; where a tool
//! says it of itself (Codex, in its conversation's record, read when a turn of it ends:
//! [`crate::quota`]), what it said is shown beside the sums, and for a tool that says
//! nothing the answer says that nobody was asked.

use std::sync::Arc;

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::conversation::LimitsSaid;
use clarp_kernel::usage::{self, Used};
use clarp_ports::clock::Clock;
use serde_json::{Map, Value, json};
use tokio::sync::mpsc;

use crate::edge::App;
use crate::fleet::FleetHandle;
use crate::hub::Hub;
use crate::quota;

/// The kind of thing a turn's use is, among what is left with the fleet.
const KIND: &str = "used";
/// And the kind of the moment from which this Host has counted.
const SINCE: &str = "counted-since";
/// The tools this Host runs, in the order an app lists them.
const TOOLS: [&str; 3] = ["claude", "codex", "agy"];
/// After how many turns what is too old to count is looked for and let go.
const LOOKED_OVER_EVERY: u32 = 200;
/// What is said where an app looks for how much of an account is left.
const NOT_ASKED: &str =
    "This Host counts what its own turns used and asks no provider how much of an account is left";

/// What is on its way to whoever keeps count.
#[derive(Debug, PartialEq)]
pub enum Told {
    /// What a turn used, and the name it is kept under.
    Used(String, Used),
    /// A turn of a tool ended this way, in this session of the tool's own: what the tool
    /// wrote of its account meanwhile is to be read.
    Over {
        tool: String,
        session: String,
        how: quota::Ending,
        /// What the tool said of its account's windows during the turn, where it said so.
        said: Option<LimitsSaid>,
    },
}

/// The way to the tally, for an agent's thread.
#[derive(Clone)]
pub struct Tally {
    tx: mpsc::UnboundedSender<Told>,
}

/// The way to the tally and what is said into it, for whoever keeps it once the fleet runs.
pub fn tally() -> (Tally, mpsc::UnboundedReceiver<Told>) {
    let (tx, rx) = mpsc::unbounded_channel();
    (Tally { tx }, rx)
}

impl Tally {
    /// A turn of an agent ended, and its tool said what it used. Does not wait.
    pub fn ended(&self, agent_id: &str, turn: u64, used: Used) {
        // Named by when it ended first, so that the oldest are found first.
        let name = format!("{:013}-{agent_id}-{turn}", used.at_ms.max(0));
        let _ = self.tx.send(Told::Used(name, used));
    }

    /// A turn of a tool ended in a session of the tool's own, whatever became of the turn:
    /// a tool that writes what is left of its account wrote it there. Does not wait.
    pub fn over(&self, tool: &str, session: &str, how: quota::Ending, said: Option<LimitsSaid>) {
        let (tool, session) = (tool.to_string(), session.to_string());
        let _ = self.tx.send(Told::Over {
            tool,
            session,
            how,
            said,
        });
    }
}

/// What the one that keeps count works with, beside the fleet.
pub struct Works {
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub hub: Arc<Hub>,
    /// What the tools said is left of their accounts.
    pub limits: Arc<quota::Desk>,
    /// Which Host this is.
    pub server_id: String,
}

/// The name under which everything is kept that ended before this moment.
fn before(at_ms: i64) -> String {
    format!("{:013}", at_ms.max(0))
}

/// From when this Host has counted what turns cost: the moment it first kept count, which
/// is kept from then on. This Host has told a turn's own share of a session's cost for as
/// long as it has counted, so every cost it keeps is one turn's.
async fn since(fleet: &FleetHandle, now_ms: i64) -> i64 {
    let kept = fleet.kept_all(SINCE).await.unwrap_or_default();
    let read = |written: &Vec<u8>| std::str::from_utf8(written).ok()?.parse::<i64>().ok();
    if let Some(at) = kept.first().and_then(|(_, written)| read(written)) {
        return at;
    }
    let written = now_ms.to_string().into_bytes();
    let _ = fleet.keep(SINCE, "at".to_string(), Some(written)).await;
    now_ms
}

/// Keeps what turns used with the fleet as it is told, and now and then lets go of what
/// ended longer ago than the longest window; and has what a tool wrote of its account read
/// when a turn of the tool is over. Ends when no agent can tell any more.
pub async fn keep(fleet: FleetHandle, mut told: mpsc::UnboundedReceiver<Told>, works: Works) {
    let clock = &works.clock;
    let home = crate::config::home();
    since(&fleet, clock.now_ms()).await;
    // The first thing told is followed by a look for what is too old.
    let mut since_looked = LOOKED_OVER_EVERY;
    while let Some(told) = told.recv().await {
        let (name, used) = match told {
            Told::Used(name, used) => (name, used),
            Told::Over {
                tool,
                session,
                how,
                said,
            } => {
                let with = quota::With {
                    fleet: &fleet,
                    hub: &works.hub,
                    server_id: &works.server_id,
                    home: &home,
                    now_ms: clock.now_ms(),
                };
                let ended = works.limits.turn_ended(with, &tool, &session, how, said);
                ended.await;
                continue;
            }
        };
        let Ok(written) = serde_json::to_vec(&used) else {
            continue;
        };
        let _ = fleet.keep(KIND, name, Some(written)).await;
        since_looked += 1;
        if since_looked < LOOKED_OVER_EVERY {
            continue;
        }
        since_looked = 0;
        let too_old = before(clock.now_ms() - usage::KEPT_FOR_MS);
        // Kept in the order of their names, which begin with when they ended.
        for (name, _) in fleet.kept_all(KIND).await.unwrap_or_default() {
            if name >= too_old {
                break;
            }
            let _ = fleet.keep(KIND, name, None).await;
        }
    }
}

/// `GET /backend-usage`: what this Host's turns used on each tool, over the last five hours
/// and the last seven days, and what a tool said is left of its account, in the Python
/// Host's fields.
async fn told(State(app): State<App>) -> Json<Value> {
    let now = app.clock.now_ms();
    let home = crate::config::home();
    let with = quota::With {
        fleet: &app.fleet,
        hub: &app.hub,
        server_id: &app.server_id,
        home: &home,
        now_ms: now,
    };
    let left = app.limits.asked(with, quota::CODEX).await;
    let of_claude = app.limits.asked_claude(with).await;
    let kept = app.fleet.kept_all(KIND).await.unwrap_or_default();
    let kept: Vec<Used> = kept
        .iter()
        .filter_map(|(_, written)| serde_json::from_slice(written).ok())
        .collect();
    let since = since(&app.fleet, now).await;
    let backends: Vec<Value> = TOOLS
        .iter()
        .map(|tool| {
            let totals = usage::totals(&kept, tool, (now, since));
            let ended_at = totals["last_turn_at"]
                .as_i64()
                .map(crate::wire::to_the_second);
            let counted = usage::row(tool, &totals, now, ended_at);
            // A tool that said what is left of its account: that is its row, with the sums
            // of this Host's own counting beside it.
            let said =
                Some(&left.row).filter(|row| *tool == quota::CODEX && row["source"] != "none");
            match said.cloned() {
                Some(mut row) => {
                    row["totals"] = counted["totals"].clone();
                    row
                }
                None => counted,
            }
        })
        .collect();
    let providers: Map<String, Value> = TOOLS
        .iter()
        .map(|tool| {
            let provider = if *tool == quota::CODEX {
                left.provider.clone()
            } else if *tool == quota::CLAUDE {
                of_claude.clone()
            } else {
                json!({
                    "freshness": "unknown", "observed_at": null, "provider_id": tool,
                    "provider_instance_id": format!("{}:{tool}", app.server_id),
                    "source": { "detail": NOT_ASKED, "kind": "unavailable" },
                    "windows": [],
                })
            };
            ((*tool).to_string(), provider)
        })
        .collect();
    Json(json!({
        "backends": backends,
        "capability_catalog_schema_version": 2,
        "computer_id": app.server_id,
        "limit_events": left.events,
        "observed_at": crate::wire::iso(now),
        "providers": providers,
        "schema_version": 1,
    }))
}

pub fn routes() -> Router<App> {
    Router::new().route("/backend-usage", get(told))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn the_newest_turns_moment_is_written_as_the_python_host_writes_it() {
        let vectors: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/usage-vectors.json"
        ))
        .unwrap();
        let mut moments = 0;
        for story in vectors["stories"].as_array().unwrap() {
            for asked in story["asked"].as_array().unwrap() {
                let Some(at) = asked["totals"]["claude"]["last_turn_at"].as_i64() else {
                    continue;
                };
                assert_eq!(
                    asked["row"]["fetched_at"],
                    crate::wire::to_the_second(at),
                    "{}",
                    story["name"]
                );
                moments += 1;
            }
        }
        assert_eq!(moments, 13);
    }

    #[test]
    fn a_turns_use_is_kept_under_a_name_that_begins_with_when_it_ended() {
        let (tally, mut told) = tally();
        let tokens = (Some(5), Some(7));
        let used = Used::said("claude", 1_800_000_000_123, tokens, None, false).unwrap();
        tally.ended("a1b2", 9, used.clone());
        tally.ended("a1b2", 9, used);
        let mut named = || match told.try_recv().unwrap() {
            Told::Used(name, _) => name,
            other => panic!("{other:?}"),
        };
        let (first, again) = (named(), named());
        assert_eq!(first, "1800000000123-a1b2-9");
        assert_eq!(first, again, "told twice, it is one name: counted once");
        assert!(before(1_800_000_000_124) > first && before(1_800_000_000_123) < first);
        assert_eq!(before(-5), "0000000000000");
        // And a turn that is over says in which session of its tool it was.
        tally.over("codex", "a-thread", quota::Ending::Completed(7), None);
        let over = Told::Over {
            tool: "codex".to_string(),
            session: "a-thread".to_string(),
            how: quota::Ending::Completed(7),
            said: None,
        };
        assert_eq!(told.try_recv().unwrap(), over);
    }
}
