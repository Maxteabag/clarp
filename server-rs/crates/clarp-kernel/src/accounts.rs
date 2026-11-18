//! The turns of one account pool, when an account is out.
//!
//! A pool is the agents of one tool on one machine: they share the tool's login. When a turn
//! fails because the account is out (its limit is reached, its sign-in failed, its plan does
//! not offer the model), the owner's selector can put another login in place. The selector is
//! a program of the owner's, and it changes the login the whole machine uses. So nothing of the
//! pool's tool may run while it does: a tool that is running refreshes its token now and then
//! and writes the old account back over the new login. Every running turn of the pool is
//! stopped first, the selector is asked once for all of them, and they are taken up again.
//!
//! This is the Python Host's coordinator (`server/lib/account_failover.py`) with the plan it
//! asks (`server/lib/policies/failover.py`), as a machine that does nothing by itself: it is
//! told what happened and answers what is to be done ([`Effect`]). Its rules were paid for
//! there, each by an incident, and are kept as they are:
//!
//! - A turn is never left waiting for ever. After half an hour it runs, fails on its account,
//!   and its user is told why ([`MAX_PARK_MS`]); it is never parked a second time.
//! - A selector that gives no answer has said nothing about the accounts: asked again later.
//! - An answer for some of the models takes up the turns of those models; the others wait
//!   while the pool works, and nothing is stopped for them. One of them that runs at its own
//!   half hour is the trial for the rest: when it has run a minute without failing on its
//!   account, the model is back and they start.
//! - A turn that meets the limit again soon after it was taken up is a relapse: the selector's
//!   answer did not hold. It is asked later each time, and the fifth relapse ends the turn
//!   ([`MAX_RELAPSES`]).
//! - A plan that does not offer a model is a fact about one turn: it stops nobody else, and
//!   after three answers without that model the turn ends with what the provider said.
//!
//! Held to the Python coordinator's own behaviour in 37 situations
//! (`tests/fixtures/account-pool-vectors.json`, written by
//! `server-rs/tools/account-pool-vectors.py`).

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Value, json};

/// How long after one asking of the selector the next is due.
pub const RECHECK_MS: i64 = 60_000;
/// How long a turn waits for an account in all, before it runs and fails where its user sees it.
pub const MAX_PARK_MS: i64 = 1_800_000;
/// A turn that fails on its account sooner than this after a recovery took it up has relapsed.
pub const QUIET_MS: i64 = 600_000;
/// The relapse in a row at which a turn that relapses ends.
pub const MAX_RELAPSES: u32 = 5;
/// How many answers without its model a turn parked for its plan hears before it ends.
pub const PLAN_CHECKS: u32 = 3;
/// How long a recovery that a starting Host takes up waits for its turns to come back.
pub const CARRIED_WAIT_MS: i64 = 120_000;
/// How often it looks for them meanwhile.
pub const CARRIED_POLL_MS: i64 = 1_000;
/// How long a pass waits for the turns it had stopped. (The Python coordinator stops a turn
/// in place: ten seconds to end, ten more after it is killed, ten for what it left behind.)
pub const STOP_WAIT_MS: i64 = 30_000;

/// What the owner's selector said.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    /// An account serves every model that was asked for.
    All,
    /// The account now active serves only these of them. Nothing is known of the others
    /// beyond that it does not serve them now.
    Only(BTreeSet<String>),
    /// It looked, and no account serves: wait for a limit to pass.
    NoAccount,
    /// No answer: it timed out, ended badly, or said something that cannot be read. That
    /// says nothing about the accounts.
    NoAnswer,
    /// It could not be run at all.
    Failed,
}

/// What a selector's answer is, from how it ended and what it printed (`switch_account`).
/// `exit`: its exit code, or nothing where it was ended for taking too long.
pub fn read(exit: Option<i32>, printed: &str, models: &[String]) -> Verdict {
    if exit != Some(0) {
        return Verdict::NoAnswer;
    }
    let Ok(Value::Object(said)) = serde_json::from_str::<Value>(printed) else {
        return Verdict::NoAnswer;
    };
    match said.get("available") {
        Some(Value::Bool(true)) => {
            let Some(served) = said.get("models") else {
                // A selector that does not say which: all of them.
                return Verdict::All;
            };
            let Some(served) = served.as_array() else {
                return Verdict::NoAnswer;
            };
            let mut covered = BTreeSet::new();
            for model in served {
                let Some(model) = model.as_str() else {
                    return Verdict::NoAnswer;
                };
                if models.iter().any(|asked| asked == model) {
                    covered.insert(model.to_string());
                }
            }
            if models.iter().all(|asked| covered.contains(asked)) {
                Verdict::All
            } else {
                Verdict::Only(covered)
            }
        }
        // The selector tells its own failure as `available: false` with an `error`. Only a
        // bare false is a word about the accounts.
        Some(Value::Bool(false)) if !said.get("error").is_some_and(crate::python::truthy) => {
            Verdict::NoAccount
        }
        _ => Verdict::NoAnswer,
    }
}

/// What the selector is given on its standard input: the models the waiting turns run on.
pub fn asked(models: &[String]) -> String {
    crate::python::dumps_ascii(&json!({ "models": models }))
}

// ---- the plan --------------------------------------------------------------------------------

/// One waiting turn, as the plan sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Work<'a> {
    pub model: &'a str,
    /// Its process is gone.
    pub stopped: bool,
}

/// What is known of the accounts when the plan is asked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Checked {
    /// The selector was not asked in this pass.
    NotYet,
    Yes,
    No,
    NoAnswer,
    Failed,
}

