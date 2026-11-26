//! Whether an agent can answer now, and when it will if it cannot (feature
//! `agent_availability`, issue 918).
//!
//! Every agent row carries `availability {state, text, since_ms, back_at_ms, queue_ahead,
//! last_alive_ms}`: one calm line an app shows under the agent, with the reason and the
//! expected wait. The states, in their order of precedence:
//!
//! - `usage_limit`: no account can take the agent's turn now. A turn that waits for an account
//!   while no other seat would take it (its wait names a reset), or an idle agent whose tool's
//!   account is used up with no seat to move to (its row's `backend_quota` says `exhausted`).
//! - `switching_account`: a turn waits for an account while the Host moves its tool to another
//!   one: after a usage limit, or after a sign-in that ran out.
//! - `restarting`: its process is starting again (an attempt after a failed one, a wait before
//!   the next attempt), or the agent is being opened after the Host started.
//! - `unresponsive`: a turn runs, no tool of it runs, and nothing came of it for
//!   [`UNRESPONSIVE_AFTER_MS`].
//! - `busy`: a turn runs; a new message waits behind `queue_ahead` others.
//! - `ok`: it can answer now.
//!
//! All of it is decided from what the Host already holds ([`decide`]); nothing is asked of the
//! agent. The snapshot's row and the live event are made by the same function, so a patched
//! row equals a fresh read. The live event (`agent-roster` of kind `availability`) is sent by
//! one Host-wide task ([`run`]) when what an app shows changes (state, text, back time, queue),
//! never for a sign of life alone: an agent whose status or queue moved is looked at at once,
//! and one sweep every [`SWEEP_EVERY`] finds the turns that fell silent and the accounts that
//! changed.
//!
//! Just before the Host stops (SIGTERM: an update or an install), every event stream is sent
//! one `{type: "host-updating", text, since_ms, back_at_ms}` as its last frame
//! ([`host_updating`]). The back time is the Host's own last measured restart, else
//! [`RESTART_DEFAULT_MS`]. A snapshot never says the Host is updating: when it answers, it is up.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::{LazyLock, Mutex, PoisonError};
use std::time::Duration;

use clarp_actor::Status;
use clarp_kernel::failures;
use clarp_kernel::turn::{Activity, Outcome, Wait};
use serde_json::{Value, json};

use crate::fleet::Member;

/// A turn that ran this long with nothing coming of it is not responding.
pub const UNRESPONSIVE_AFTER_MS: i64 = 180_000;
/// How often the Host looks at every agent for what time alone changes.
pub const SWEEP_EVERY: Duration = Duration::from_secs(15);
/// How long a turn refused for a usage limit shows the switch to another seat, which takes the
/// next turn: by then the Host has moved the tool.
const SWITCH_SHOWN_MS: i64 = 120_000;
/// How long a turn refused for a usage limit with no reset known shows the limit: a usage window
/// is five hours at most, and the next turn tells again.
const REFUSAL_SHOWN_MS: i64 = 5 * 3_600_000;
/// How long the moves of agents are gathered before they are looked at.
const BATCHED_FOR: Duration = Duration::from_millis(100);
/// How long a tool's process takes to start, until the Host has measured a few of its own.
pub const START_DEFAULT_MS: i64 = 20_000;
/// How long the Host takes to come back after it is told to stop, until it has measured it.
pub const RESTART_DEFAULT_MS: i64 = 45_000;
/// The starts of a tool kept to estimate the next, and how many it takes to trust them.
const STARTS_KEPT: usize = 9;
const STARTS_TRUSTED: usize = 3;
/// A Host that was stopped for longer than this was stopped, not restarted: not measured.
const RESTART_MEASURED_UP_TO_MS: i64 = 15 * 60_000;
const STOPPED_AT: &str = "host-stopped-at-ms";
const RESTART_TOOK: &str = "host-restart-ms";

/// What an app shows of an agent's availability.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Availability {
    pub state: &'static str,
    pub text: String,
    pub since_ms: i64,
    pub back_at_ms: Option<i64>,
    pub queue_ahead: usize,
    pub last_alive_ms: Option<i64>,
}

impl Availability {
    pub fn to_json(&self) -> Value {
        json!({
            "state": self.state,
            "text": self.text,
            "since_ms": self.since_ms,
            "back_at_ms": self.back_at_ms,
            "queue_ahead": self.queue_ahead,
            "last_alive_ms": self.last_alive_ms,
        })
    }

    /// What a live event is sent for when it changes: not the times that only follow along.
    fn shown(&self) -> (&'static str, &str, Option<i64>, usize) {
        (self.state, &self.text, self.back_at_ms, self.queue_ahead)
    }
}

/// What the decision reads of one agent, all of it held by the Host already.
pub struct Facts<'a> {
    pub status: &'a Status,
    /// Why the agent's thread cannot answer at all now (`agent::View::broken`).
    pub broken: Option<&'a str>,
    /// The row's `backend_quota`: what it says of the tool's account.
    pub backend_quota: &'a Value,
    /// The agent runs on a provider of its own, never on the machine's accounts.
    pub bound: bool,
    /// Another seat of its tool has room and this Host moves the tool to it (the Hotseat's
    /// failover, `quota::Rows::spare_seat`).
    pub spare_seat: bool,
    /// The running turn's last sign of life: its tool's last output, else its start.
    pub last_alive_ms: Option<i64>,
    /// How long its tool takes to start ([`start_estimate`]).
    pub start_ms: i64,
    pub created_at_ms: i64,
    /// When this run of the Host started.
    pub host_started_ms: i64,
    pub now_ms: i64,
}