/// What the plan says is to be done with the waiting turns.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// A turn of the pool still runs: stop it first.
    Kill { reason: &'static str },
    /// Ask the selector, for these models.
    Check { models: Vec<String> },
    /// Take the waiting turns up again.
    Resume { reason: &'static str },
    /// Leave them waiting, and look again after so long.
    Park { reason: &'static str, delay_ms: i64 },
    /// Let them go: nothing waits any more, or they waited as long as a turn may.
    Release { reason: &'static str },
}

pub const NO_OWNED_WORK: &str = "no-owned-work";
pub const PARK_EXPIRED: &str = "park-expired";

/// The plan of one recovery (`FailoverPlan.decide`). It reads no clock.
pub fn decide(
    work: &[Work<'_>],
    verdict: Checked,
    parked_since_ms: i64,
    next_check_ms: i64,
    checked_models: &[String],
    now_ms: i64,
) -> Decision {
    if work.is_empty() {
        return Decision::Release {
            reason: NO_OWNED_WORK,
        };
    }
    let reason = match verdict {
        Checked::NotYet => {
            if work.iter().any(|item| !item.stopped) {
                return Decision::Kill {
                    reason: "owned-work-still-running",
                };
            }
            let delay_ms = next_check_ms - now_ms;
            if delay_ms > 0 {
                return Decision::Park {
                    reason: "recheck-not-due",
                    delay_ms,
                };
            }
            let models: BTreeSet<&str> = work.iter().map(|item| item.model).collect();
            return Decision::Check {
                models: models.into_iter().map(str::to_string).collect(),
            };
        }
        Checked::Yes => {
            // A turn that arrived meanwhile can be of a model that was not asked for: it
            // waits with the rest for one more whole asking.
            let all = |item: &Work<'_>| checked_models.iter().any(|model| model == item.model);
            if work.iter().all(all) {
                return Decision::Resume {
                    reason: "account-verified",
                };
            }
            "unchecked-model"
        }
        Checked::No => "no-account",
        Checked::NoAnswer => "inconclusive",
        Checked::Failed => "recovery-failed",
    };
    if now_ms - parked_since_ms >= MAX_PARK_MS {
        return Decision::Release {
            reason: PARK_EXPIRED,
        };
    }
    Decision::Park {
        reason,
        delay_ms: RECHECK_MS,
    }
}

// ---- the pool --------------------------------------------------------------------------------

/// A timer of the pool: what it is for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Timer {
    /// The next pass of a recovery.
    Recovery,
    /// A look at the turns that wait for their model while the pool works. Only the latest
    /// one that was set counts.
    Held(u64),
    /// A pass has waited long enough for the turns it had stopped.
    Stops,
}

/// What is to be done. A pool does none of it itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    /// The agent's turn waits for an account: its user is shown that. Where it still runs
    /// it is stopped next ([`Effect::Stop`]).
    Pause { agent: String },
    /// The agent's turn waits for an account that serves its model, while the rest of the
    /// pool works.
    Hold { agent: String },
    /// The process of the agent's turn is to be stopped, and the pool told when nothing of
    /// it can work any more ([`Pool::stopped`]).
    Stop { agent: String },
    /// The agent's turn is taken up again. `relapsed`: it was told once already that it
    /// was taken up, and the account ran out again.
    Resume { agent: String, relapsed: bool },
    /// The agent's turn ends, with what the provider said of its plan.
    End { agent: String },
    /// The owner's selector is to be run for these models, and the pool told what it said
    /// ([`Pool::answered`]).
    Ask { models: Vec<String> },
    /// The pool is to be told when so long has passed ([`Pool::fired`]).
    Timer { timer: Timer, after_ms: i64 },
}

/// A turn of the pool, as the pool knows it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Attempt {
    agent: String,
    trace: String,
    model: String,
    /// It is still the agent's turn: nobody stopped it, removed the agent, or put another
    /// turn in its place.
    owned: bool,
    /// It waits for an account.
    parked: bool,
    /// It was parked again soon after a recovery took it up.
    relapsed: bool,
    /// Nothing of it runs.
    stopped: bool,
    /// It began a recovery by failing on its plan.
    plan: bool,
    /// How many answers of the selector since then did not serve its model.
    unserved_checks: u32,
    /// It can be told that it is held for its model, and it can be ended.
    holds: bool,
    ends: bool,
}

impl Attempt {
    fn key(&self) -> Key {
        (self.agent.clone(), self.trace.clone())
    }
}

type Key = (String, String);

/// A pass of a recovery that is under way.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Pass {
    /// It waits for the turns it had stopped.
    Stopping,
    /// It waits for the selector.
    Asking,
}

/// A turn that is about to start.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Starting<'a> {
    pub agent: &'a str,
    pub trace: &'a str,
    pub model: &'a str,
    /// Its agent can be shown as held for its model, and the turn can be ended by the pool.
    pub holds: bool,
    pub ends: bool,
}

/// The account pool of one tool.
#[derive(Debug, Clone, Default)]
pub struct Pool {
    /// The turns the pool knows, in the order their agents came to it.
    attempts: Vec<Attempt>,
    recovering: bool,
    pass: Option<Pass>,
    next_check_ms: i64,
    parked_since_ms: i64,
    checked_models: Vec<String>,
    /// Agents whose waiting turns a Host that started is to bring back, and until when the
    /// recovery waits for them.
    awaiting: BTreeSet<String>,
    awaiting_until_ms: i64,
    /// Turns let go at their half hour: they run and fail, and are never parked again.
    park_expired: BTreeSet<Key>,
    /// When a recovery last took each turn up.
    resumed_at_ms: BTreeMap<Key, i64>,
    /// When each turn's present wait began.
    first_parked_ms: BTreeMap<Key, i64>,
    /// When turns of the pool were last taken up, and how many recoveries in a row a
    /// relapse followed.
    last_resumed_ms: Option<i64>,
    relapses: u32,
    /// The latest timer set for turns that wait while the pool works.
    hold_timer: u64,
    /// Turns let go at their half hour while others still waited for their model: each is
    /// the trial of whether the account now active serves that model again.
    trials: BTreeMap<Key, (String, i64)>,
}

impl Pool {
    pub fn new() -> Self {
        Self::default()
    }

    fn at(&self, agent: &str) -> Option<usize> {
        self.attempts
            .iter()
            .position(|attempt| attempt.agent == agent)
    }

    fn take(&mut self, agent: &str) -> Option<Attempt> {
        self.at(agent).map(|at| self.attempts.remove(at))
    }

    fn expired(&self, attempt: &Attempt) -> bool {
        self.park_expired.contains(&attempt.key())
    }

    /// The turns that wait for an account, in order.
    fn parked_now(&self) -> Vec<Attempt> {
        let waits = |attempt: &&Attempt| attempt.parked && attempt.owned && !self.expired(attempt);
        self.attempts.iter().filter(waits).cloned().collect()
    }

    /// While the pool works: the models the account now active does not serve and turns
    /// wait for.
    fn unserved_models(&self) -> BTreeSet<String> {
        if self.recovering {
            return BTreeSet::new();
        }
        self.parked_now()
            .into_iter()
            .map(|attempt| attempt.model)
            .collect()
    }

    fn relapsed(&self, key: &Key, now_ms: i64) -> bool {
        self.resumed_at_ms
            .get(key)
            .is_some_and(|resumed| now_ms - resumed < QUIET_MS)
    }

    /// The turn's wait begins: its clock starts, and whoever takes it up is told when it
    /// was taken up only just before.
    fn note_park(&mut self, at: usize, now_ms: i64) {
        let key = self.attempts[at].key();
        self.first_parked_ms.insert(key.clone(), now_ms);
        if self.relapsed(&key, now_ms) {
            self.attempts[at].relapsed = true;
        }
    }

    /// A turn is about to start. Whether it has to wait instead; where it does not, it
    /// runs, and the pool takes it to have a process from now on.
    pub fn register(&mut self, turn: Starting<'_>, now_ms: i64) -> (bool, Vec<Effect>) {
        let mut out = Vec::new();
        // A new turn of the agent ends what was kept of its older ones.
        let other = |key: &Key| key.0 == turn.agent && key.1 != turn.trace;
        self.park_expired.retain(|key| !other(key));
        self.resumed_at_ms.retain(|key, _| !other(key));
        self.first_parked_ms.retain(|key, _| !other(key));
        self.trials.retain(|key, _| !other(key));
        let attempt = Attempt {
            agent: turn.agent.to_string(),
            trace: turn.trace.to_string(),
            model: turn.model.to_string(),
            owned: true,
            parked: false,
            relapsed: false,
            stopped: false,
            plan: false,
            unserved_checks: 0,
            holds: turn.holds,
            ends: turn.ends,
        };
        let at = match self.at(turn.agent) {
            Some(at) => {
                self.attempts[at] = attempt;
                at
            }
            None => {
                self.attempts.push(attempt);
                self.attempts.len() - 1
            }
        };
        self.awaiting.remove(turn.agent);
        // Behind a recovery, or behind the turns that wait for this model already: started
        // now it would meet the same limit and stop the pool.
        let unserved = self.unserved_models().contains(turn.model);
        if !(self.recovering || unserved) || self.expired(&self.attempts[at]) {
            return (false, out);
        }
        self.attempts[at].parked = true;
        self.note_park(at, now_ms);
        let agent = turn.agent.to_string();
        out.push(if self.recovering || !turn.holds {
            Effect::Pause { agent }
        } else {
            Effect::Hold { agent }
        });
        // Parked before it started: there is no process to stop.
        self.attempts[at].stopped = true;
        // It may have taken the place of a turn a pass waited for.
        self.stopping_over(now_ms, &mut out);
        (true, out)
    }

    /// The turn is over, however it ended.
    pub fn discard(&mut self, agent: &str, trace: &str, now_ms: i64) -> Vec<Effect> {
        let mut out = Vec::new();
        let key = (agent.to_string(), trace.to_string());
        self.park_expired.remove(&key);
        self.resumed_at_ms.remove(&key);
        self.first_parked_ms.remove(&key);
        self.trials.remove(&key);
        if self
            .at(agent)
            .is_some_and(|at| self.attempts[at].trace == trace)
        {
            self.take(agent);
        }
        // A pass may have waited for it to stop.
        self.stopping_over(now_ms, &mut out);
        out
    }

    /// The turn is no longer the agent's: its user stopped it, the agent was removed, or
    /// another turn was put in its place. A recovery lets it go at its next pass.
    pub fn disown(&mut self, agent: &str, now_ms: i64) -> Vec<Effect> {
        let mut out = Vec::new();
        if let Some(at) = self.at(agent) {
            self.attempts[at].owned = false;
        }
        self.stopping_over(now_ms, &mut out);
        out
    }