/// Whether the agent can answer now, and if not why and until when.
pub fn decide(facts: &Facts<'_>) -> Availability {
    let status = facts.status;
    let queue_ahead = status.queue_depth;
    let live = status.live.as_ref();
    let at = |state: &'static str, text: String, since_ms: i64, back_at_ms: Option<i64>| {
        at_state(state, text, since_ms, back_at_ms, queue_ahead)
    };
    // The account in use is used up and no other seat would take the turn.
    let used_up = Some(facts.backend_quota)
        .filter(|_| !facts.bound)
        .filter(|row| row["state"] == "exhausted");
    let when = |field: &str| {
        used_up
            .and_then(|row| row[field].as_str())
            .and_then(clarp_kernel::quota::from_text)
    };
    if let Some(live) = live {
        let parked = live.parked.as_ref();
        let account_wait = parked.is_some_and(|parked| parked.wait.is_for_an_account());
        if account_wait || live.held_for_an_account {
            let since = parked.map_or(live.moved_at_ms, |parked| parked.since_ms);
            let sign_in = parked.is_some_and(|parked| parked.kind == failures::AUTH);
            let resets = parked.and_then(|parked| parked.wait.resets_at_ms());
            // A wait that names a reset is one no other seat would take (the pool's word,
            // `Pooled::shown_reset`); one that names none waits while the Host switches.
            if !sign_in && (used_up.is_some() || resets.is_some()) {
                let back = resets.or_else(|| when("resets_at"));
                return at("usage_limit", "Usage limit".into(), since, back);
            }
            let why = if sign_in { "sign-in" } else { "usage limit" };
            return at(
                "switching_account",
                format!("Switching account ({why})"),
                since,
                None,
            );
        }
    } else {
        // Its last turn was refused for a usage limit, whatever its tool: it ended failed with
        // that kind, as a tool the Host keeps no pool of ends it.
        let refused_at = status.last.as_ref().and_then(|last| match &last.outcome {
            Outcome::Failed { reason, kind } => {
                let kind = if kind.is_empty() {
                    failures::classify_error(reason)
                } else {
                    kind
                };
                (kind == failures::USAGE_LIMIT).then_some(last.ended_at_ms)
            }
            _ => None,
        });
        let back = when("resets_at").filter(|at| *at > facts.now_ms);
        let age = |at: i64| facts.now_ms.saturating_sub(at);
        match refused_at {
            // Another seat takes the next turn: the Host is moving the tool to it.
            Some(refused) if facts.spare_seat && !facts.bound => {
                if age(refused) < SWITCH_SHOWN_MS {
                    let text = "Switching account (usage limit)".to_string();
                    return at("switching_account", text, refused, None);
                }
            }
            Some(refused) if used_up.is_some() || age(refused) < REFUSAL_SHOWN_MS => {
                return at("usage_limit", "Usage limit".into(), refused, back);
            }
            _ if used_up.is_some() => {
                let since = when("observed_at").unwrap_or_else(|| rested_since(facts));
                return at("usage_limit", "Usage limit".into(), since, back);
            }
            _ => {}
        }
    }
    if facts.broken.is_some() {
        return at("restarting", "Restarting".into(), facts.host_started_ms, None);
    }
    let Some(live) = live else {
        return at("ok", String::new(), rested_since(facts), None);
    };
    if let Some(parked) = &live.parked {
        let back = match parked.wait {
            Wait::Retry { until_ms, .. } => Some(until_ms.saturating_add(facts.start_ms)),
            Wait::Network { .. } | Wait::Account { .. } => None,
        };
        return at("restarting", "Restarting".into(), parked.since_ms, back);
    }
    if live.starting && (live.failed_attempts > 0 || live.handoff_retry.is_some()) {
        let back = live.moved_at_ms.saturating_add(facts.start_ms);
        return at("restarting", "Restarting".into(), live.moved_at_ms, Some(back));
    }
    let alive = facts.last_alive_ms.filter(|_| !live.starting);
    // Only a turn that thinks or writes can fall silent: a tool that runs is work however
    // quiet (its row says what runs), and so is a wait for an approval or a background task, a
    // compaction, and the tool's own retries at a provider that would not answer.
    let silent_work = live.tools.is_empty()
        && matches!(live.activity, Activity::Thinking | Activity::Responding);
    if let Some(alive) = alive.filter(|_| silent_work && !live.stopping)
        && facts.now_ms.saturating_sub(alive) >= UNRESPONSIVE_AFTER_MS
    {
        let mut shown = at(
            "unresponsive",
            "Not responding".into(),
            alive.saturating_add(UNRESPONSIVE_AFTER_MS),
            None,
        );
        shown.last_alive_ms = Some(alive);
        return shown;
    }
    let text = match queue_ahead {
        0 => "Busy".to_string(),
        1 => "Busy · 1 message ahead of you".to_string(),
        many => format!("Busy · {many} messages ahead of you"),
    };
    let mut shown = at("busy", text, live.started_at_ms, None);
    shown.last_alive_ms = alive;
    shown
}

fn at_state(
    state: &'static str,
    text: String,
    since_ms: i64,
    back_at_ms: Option<i64>,
    queue_ahead: usize,
) -> Availability {
    Availability {
        state,
        text,
        since_ms,
        back_at_ms,
        queue_ahead,
        last_alive_ms: None,
    }
}

/// Since when an agent with no turn has rested: its last turn's end, else its making.
fn rested_since(facts: &Facts<'_>) -> i64 {
    facts
        .status
        .last
        .as_ref()
        .map_or(facts.created_at_ms, |last| last.ended_at_ms)
}

/// The moment a row is made at, and the run of the Host it is made in.
#[derive(Debug, Clone, Copy)]
pub struct At {
    pub now_ms: i64,
    pub host_started_ms: i64,
}

impl At {
    pub fn of(app: &crate::edge::App) -> Self {
        Self {
            now_ms: app.clock.now_ms(),
            host_started_ms: app.hub.started_ms(),
        }
    }
}

/// An agent's availability as its row has it, given what the row says of its tool's account.
pub fn of_member(
    member: &Member,
    view: &crate::agent::View,
    quota: &crate::quota::Rows,
    backend_quota: &Value,
    at: At,
) -> Value {
    of(member, view, quota, backend_quota, at).to_json()
}