    /// Whether the turn waits for an account with no process left: whoever stops it has
    /// nothing to stop.
    pub fn parked(&self, agent: &str, trace: &str) -> bool {
        self.at(agent).is_some_and(|at| {
            let attempt = &self.attempts[at];
            attempt.trace == trace && attempt.parked && attempt.stopped
        })
    }

    /// A turn failed on its account. Whether the pool takes it up: where it does not, the
    /// turn ends on that failure, and its user is told. `selector`: whether the owner has a
    /// selector for this pool. `plan`: the provider said that the account does not offer the
    /// turn's model, which is a fact about one turn and not about the account's limits.
    pub fn request(
        &mut self,
        agent: &str,
        trace: &str,
        selector: bool,
        plan: bool,
        now_ms: i64,
    ) -> (bool, Vec<Effect>) {
        let mut out = Vec::new();
        let Some(trigger) = self.at(agent) else {
            return (false, out);
        };
        if !selector || self.attempts[trigger].trace != trace {
            return (false, out);
        }
        let key = self.attempts[trigger].key();
        if self.expired(&self.attempts[trigger]) {
            // Its half hour is over, and its model is still not served.
            self.trials.remove(&key);
            return (false, out);
        }
        let relapsed = self.relapsed(&key, now_ms);
        if plan && relapsed {
            // Taken up onto an account said to offer the model, and it does not.
            return (false, out);
        }
        if self.recovering {
            return (true, out);
        }
        if plan {
            // The others run fine: nothing is stopped for one turn's model.
            let runs =
                |(at, other): (usize, &Attempt)| at != trigger && other.owned && !other.parked;
            if self.attempts.iter().enumerate().any(runs) {
                return (false, out);
            }
            self.attempts[trigger].plan = true;
        } else if self
            .last_resumed_ms
            .is_none_or(|resumed| now_ms - resumed >= QUIET_MS)
        {
            // Ten quiet minutes: this is a new limit.
            self.relapses = 0;
        } else {
            // The last recovery's answer did not hold. Asked again, later each time; a
            // turn that keeps relapsing ends here.
            self.relapses += 1;
            if relapsed && self.relapses >= MAX_RELAPSES {
                return (false, out);
            }
            let wait = RECHECK_MS << (self.relapses.min(MAX_RELAPSES - 1) - 1);
            self.next_check_ms = self.next_check_ms.max(now_ms + wait);
        }
        self.recovering = true;
        self.parked_since_ms = now_ms;
        for at in 0..self.attempts.len() {
            let attempt = &self.attempts[at];
            if attempt.parked || !attempt.owned || self.expired(attempt) {
                continue; // parked already (its clock runs from that), or not to be parked
            }
            self.attempts[at].parked = true;
            self.note_park(at, now_ms);
            out.push(Effect::Pause {
                agent: self.attempts[at].agent.clone(),
            });
        }
        out.push(Effect::Timer {
            timer: Timer::Recovery,
            after_ms: 0,
        });
        (true, out)
    }

    /// A Host that starts takes up a recovery that was under way when the one before it
    /// stopped, with the time it began, so that a restart never makes a turn wait longer
    /// than a turn may. `since_ms`: when its turns were first parked. `agents`: whose
    /// waiting turns are to come back.
    pub fn carried(
        &mut self,
        selector: bool,
        since_ms: i64,
        agents: &[String],
        now_ms: i64,
    ) -> (bool, Vec<Effect>) {
        if !selector || self.recovering {
            return (false, Vec::new());
        }
        self.recovering = true;
        self.parked_since_ms = since_ms.min(now_ms);
        self.next_check_ms = 0;
        self.awaiting = agents
            .iter()
            .filter(|agent| self.at(agent).is_none())
            .cloned()
            .collect();
        self.awaiting_until_ms = now_ms + CARRIED_WAIT_MS;
        let timer = Effect::Timer {
            timer: Timer::Recovery,
            after_ms: 0,
        };
        (true, vec![timer])
    }

    /// Someone else changed the machine's login (the owner, the desk of seats) to an
    /// account known to serve the models `serves` accepts. The turns that wait for them
    /// while the pool works start now: how many. Nothing is stopped and nobody is asked.
    /// During a recovery nothing is done and nothing is answered: its own pass asks the
    /// selector, and whoever reports must report again.
    pub fn served(
        &mut self,
        serves: impl Fn(&str) -> bool,
        now_ms: i64,
    ) -> (Option<usize>, Vec<Effect>) {
        let mut out = Vec::new();
        if self.recovering {
            return (None, out);
        }
        let back: Vec<Attempt> = self
            .parked_now()
            .into_iter()
            .filter(|attempt| serves(&attempt.model))
            .collect();
        self.start_parked(&back, now_ms, &mut out);
        (Some(back.len()), out)
    }

    /// The account desk confirmed a new login after draining its tool. A recovery that
    /// already sent a selector finishes its own pass. Between passes the new information
    /// takes up the waiting models it covers.
    pub fn login_available(&mut self, serves: impl Fn(&str) -> bool, now: i64) -> Vec<Effect> {
        if !self.recovering {
            return self.served(serves, now).1;
        }
        if self.pass.is_some() || self.attempts.iter().any(|a| a.owned && !a.stopped) {
            return Vec::new();
        }
        let models = self
            .parked_now()
            .into_iter()
            .filter(|a| serves(&a.model))
            .map(|a| a.model)
            .collect();
        let mut effects = Vec::new();
        self.after(Verdict::Only(models), now, &mut effects);
        effects
    }

    /// The turns that are still their agents', the others forgotten; without those let go
    /// at their half hour, which run and are not parked again.
    fn pending(&mut self) -> Vec<Attempt> {
        self.attempts.retain(|attempt| attempt.owned);
        let waits = |attempt: &&Attempt| !self.expired(attempt);
        self.attempts.iter().filter(waits).cloned().collect()
    }

    fn decided(&self, pending: &[Attempt], verdict: Checked, now_ms: i64) -> Decision {
        let work: Vec<Work<'_>> = pending
            .iter()
            .map(|attempt| Work {
                model: &attempt.model,
                stopped: attempt.stopped,
            })
            .collect();
        let (since, next) = (self.parked_since_ms, self.next_check_ms);
        decide(&work, verdict, since, next, &self.checked_models, now_ms)
    }

    /// Takes out the turns whose own wait is half an hour old; they are never parked again.
    fn take_overdue(&mut self, attempts: &[Attempt], now_ms: i64) -> Vec<Attempt> {
        let old = |attempt: &&Attempt| {
            let since = self.first_parked_ms.get(&attempt.key()).copied();
            now_ms - since.unwrap_or(now_ms) >= MAX_PARK_MS
        };
        let overdue: Vec<Attempt> = attempts.iter().filter(old).cloned().collect();
        for attempt in &overdue {
            self.take(&attempt.agent);
            self.park_expired.insert(attempt.key());
        }
        overdue
    }

    /// The turns let go at their half hour run, and fail where their users see it.
    fn release(overdue: &[Attempt], out: &mut Vec<Effect>) {
        out.extend(overdue.iter().filter(|attempt| attempt.owned).map(resumed));
    }

    /// A turn let go at its half hour, while others still wait for its model, is their trial.
    fn note_trials(&mut self, released: &[Attempt], parked: &[Attempt], now_ms: i64) {
        for attempt in released {
            if parked.iter().any(|waits| waits.model == attempt.model) {
                self.trials
                    .insert(attempt.key(), (attempt.model.clone(), now_ms));
            }
        }
    }

    /// The models a trial has shown the account now active to serve again: its turn, let
    /// go a minute ago or more, still runs and has met no failure on its account. A trial
    /// that is over, either way, proves nothing more.
    fn proven(&mut self, now_ms: i64) -> BTreeSet<String> {
        let mut proven = BTreeSet::new();
        let due: Vec<Key> = self
            .trials
            .iter()
            .filter(|(_, (_, released))| now_ms - released >= RECHECK_MS)
            .map(|(key, _)| key.clone())
            .collect();
        for key in due {
            let Some((model, _)) = self.trials.remove(&key) else {
                continue;
            };
            let runs = self.at(&key.0).is_some_and(|at| {
                let attempt = &self.attempts[at];
                attempt.trace == key.1 && attempt.owned && !attempt.parked
            });
            if runs {
                proven.insert(model);
            }
        }
        proven
    }

    /// Starts turns that waited while the pool worked: their model is served again.
    fn start_parked(&mut self, back: &[Attempt], now_ms: i64, out: &mut Vec<Effect>) {
        if back.is_empty() {
            return;
        }
        self.last_resumed_ms = Some(now_ms);
        // All of them out first: a turn that is taken up comes back as a turn that starts,
        // and must not find the others still waiting.
        for attempt in back {
            self.take(&attempt.agent);
        }
        for attempt in back {
            self.resumed_at_ms.insert(attempt.key(), now_ms);
            out.push(resumed(attempt));
        }
    }

    fn arm_hold(&mut self, out: &mut Vec<Effect>) {
        self.hold_timer += 1;
        out.push(Effect::Timer {
            timer: Timer::Held(self.hold_timer),
            after_ms: RECHECK_MS,
        });
    }

    /// Whether what follows is a pass of a recovery. `held`: the timer of a look at turns
    /// that wait while the pool works; a recovery's own passes have none.
    ///
    /// Outside a recovery, turns can wait for a model the account now active does not
    /// serve while the turns it does serve work. For them nothing is done to the others:
    /// no turn is stopped, and the selector, which changes the machine's login while it
    /// looks, is not run. Each such turn is let go at its own half hour. Only when no turn
    /// of the pool runs does a recovery begin for what waits.
    fn begin(&mut self, held: Option<u64>, now_ms: i64, out: &mut Vec<Effect>) -> bool {
        if self.recovering {
            return held.is_none(); // the recovery has its own passes
        }
        if held.is_some_and(|timer| timer != self.hold_timer) {
            return false;
        }
        let mut parked = self.parked_now();
        let overdue = self.take_overdue(&parked, now_ms);
        parked.retain(|attempt| !overdue.contains(attempt));
        // The model is served again: the turns that waited for it start, and with none of
        // them waiting a new one for it runs at once.
        let proven = self.proven(now_ms);
        let back: Vec<Attempt> = parked
            .iter()
            .filter(|attempt| proven.contains(&attempt.model))
            .cloned()
            .collect();
        parked.retain(|attempt| !back.contains(attempt));
        self.start_parked(&back, now_ms, out);
        self.note_trials(&overdue, &parked, now_ms);
        let running = self
            .attempts
            .iter()
            .any(|attempt| attempt.owned && !attempt.parked);
        let begin = !parked.is_empty() && !running;
        if begin {
            self.recovering = true;
            self.parked_since_ms = now_ms;
        } else if !parked.is_empty() {
            self.arm_hold(out);
        }
        Self::release(&overdue, out);
        begin
    }