fn of(
    member: &Member,
    view: &crate::agent::View,
    quota: &crate::quota::Rows,
    backend_quota: &Value,
    at: At,
) -> Availability {
    decide(&Facts {
        status: &view.status,
        broken: view.broken.as_deref(),
        backend_quota,
        bound: member.record.bound(),
        spare_seat: quota.spare_seat(view.backend, &member.record.model),
        last_alive_ms: member.agent.last_alive_ms(),
        start_ms: start_estimate(view.backend),
        created_at_ms: member.record.created_at_ms,
        host_started_ms: at.host_started_ms,
        now_ms: at.now_ms,
    })
}

/// How long the last starts of each tool took, newest last.
static STARTS: LazyLock<Mutex<HashMap<String, VecDeque<i64>>>> = LazyLock::new(Mutex::default);

/// A turn's process of `backend` started and said its first word `took_ms` after it was begun.
pub(crate) fn started(backend: &str, took_ms: i64) {
    if !(0..=600_000).contains(&took_ms) {
        return;
    }
    let mut starts = STARTS.lock().unwrap_or_else(PoisonError::into_inner);
    let kept = starts.entry(backend.to_owned()).or_default();
    if kept.len() >= STARTS_KEPT {
        kept.pop_front();
    }
    kept.push_back(took_ms);
}

/// How long a start of `backend`'s process takes: the median of its last starts on this Host,
/// once there are a few, else [`START_DEFAULT_MS`].
pub fn start_estimate(backend: &str) -> i64 {
    let starts = STARTS.lock().unwrap_or_else(PoisonError::into_inner);
    starts
        .get(backend)
        .filter(|kept| kept.len() >= STARTS_TRUSTED)
        .map_or(START_DEFAULT_MS, |kept| median(kept))
}

fn median(kept: &VecDeque<i64>) -> i64 {
    let mut sorted: Vec<i64> = kept.iter().copied().collect();
    sorted.sort_unstable();
    sorted[sorted.len() / 2].max(1_000)
}

/// The Host is told to stop: what every event stream is sent as its last frame, with when the
/// Host expects to be back. Notes the moment, so that the next start measures how long it took.
pub fn host_updating(data_dir: &Path, now_ms: i64) -> Value {
    let _ = std::fs::write(data_dir.join(STOPPED_AT), now_ms.to_string());
    let took = read_ms(&data_dir.join(RESTART_TOOK))
        .filter(|took| (1_000..=RESTART_MEASURED_UP_TO_MS).contains(took))
        .unwrap_or(RESTART_DEFAULT_MS);
    json!({
        "type": "host-updating",
        "text": "Host updating",
        "since_ms": now_ms,
        "back_at_ms": now_ms.saturating_add(took),
    })
}

/// The Host listens again: how long it was away since it was told to stop is kept, as the
/// back time of the next stop. A stop longer than a restart is not kept.
pub fn host_started(data_dir: &Path, now_ms: i64) {
    let stopped = data_dir.join(STOPPED_AT);
    let Some(at) = read_ms(&stopped) else {
        return;
    };
    let _ = std::fs::remove_file(&stopped);
    let took = now_ms.saturating_sub(at);
    if (1_000..=RESTART_MEASURED_UP_TO_MS).contains(&took) {
        let _ = std::fs::write(data_dir.join(RESTART_TOOK), took.to_string());
    }
}

fn read_ms(path: &Path) -> Option<i64> {
    std::fs::read_to_string(path).ok()?.trim().parse().ok()
}

/// The live event of an agent's availability.
pub fn event(member: &Member, availability: Value) -> Value {
    json!({
        "type": "agent-roster",
        "kind": "availability",
        "session": member.record.session,
        "agent_id": member.record.agent_id,
        "fields": { "availability": availability },
    })
}