    /// A timer of the pool came due.
    pub fn fired(&mut self, timer: Timer, now_ms: i64) -> Vec<Effect> {
        let mut out = Vec::new();
        if timer == Timer::Stops {
            if self.pass == Some(Pass::Stopping) {
                // A turn did not stop in time. Nothing is asked while it may still run: the
                // waiting turns wait on for a later pass, as after a selector that could not
                // be run, and are let go at their half hour all the same.
                self.pass = None;
                self.after(Verdict::Failed, now_ms, &mut out);
            }
            return out;
        }
        if self.pass.is_some() {
            // A pass is under way, and goes on when it is told what it waits for.
            return out;
        }
        let held = match timer {
            Timer::Held(number) => Some(number),
            Timer::Recovery | Timer::Stops => None,
        };
        if held.is_some() && !self.begin(held, now_ms, &mut out) {
            return out;
        }
        if !self.begin(None, now_ms, &mut out) {
            return out;
        }
        let pending = self.pending();
        if matches!(
            self.decided(&pending, Checked::NotYet, now_ms),
            Decision::Kill { .. }
        ) {
            let runs: Vec<String> = pending
                .iter()
                .filter(|attempt| !attempt.stopped)
                .map(|attempt| attempt.agent.clone())
                .collect();
            self.pass = Some(Pass::Stopping);
            out.extend(runs.into_iter().map(|agent| Effect::Stop { agent }));
            out.push(Effect::Timer {
                timer: Timer::Stops,
                after_ms: STOP_WAIT_MS,
            });
            return out;
        }
        self.stopped_all(now_ms, &mut out);
        out
    }

    /// Nothing of the agent's turn can work any more.
    pub fn stopped(&mut self, agent: &str, now_ms: i64) -> Vec<Effect> {
        let mut out = Vec::new();
        if let Some(at) = self.at(agent) {
            self.attempts[at].stopped = true;
        }
        self.stopping_over(now_ms, &mut out);
        out
    }

    /// A pass that waits for turns to stop goes on when none of them runs any more.
    fn stopping_over(&mut self, now_ms: i64, out: &mut Vec<Effect>) {
        if self.pass != Some(Pass::Stopping) {
            return;
        }
        let runs = |attempt: &Attempt| attempt.owned && !attempt.stopped && !self.expired(attempt);
        if self.attempts.iter().any(runs) {
            return;
        }
        self.pass = None;
        self.stopped_all(now_ms, out);
    }

    /// The pass, once nothing of the pool runs: is it time to ask the selector?
    fn stopped_all(&mut self, now_ms: i64, out: &mut Vec<Effect>) {
        let pending = self.pending();
        if pending.is_empty() && !self.awaiting.is_empty() && now_ms < self.awaiting_until_ms {
            // A recovery that was taken up, whose turns are not back yet: ending it now
            // would run them unasked when they come.
            out.push(Effect::Timer {
                timer: Timer::Recovery,
                after_ms: CARRIED_POLL_MS,
            });
            return;
        }
        self.awaiting.clear();
        match self.decided(&pending, Checked::NotYet, now_ms) {
            Decision::Release { .. } => self.recovering = false,
            Decision::Park { delay_ms, .. } => out.push(Effect::Timer {
                timer: Timer::Recovery,
                after_ms: delay_ms,
            }),
            // A turn that still runs, though this pass stopped all it had found: it came
            // meanwhile, and gets a pass of its own at once.
            Decision::Kill { .. } => out.push(Effect::Timer {
                timer: Timer::Recovery,
                after_ms: 0,
            }),
            Decision::Check { models } => {
                self.next_check_ms = now_ms + RECHECK_MS;
                self.checked_models.clone_from(&models);
                self.pass = Some(Pass::Asking);
                out.push(Effect::Ask { models });
            }
            Decision::Resume { .. } => {}
        }
    }

    /// The selector answered.
    pub fn answered(&mut self, verdict: Verdict, now_ms: i64) -> Vec<Effect> {
        let mut out = Vec::new();
        if self.pass != Some(Pass::Asking) {
            return out;
        }
        self.pass = None;
        self.after(verdict, now_ms, &mut out);
        out
    }

    /// What follows an answer of the selector, or the lack of one.
    fn after(&mut self, verdict: Verdict, now_ms: i64, out: &mut Vec<Effect>) {
        let mut pending = self.pending();
        let served = match &verdict {
            Verdict::Only(models) => Some(models.clone()),
            _ => None,
        };
        // A turn parked for its plan has heard one more answer that does not serve its
        // model. After three of them it ends, with what the provider said. Nothing else
        // ends here: a limit is waited out to the half hour.
        let mut ended = Vec::new();
        if verdict == Verdict::NoAccount || served.is_some() {
            for attempt in &mut pending {
                let unserved = served
                    .as_ref()
                    .is_none_or(|served| !served.contains(&attempt.model));
                if attempt.plan && unserved {
                    attempt.unserved_checks += 1;
                    if let Some(at) = self.at(&attempt.agent) {
                        self.attempts[at].unserved_checks = attempt.unserved_checks;
                    }
                    if attempt.ends && attempt.unserved_checks >= PLAN_CHECKS {
                        ended.push(attempt.clone());
                    }
                }
            }
        }
        for attempt in &ended {
            self.take(&attempt.agent);
        }
        pending.retain(|attempt| !ended.iter().any(|gone| gone.agent == attempt.agent));
        // An answer for some of the models: the turns it covers are taken up, the others
        // wait on, each in its own wait.
        let mut waiting = Vec::new();
        let mut checked = match verdict {
            Verdict::All => Checked::Yes,
            Verdict::NoAccount => Checked::No,
            Verdict::NoAnswer => Checked::NoAnswer,
            Verdict::Failed => Checked::Failed,
            Verdict::Only(_) => Checked::No, // it serves none of them now, unless it does:
        };
        if let Some(served) = &served {
            let covers = |attempt: &Attempt| served.contains(&attempt.model);
            if pending.iter().any(covers) {
                waiting = pending.iter().filter(|a| !covers(a)).cloned().collect();
                pending.retain(covers);
                checked = Checked::Yes;
            }
        }
        // A turn that waited while the pool worked is let go when its own wait is half an
        // hour old, though this recovery began later.
        let mut old = waiting.clone();
        if checked != Checked::Yes {
            old.extend(pending.iter().cloned());
        }
        let overdue = self.take_overdue(&old, now_ms);
        pending.retain(|attempt| !overdue.contains(attempt));
        waiting.retain(|attempt| !overdue.contains(attempt));
        self.note_trials(&overdue, &waiting, now_ms);
        let decision = self.decided(&pending, checked, now_ms);
        match &decision {
            Decision::Resume { .. } => {
                self.recovering = false;
                self.last_resumed_ms = Some(now_ms);
                // All of them out first, as above.
                for attempt in &pending {
                    self.take(&attempt.agent);
                }
                for attempt in &pending {
                    if attempt.owned {
                        self.resumed_at_ms.insert(attempt.key(), now_ms);
                        out.push(resumed(attempt));
                    }
                }
                if !waiting.is_empty() {
                    for attempt in &waiting {
                        if attempt.holds && attempt.owned {
                            out.push(Effect::Hold {
                                agent: attempt.agent.clone(),
                            });
                        }
                    }
                    self.arm_hold(out);
                }
            }
            Decision::Release { reason } => {
                self.recovering = false;
                if *reason == PARK_EXPIRED {
                    self.park_expired.extend(pending.iter().map(Attempt::key));
                    self.attempts.clear();
                }
            }
            _ => {}
        }
        for attempt in &ended {
            if attempt.owned {
                out.push(Effect::End {
                    agent: attempt.agent.clone(),
                });
            }
        }
        Self::release(&overdue, out);
        match decision {
            Decision::Resume { .. } => {}
            Decision::Release { reason } => {
                if reason == PARK_EXPIRED {
                    // Half an hour without an account: the turns run and fail where their
                    // users see it, instead of waiting for ever.
                    out.extend(pending.iter().filter(|a| a.owned).map(resumed));
                }
            }
            Decision::Park { delay_ms, .. } => out.push(Effect::Timer {
                timer: Timer::Recovery,
                after_ms: delay_ms,
            }),
            Decision::Kill { .. } | Decision::Check { .. } => {}
        }
    }

    /// Whether a recovery is under way.
    pub fn recovering(&self) -> bool {
        self.recovering
    }

    /// Whether a pass of it waits for something it asked for.
    pub fn checking(&self) -> bool {
        self.pass.is_some()
    }

    /// The agents whose turns wait for an account, by name.
    pub fn waiting(&self) -> Vec<String> {
        let mut waiting: Vec<String> = self
            .attempts
            .iter()
            .filter(|attempt| attempt.parked && attempt.owned)
            .map(|attempt| attempt.agent.clone())
            .collect();
        waiting.sort();
        waiting
    }

    /// When the recovery under way began: a Host that stops hands this on to the one that
    /// starts ([`Pool::carried`]).
    pub fn parked_since_ms(&self) -> Option<i64> {
        self.recovering.then_some(self.parked_since_ms)
    }

    /// How the pool stands, for whoever looks into it: what the Python coordinator's own
    /// fields say, under names of this Host.
    pub fn stood(&self, now_ms: i64) -> Value {
        let mut turns: Vec<Value> = self
            .attempts
            .iter()
            .filter(|attempt| attempt.owned)
            .map(|attempt| {
                json!([
                    attempt.agent,
                    attempt.trace,
                    attempt.model,
                    attempt.parked,
                    attempt.plan,
                    attempt.unserved_checks
                ])
            })
            .collect();
        turns.sort_by_key(Value::to_string);
        let released: Vec<Value> = self
            .park_expired
            .iter()
            .map(|key| json!([key.0, key.1]))
            .collect();
        let trials: Vec<Value> = self
            .trials
            .iter()
            .map(|(key, (model, _))| json!([key.0, key.1, model]))
            .collect();
        json!({
            "recovering": self.recovering,
            "waiting": self.waiting(),
            "relapses": self.relapses,
            "next_check_in_ms": (self.next_check_ms - now_ms).max(0),
            "released": released,
            "trials": trials,
            "turns": turns,
        })
    }
}

fn resumed(attempt: &Attempt) -> Effect {
    Effect::Resume {
        agent: attempt.agent.clone(),
        relapsed: attempt.relapsed,
    }
}

#[cfg(test)]
mod tests {
    //! What the vectors cannot show: the Python coordinator stops a turn before it goes on,
    //! and this pool is told when a turn has stopped.

    use super::*;

    fn starting<'a>(agent: &'a str, trace: &'a str) -> Starting<'a> {
        Starting {
            agent,
            trace,
            model: "m1",
            holds: true,
            ends: true,
        }
    }

    fn stop(agent: &str) -> Effect {
        Effect::Stop {
            agent: agent.to_string(),
        }
    }