/// Tells the apps when an agent's availability changes: one task for the whole Host. It looks at
/// an agent at once when its status or queue moved (`Hub::availability_moved`), and at every
/// agent every [`SWEEP_EVERY`], for the turns that fell silent and the accounts that changed.
pub fn run(app: &crate::edge::App) {
    let app = app.clone();
    tokio::spawn(async move {
        let mut told: HashMap<String, Availability> = HashMap::new();
        let mut sweep = tokio::time::interval(SWEEP_EVERY);
        sweep.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        // Read at the first sweep, which comes at once, and at every sweep after.
        let mut quota = crate::quota::Rows::unasked();
        // The first look only learns how every agent stands: an app read that in the snapshot.
        let mut first = true;
        loop {
            let moved = tokio::select! {
                _ = sweep.tick() => {
                    quota = crate::quota::rows(&app).await;
                    None
                }
                () = app.hub.availability_wait() => {
                    // What moved in the same breath is looked at together.
                    tokio::time::sleep(BATCHED_FOR).await;
                    Some(app.hub.take_availability_moved())
                }
            };
            let directory = app.fleet.directory();
            let at = At::of(&app);
            let members = directory.members.iter().chain(&directory.janitors);
            let mut seen = HashSet::new();
            for member in members {
                let id = &member.record.agent_id;
                if moved.as_ref().is_some_and(|moved| !moved.contains(id)) {
                    continue;
                }
                let view = member.agent.view();
                let backend_quota = quota.of(view.backend, &member.record.model);
                let now = of(member, &view, &quota, &backend_quota, at);
                if moved.is_none() {
                    seen.insert(id.clone());
                }
                // An agent first seen able to answer has nothing to tell: its row says so.
                let changed = match told.get(id) {
                    Some(before) => before.shown() != now.shown(),
                    None => now.state != "ok",
                };
                if changed && !first {
                    app.hub.publish(event(member, now.to_json()), at.now_ms);
                }
                told.insert(id.clone(), now);
            }
            if moved.is_none() {
                // An agent the Host no longer has is forgotten.
                told.retain(|id, _| seen.contains(id));
                first = false;
            }
        }
    });
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use clarp_actor::LiveStatus;
    use clarp_kernel::turn::{Origin, Parked, TurnId};

    use super::*;

    const NOW: i64 = 1_900_000_000_000;

    fn idle() -> Status {
        Status {
            moved: None,
            live: None,
            last: None,
            interrupted_since_ms: None,
            waiting: Vec::new(),
            queue_depth: 0,
            listed_queue_depth: 0,
            paused: false,
            queue_revision: 0,
            stop_generation: 0,
            head_revision: 0,
            backend_session: None,
            label: String::new(),
            label_at_ms: None,
            resting: None,
            context_tokens: None,
            last_compaction: None,
            completed_at_ms: None,
            answered_by: None,
            arrived: false,
            note: None,
        }
    }

    fn running(started_at_ms: i64) -> LiveStatus {
        LiveStatus {
            turn: TurnId(1),
            message_id: "m-1".into(),
            client_id: "c-1".into(),
            trace_id: None,
            origin: Origin::User,
            from_queue: false,
            starting: false,
            activity: Activity::Thinking,
            started_at_ms,
            moved_at_ms: started_at_ms,
            tool_count: 0,
            tools: Vec::new(),
            stopping: false,
            parked: None,
            handoff_retry: None,
            held_for_an_account: false,
            waits_for_its_model: None,
            failed_attempts: 0,
            waits_for_the_network: false,
            network_retry_at_ms: None,
        }
    }

    fn parked(wait: Wait, kind: &str, since_ms: i64) -> Parked {
        Parked {
            id: TurnId(1),
            message_seq: 1,
            origin: Origin::User,
            accepted_at_ms: since_ms - 10_000,
            started_at_ms: since_ms - 10_000,
            attempts: 1,
            backend_session: None,
            tool_count: 0,
            wait,
            kind: kind.into(),
            reason: String::new(),
            since_ms,
            ran: true,
            account: Default::default(),
            offline: 0,
        }
    }

    fn decided(status: &Status, quota: &Value, alive: Option<i64>) -> Availability {
        decide(&Facts {
            status,
            broken: None,
            backend_quota: quota,
            bound: false,
            spare_seat: false,
            last_alive_ms: alive,
            start_ms: START_DEFAULT_MS,
            created_at_ms: 5,
            host_started_ms: 7,
            now_ms: NOW,
        })
    }

    #[test]
    fn an_idle_agent_can_answer_and_one_at_work_is_busy_behind_its_queue() {
        let ok = decided(&idle(), &Value::Null, None);
        assert_eq!((ok.state, ok.text.as_str(), ok.since_ms), ("ok", "", 5));
        assert_eq!(ok.to_json()["back_at_ms"], Value::Null);
        let mut status = idle();
        status.live = Some(running(NOW - 1_000));
        let busy = decided(&status, &Value::Null, Some(NOW - 500));
        assert_eq!((busy.state, busy.text.as_str()), ("busy", "Busy"));
        assert_eq!(busy.last_alive_ms, Some(NOW - 500));
        status.queue_depth = 1;
        let one = decided(&status, &Value::Null, Some(NOW - 500));
        assert_eq!(one.text, "Busy · 1 message ahead of you");
        status.queue_depth = 25;
        let many = decided(&status, &Value::Null, Some(NOW - 500));
        assert_eq!(
            (many.text.as_str(), many.queue_ahead, many.since_ms),
            ("Busy · 25 messages ahead of you", 25, NOW - 1_000)
        );
    }

    #[test]
    fn a_silent_turn_is_not_responding_after_three_minutes_unless_a_tool_runs() {
        let mut status = idle();
        status.live = Some(running(NOW - 600_000));
        let quiet = NOW - UNRESPONSIVE_AFTER_MS;
        let shown = decided(&status, &Value::Null, Some(quiet));
        assert_eq!((shown.state, shown.text.as_str()), ("unresponsive", "Not responding"));
        assert_eq!((shown.last_alive_ms, shown.since_ms), (Some(quiet), NOW));
        let just = decided(&status, &Value::Null, Some(quiet + 1));
        assert_eq!(just.state, "busy");
        if let Some(live) = status.live.as_mut() {
            live.activity = Activity::Tool {
                name: "Bash".into(),
            };
        }
        assert_eq!(decided(&status, &Value::Null, Some(quiet)).state, "busy");
    }

    #[test]
    fn an_account_wait_is_a_usage_limit_or_a_switch_by_what_the_pool_said() {
        let mut status = idle();
        let mut live = running(NOW - 60_000);
        let reset = NOW + 3_600_000;
        let wait = Wait::Account {
            until_ms: NOW + 9_000_000,
            resets_at_ms: Some(reset),
        };
        live.parked = Some(parked(wait, failures::USAGE_LIMIT, NOW - 30_000));
        status.live = Some(live.clone());
        status.queue_depth = 3;
        let limit = decided(&status, &Value::Null, None);
        assert_eq!(
            (limit.state, limit.text.as_str(), limit.since_ms, limit.back_at_ms),
            ("usage_limit", "Usage limit", NOW - 30_000, Some(reset))
        );
        assert_eq!(limit.queue_ahead, 3);
        // No reset named: another seat takes the turn, the Host is moving it.
        let wait = Wait::Account {
            until_ms: NOW + 9_000_000,
            resets_at_ms: None,
        };
        live.parked = Some(parked(wait, failures::USAGE_LIMIT, NOW - 30_000));
        status.live = Some(live.clone());
        let switching = decided(&status, &Value::Null, None);
        assert_eq!(
            (switching.state, switching.text.as_str(), switching.back_at_ms),
            ("switching_account", "Switching account (usage limit)", None)
        );
        // ...unless the row says the account is used up with no seat to move to.
        let used_up = json!({"state": "exhausted", "resets_at": "2030-03-17T19:00:00Z"});
        let limit = decided(&status, &used_up, None);
        assert_eq!(limit.state, "usage_limit");
        assert_eq!(
            limit.back_at_ms,
            clarp_kernel::quota::from_text("2030-03-17T19:00:00Z")
        );
        // A sign-in that ran out.
        live.parked = Some(parked(wait_for_sign_in(), failures::AUTH, NOW - 1_000));
        status.live = Some(live);
        let signing = decided(&status, &Value::Null, None);
        assert_eq!(signing.text, "Switching account (sign-in)");
    }

    fn wait_for_sign_in() -> Wait {
        Wait::Account {
            until_ms: NOW + 600_000,
            resets_at_ms: None,
        }
    }

    fn refused(kind: &str, reason: &str, ended_at_ms: i64) -> Status {
        let mut status = idle();
        status.last = Some(clarp_kernel::turn::Settled {
            id: TurnId(1),
            message_seq: 1,
            origin: Origin::User,
            outcome: Outcome::Failed {
                reason: reason.into(),
                kind: kind.into(),
            },
            accepted_at_ms: ended_at_ms - 5_000,
            started_at_ms: Some(ended_at_ms - 4_000),
            ended_at_ms,
            tool_count: 0,
            attempts: 1,
            account_waits: 0,
            never_ran: false,
        });
        status
    }

    #[test]
    fn a_turn_that_ended_on_a_usage_limit_shows_the_limit_whatever_its_tool() {
        // A tool the Host keeps no pool of ends the turn failed with the kind.
        let status = refused(failures::USAGE_LIMIT, "Usage limit reached", NOW - 60_000);
        let limit = decided(&status, &Value::Null, None);
        assert_eq!(
            (limit.state, limit.text.as_str(), limit.since_ms, limit.back_at_ms),
            ("usage_limit", "Usage limit", NOW - 60_000, None)
        );
        // With the reset where the Host's reading of the tool's account has one.
        let used_up = json!({"state": "exhausted", "resets_at": "2030-03-17T19:00:00Z"});
        let limit = decided(&status, &used_up, None);
        assert_eq!(
            (limit.state, limit.since_ms, limit.back_at_ms),
            ("usage_limit", NOW - 60_000, clarp_kernel::quota::from_text("2030-03-17T19:00:00Z"))
        );
        // A log from before the kind was kept is read by its words.
        let words = "You've hit your usage limit. Try again later.";
        let older = refused("", words, NOW - 60_000);
        let expected = if failures::classify_error(words) == failures::USAGE_LIMIT {
            "usage_limit"
        } else {
            "ok"
        };
        assert_eq!(decided(&older, &Value::Null, None).state, expected);
        // Another failure says nothing of the account.
        let other = refused("stalled", "the tool stalled", NOW - 60_000);
        assert_eq!(decided(&other, &Value::Null, None).state, "ok");
        // Long after, with no reset known, the next turn tells again.
        let old = refused(failures::USAGE_LIMIT, "Usage limit reached", NOW - REFUSAL_SHOWN_MS);
        assert_eq!(decided(&old, &Value::Null, None).state, "ok");
        // Where another seat has room, the Host moves the tool to it: a switch, for a while.
        let switch = |ended_at_ms: i64| {
            decide(&Facts {
                status: &refused(failures::USAGE_LIMIT, "Usage limit reached", ended_at_ms),
                broken: None,
                backend_quota: &Value::Null,
                bound: false,
                spare_seat: true,
                last_alive_ms: None,
                start_ms: START_DEFAULT_MS,
                created_at_ms: 5,
                host_started_ms: 7,
                now_ms: NOW,
            })
        };
        let switching = switch(NOW - 10_000);
        assert_eq!(
            (switching.state, switching.text.as_str()),
            ("switching_account", "Switching account (usage limit)")
        );
        assert_eq!(switch(NOW - SWITCH_SHOWN_MS).state, "ok");
    }

    #[test]
    fn an_idle_agent_whose_account_is_used_up_with_no_seat_left_cannot_answer() {
        let used_up = json!({
            "state": "exhausted",
            "resets_at": "2030-03-17T19:00:00Z",
            "observed_at": "2030-03-17T17:45:40Z",
        });
        let limit = decided(&idle(), &used_up, None);
        assert_eq!(limit.state, "usage_limit");
        assert_eq!(
            limit.since_ms,
            clarp_kernel::quota::from_text("2030-03-17T17:45:40Z").unwrap()
        );
        // Near a limit is advice, not a reason it cannot answer; a bound agent has no account.
        let near = json!({"state": "near", "resets_at": "2030-03-17T19:00:00Z"});
        assert_eq!(decided(&idle(), &near, None).state, "ok");
        let bound = decide(&Facts {
            status: &idle(),
            broken: None,
            backend_quota: &used_up,
            bound: true,
            spare_seat: false,
            last_alive_ms: None,
            start_ms: START_DEFAULT_MS,
            created_at_ms: 5,
            host_started_ms: 7,
            now_ms: NOW,
        });
        assert_eq!(bound.state, "ok");
    }

    #[test]
    fn a_process_that_starts_again_is_restarting_with_its_expected_return() {
        let mut status = idle();
        let mut live = running(NOW - 60_000);
        let wait = Wait::Retry {
            until_ms: NOW + 5_000,
            resume: true,
        };
        live.parked = Some(parked(wait, "stalled", NOW - 2_000));
        status.live = Some(live.clone());
        let waiting = decided(&status, &Value::Null, None);
        assert_eq!(
            (waiting.state, waiting.text.as_str(), waiting.back_at_ms),
            ("restarting", "Restarting", Some(NOW + 5_000 + START_DEFAULT_MS))
        );
        live.parked = None;
        live.starting = true;
        live.failed_attempts = 1;
        live.moved_at_ms = NOW - 3_000;
        status.live = Some(live.clone());
        let starting = decided(&status, &Value::Null, None);
        assert_eq!(
            (starting.state, starting.since_ms, starting.back_at_ms),
            ("restarting", NOW - 3_000, Some(NOW - 3_000 + START_DEFAULT_MS))
        );
        // A first start is the turn's work: busy.
        live.failed_attempts = 0;
        status.live = Some(live);
        assert_eq!(decided(&status, &Value::Null, None).state, "busy");
        // An agent being opened after the Host started.
        let opening = decide(&Facts {
            status: &idle(),
            broken: Some(crate::agent::OPENING),
            backend_quota: &Value::Null,
            bound: false,
            spare_seat: false,
            last_alive_ms: None,
            start_ms: START_DEFAULT_MS,
            created_at_ms: 5,
            host_started_ms: 7,
            now_ms: NOW,
        });
        assert_eq!((opening.state, opening.since_ms), ("restarting", 7));
    }

    #[test]
    fn a_tools_start_is_estimated_from_its_own_last_starts() {
        assert_eq!(start_estimate("test-tool-a"), START_DEFAULT_MS);
        for took in [4_000, 6_000, 5_000] {
            started("test-tool-a", took);
        }
        assert_eq!(start_estimate("test-tool-a"), 5_000);
    }

    struct Dir(std::path::PathBuf);

    impl Dir {
        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn a_restart_is_measured_from_the_stop_to_the_next_start() {
        let dir = std::env::temp_dir().join(format!("clarp-availability-{}", crate::ids::hex(6)));
        std::fs::create_dir_all(&dir).unwrap();
        let dir = Dir(dir);
        let first = host_updating(dir.path(), NOW);
        assert_eq!(first["type"], "host-updating");
        assert_eq!(first["text"], "Host updating");
        assert_eq!(first["back_at_ms"], NOW + RESTART_DEFAULT_MS);
        host_started(dir.path(), NOW + 12_000);
        let second = host_updating(dir.path(), NOW + 100_000);
        assert_eq!(second["back_at_ms"], NOW + 100_000 + 12_000);
        // A stop of an hour was no restart: the measure stays.
        host_started(dir.path(), NOW + 100_000 + 3_600_000);
        let third = host_updating(dir.path(), NOW + 200_000);
        assert_eq!(third["back_at_ms"], NOW + 200_000 + 12_000);
    }
}