    fn resume(agent: &str) -> Effect {
        Effect::Resume {
            agent: agent.to_string(),
            relapsed: false,
        }
    }

    /// Two running turns, one of which failed on its account: the pool as it stands when
    /// its first pass has asked for both to be stopped.
    fn stopping() -> Pool {
        let mut pool = Pool::new();
        assert!(!pool.register(starting("a", "t-a"), 0).0);
        assert!(!pool.register(starting("b", "t-b"), 0).0);
        assert!(pool.request("a", "t-a", true, false, 0).0);
        let watch = Effect::Timer {
            timer: Timer::Stops,
            after_ms: STOP_WAIT_MS,
        };
        assert_eq!(
            pool.fired(Timer::Recovery, 0),
            vec![stop("a"), stop("b"), watch]
        );
        assert!(pool.checking());
        pool
    }

    #[test]
    fn a_pass_goes_on_when_every_running_turn_has_stopped() {
        let mut pool = stopping();
        assert_eq!(pool.stopped("a", 1_000), vec![]);
        assert!(pool.checking() && !pool.parked("b", "t-b") && pool.parked("a", "t-a"));
        // A timer that comes due meanwhile begins no second pass.
        assert_eq!(pool.fired(Timer::Recovery, 2_000), vec![]);
        assert_eq!(pool.fired(Timer::Held(1), 2_000), vec![]);
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        assert_eq!(pool.stopped("b", 3_000), vec![asked]);
        assert_eq!(
            pool.answered(Verdict::All, 9_000),
            vec![resume("a"), resume("b")]
        );
        assert!(!pool.checking() && !pool.recovering() && pool.waiting().is_empty());
    }

    #[test]
    fn a_turn_its_user_stops_while_the_pass_waits_for_it_is_not_waited_for() {
        let mut pool = stopping();
        assert_eq!(pool.stopped("a", 1_000), vec![]);
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        assert_eq!(pool.disown("b", 2_000), vec![asked]);
        assert_eq!(pool.answered(Verdict::All, 3_000), vec![resume("a")]);
        assert!(!pool.recovering());
    }

    #[test]
    fn every_turn_stopped_by_its_user_while_the_pass_waits_ends_the_recovery() {
        let mut pool = stopping();
        assert_eq!(pool.disown("a", 1_000), vec![]);
        assert_eq!(pool.disown("b", 1_000), vec![]);
        assert!(!pool.checking() && !pool.recovering());
        assert_eq!(pool.parked_since_ms(), None);
    }

    #[test]
    fn a_turn_that_ends_by_itself_while_the_pass_waits_for_it_is_not_waited_for() {
        let mut pool = stopping();
        assert_eq!(pool.stopped("a", 1_000), vec![]);
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        // It was over before the stop reached it.
        assert_eq!(pool.discard("b", "t-b", 2_000), vec![asked]);
        assert_eq!(pool.answered(Verdict::All, 3_000), vec![resume("a")]);
    }

    #[test]
    fn a_turn_that_takes_the_place_of_one_the_pass_waits_for_waits_with_the_rest() {
        let mut pool = stopping();
        assert_eq!(pool.stopped("a", 1_000), vec![]);
        // The agent's turn is replaced by a new one: that one is parked before it starts.
        let (waits, did) = pool.register(starting("b", "t-next"), 2_000);
        let paused = Effect::Pause {
            agent: "b".to_string(),
        };
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        assert!(waits);
        assert_eq!(did, vec![paused, asked]);
        assert_eq!(
            pool.answered(Verdict::All, 3_000),
            vec![resume("a"), resume("b")]
        );
    }

    #[test]
    fn a_turn_that_does_not_stop_in_time_leaves_the_others_waiting_for_a_later_pass() {
        let mut pool = stopping();
        assert_eq!(pool.stopped("a", 1_000), vec![]);
        // The wait for the stops is over, and b still runs: nobody is asked.
        let later = Effect::Timer {
            timer: Timer::Recovery,
            after_ms: RECHECK_MS,
        };
        assert_eq!(pool.fired(Timer::Stops, STOP_WAIT_MS), vec![later]);
        assert!(!pool.checking() && pool.recovering());
        // The next pass stops what still runs, and waits again.
        let again = pool.fired(Timer::Recovery, STOP_WAIT_MS + RECHECK_MS);
        assert_eq!(again[0], stop("b"));
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        assert_eq!(
            pool.stopped("b", STOP_WAIT_MS + RECHECK_MS + 1),
            vec![asked]
        );
        // A timer for stops that is no longer waited for does nothing.
        assert_eq!(
            pool.fired(Timer::Stops, STOP_WAIT_MS + RECHECK_MS + 2),
            vec![]
        );
        assert!(pool.checking());
    }

    #[test]
    fn turns_that_never_stop_are_let_go_at_the_half_hour_all_the_same() {
        let mut pool = stopping();
        let did = pool.fired(Timer::Stops, MAX_PARK_MS);
        assert_eq!(did, vec![resume("a"), resume("b")]);
        assert!(!pool.recovering() && !pool.checking());
    }

    #[test]
    fn an_answer_nobody_asked_for_changes_nothing() {
        let mut pool = Pool::new();
        assert_eq!(pool.answered(Verdict::All, 0), vec![]);
        let mut pool = stopping();
        assert_eq!(pool.answered(Verdict::NoAccount, 0), vec![]);
        assert!(pool.checking() && pool.recovering());
        assert_eq!(pool.parked_since_ms(), Some(0));
    }
}
