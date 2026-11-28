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
//!   and its user is told why ([`MAX_PARK_MS`]); it is never parked a second time. Where a
//!   turn's tool said when the account has usage again, every waiting turn of that model
//!   waits until then instead, where that is later ([`Pool::out_until`]), and may wait again
//!   for a next reset: the selector is asked less often meanwhile, within a minute of a turn
//!   that comes, and once after the first such reset. A turn let go at its reset runs on while
//!   the recovery waits for another model's later reset: only a new failure on its account
//!   takes it up again. (The Rust Host's own rule: `docs/differences.md`.)
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
/// The same on the Rust Host: the first answer of the selector that leaves the turn's model
/// out ends it. That is a choice of how long to wait, not proof that no account has the model
/// (an answer says only what the account it made active serves); waiting three minutes on
/// it left a turn shown as waiting for usage it did not lack (issue 720, `docs/differences.md`).
pub const PLAN_CHECKS_HERE: u32 = 1;
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
    /// When its wait may end, where its tool said when the account it ran out on has usage
    /// again: then, and a minute more (`recovery::after_reset`).
    resets_until_ms: Option<i64>,
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
    /// Turns let go at the reset they waited for, past their half hour: their account has
    /// usage again, so they run on. A recovery still under way for another model's later reset
    /// neither parks them again when they start nor stops them at its passes, and turns still
    /// held for their model do not hold them. Only a new failure on their account takes them
    /// up again, for its next reset; a recovery that begins anew stops them like any turn.
    released_at_reset: BTreeSet<Key>,
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
    /// How many answers without its model a turn parked for its plan hears before it ends,
    /// where it is not [`PLAN_CHECKS`].
    plan_checks: Option<u32>,
    /// How many answers in a row found no account while every waiting turn waits for a
    /// reset it was told: the next asking is twice as far off after each.
    quiet_checks: u32,
    /// A confirmed login change overlapped the outstanding selector request.
    login_changed_while_asking: bool,
    /// When the account has usage again for each model, as the tool of a turn of that model
    /// said in this recovery (the latest said): a fact of the machine's one login, so every
    /// waiting turn of the model waits for it, also one that was stopped with that turn or
    /// came after it and was never refused itself.
    resets_by_model: BTreeMap<String, i64>,
}

impl Pool {
    pub fn new() -> Self {
        Self::default()
    }

    /// A pool whose turns parked for their plan end after `checks` answers without their
    /// model (at least one).
    #[must_use]
    pub fn ending_plans_after(mut self, checks: u32) -> Self {
        self.plan_checks = Some(checks.max(1));
        self
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

    /// Whether the turn was let go, at its half hour or at its reset, and runs on outside the
    /// recovery that let it go.
    fn runs_on(&self, attempt: &Attempt) -> bool {
        let key = attempt.key();
        self.park_expired.contains(&key) || self.released_at_reset.contains(&key)
    }

    /// The turns that wait for an account, in order.
    fn parked_now(&self) -> Vec<Attempt> {
        let waits = |attempt: &&Attempt| attempt.parked && attempt.owned && !self.runs_on(attempt);
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
        self.released_at_reset.retain(|key| !other(key));
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
            resets_until_ms: None,
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
        // now it would meet the same limit and stop the pool. One let go at its half hour or
        // at its reset runs all the same.
        let unserved = self.unserved_models().contains(turn.model);
        if !(self.recovering || unserved) || self.runs_on(&self.attempts[at]) {
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
        // A recovery that asks the selector less often while it waits for a reset asks it
        // within a minute again for a turn that came: its model may be served.
        if self.recovering && self.quiet_checks > 0 {
            self.quiet_checks = 0;
            self.next_check_ms = self.next_check_ms.min(now_ms + RECHECK_MS);
            out.push(Effect::Timer {
                timer: Timer::Recovery,
                after_ms: (self.next_check_ms - now_ms).max(0),
            });
        }
        // It may have taken the place of a turn a pass waited for.
        self.stopping_over(now_ms, &mut out);
        (true, out)
    }

    /// The turn is over, however it ended.
    pub fn discard(&mut self, agent: &str, trace: &str, now_ms: i64) -> Vec<Effect> {
        let mut out = Vec::new();
        let key = (agent.to_string(), trace.to_string());
        self.park_expired.remove(&key);
        self.released_at_reset.remove(&key);
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

    /// The tool of the agent's turn said when the account it ran out on has usage again. The
    /// turn, and every other waiting turn of its model, waits until then where that is longer
    /// than half an hour, and is let go then. Told after the pool took the turn
    /// ([`Pool::request`]): a turn the pool did not take waits for nothing, and says nothing
    /// of the model to the others. A time that is over, or further than a week, is no reset
    /// to wait for.
    pub fn out_until(&mut self, agent: &str, trace: &str, resets_at_ms: i64, now_ms: i64) {
        if let Some(at) = self
            .at(agent)
            .filter(|at| self.attempts[*at].trace == trace && self.attempts[*at].parked)
        {
            let until = crate::recovery::after_reset(Some(resets_at_ms), now_ms);
            self.attempts[at].resets_until_ms = until;
            if let Some(until) = until {
                let model = self.attempts[at].model.clone();
                self.resets_by_model.insert(model, until);
            }
        }
    }

    /// When the turn's wait may end at its account's reset: its own tool's word, or that of
    /// another turn of its model in this recovery.
    fn resets_until(&self, attempt: &Attempt) -> Option<i64> {
        attempt
            .resets_until_ms
            .or_else(|| self.resets_by_model.get(&attempt.model).copied())
    }

    /// A recovery is over. What its turns were told of their account stands for the turns of
    /// the model that still wait (an answer for some of the models holds the others), and is
    /// no word on the next recovery.
    fn recovered(&mut self, now_ms: i64) {
        self.recovering = false;
        self.quiet_checks = 0;
        self.forget_resets(now_ms);
    }

    /// Forgets the reset of a model that is over, or that no waiting turn waits for any more.
    fn forget_resets(&mut self, now_ms: i64) {
        let waiting: BTreeSet<String> = self
            .attempts
            .iter()
            .filter(|attempt| attempt.parked && attempt.owned)
            .map(|attempt| attempt.model.clone())
            .collect();
        self.resets_by_model
            .retain(|model, at| *at > now_ms && waiting.contains(model));
    }

    /// When the turn's own wait is half an hour old.
    fn half_hour(&self, attempt: &Attempt, now_ms: i64) -> i64 {
        let since = self.first_parked_ms.get(&attempt.key()).copied();
        since.unwrap_or(now_ms) + MAX_PARK_MS
    }

    /// Whether the turn waits past its half hour for a reset it was told: let go then, it may
    /// be parked again for a next one. A reset that came before its half hour is none.
    fn waits_for_reset(&self, attempt: &Attempt, now_ms: i64) -> bool {
        let half_hour = self.half_hour(attempt, now_ms);
        self.resets_until(attempt).is_some_and(|at| at > half_hour)
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
        // Let go at its reset, and out again: it waits once more, for the next reset its
        // tool names, from now.
        let released = self.released_at_reset.remove(&key);
        if self.recovering {
            self.attempts[trigger].plan |= plan;
            if released {
                self.attempts[trigger].parked = true;
                self.note_park(trigger, now_ms);
                out.push(Effect::Pause {
                    agent: agent.to_string(),
                });
            }
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
        // A recovery begins anew: every turn that runs is stopped for it, also one that was let
        // go at its reset by the one before.
        self.released_at_reset.clear();
        self.recovering = true;
        self.quiet_checks = 0;
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
        self.quiet_checks = 0;
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

    /// Restore the same account failure the actor persisted, including its model and reset.
    pub fn carry_turn(
        &mut self,
        turn: Starting<'_>,
        since_ms: i64,
        plan: bool,
        reset: Option<i64>,
        now: i64,
    ) -> Vec<Effect> {
        let (_, mut effects) = self.carried(true, since_ms, &[turn.agent.to_owned()], now);
        effects.extend(self.register(turn, now).1);
        if let Some(at) = self.at(turn.agent) {
            self.attempts[at].plan = plan;
        }
        if let Some(reset) = reset {
            self.out_until(turn.agent, turn.trace, reset, now);
        }
        effects
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

    /// A new Codex login or quota reading may serve waiting turns. Only the selector
    /// qualifies models. Bring its next check forward without stopping working turns.
    pub fn recheck_available(&mut self, switched: bool, now: i64) -> Vec<Effect> {
        if switched && self.pass == Some(Pass::Asking) {
            self.login_changed_while_asking = true;
        }
        if self.pass.is_some() || self.parked_now().is_empty() {
            return Vec::new();
        }
        // A retry on the same login already contradicted the last qualification.
        // Another positive quota reading of that login cannot cancel relapse backoff.
        if !switched && self.relapses > 0 && now < self.next_check_ms {
            return Vec::new();
        }
        self.next_check_ms = now;
        self.fired(Timer::Recovery, now)
    }

    /// The turns that are still their agents', the others forgotten; without those let go
    /// at their half hour or at their reset, which run and are not parked again.
    fn pending(&mut self) -> Vec<Attempt> {
        self.attempts.retain(|attempt| attempt.owned);
        let waits = |attempt: &&Attempt| !self.runs_on(attempt);
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
        // A recovery whose turns wait for a reset they were told is let go at the last of
        // them, where that is after its half hour: its clock runs from so much later.
        let reset = pending
            .iter()
            .filter_map(|attempt| self.resets_until(attempt))
            .max();
        let since = reset.map_or(self.parked_since_ms, |at| {
            self.parked_since_ms.max(at - MAX_PARK_MS)
        });
        decide(
            &work,
            verdict,
            since,
            self.next_check_ms,
            &self.checked_models,
            now_ms,
        )
    }

    /// How long until the selector is asked again, after an answer that found no account. Where
    /// every waiting turn waits for a reset it was told, nothing is likely to change before:
    /// asked a minute later, then two, four and eight minutes, and at the first reset at the
    /// latest. Otherwise a minute later, as always.
    fn next_asking(&mut self, pending: &[Attempt], checked: Checked, now_ms: i64) -> i64 {
        let first = pending
            .iter()
            .map(|attempt| self.resets_until(attempt).filter(|at| *at > now_ms))
            .collect::<Option<Vec<i64>>>()
            .and_then(|resets| resets.into_iter().min());
        let Some(first) = first.filter(|_| checked == Checked::No) else {
            self.quiet_checks = 0;
            return RECHECK_MS;
        };
        let gap = (RECHECK_MS << self.quiet_checks.min(3)).min(first - now_ms);
        self.quiet_checks += 1;
        self.next_check_ms = now_ms + gap;
        gap
    }

    /// Takes out the turns whose own wait is half an hour old, or as old as its account's
    /// reset where that is later. One that waited for no reset is never parked again. One
    /// that did runs on while the recovery waits for other models' later resets, and is
    /// parked again only where its account is out once more and its tool says until when: it
    /// waits for that reset as well, as a turn of a Host that follows the login does.
    fn take_overdue(&mut self, attempts: &[Attempt], now_ms: i64) -> Vec<Attempt> {
        let old = |attempt: &&Attempt| {
            let half_hour = self.half_hour(attempt, now_ms);
            now_ms
                >= self
                    .resets_until(attempt)
                    .map_or(half_hour, |at| at.max(half_hour))
        };
        let overdue: Vec<Attempt> = attempts.iter().filter(old).cloned().collect();
        for attempt in &overdue {
            let told = self.waits_for_reset(attempt, now_ms);
            self.take(&attempt.agent);
            if told {
                self.released_at_reset.insert(attempt.key());
            } else {
                self.park_expired.insert(attempt.key());
            }
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
        // and must not find the others still waiting. Their model is served: what was said
        // of its reset is over.
        for attempt in back {
            self.take(&attempt.agent);
        }
        self.forget_resets(now_ms);
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
            self.quiet_checks = 0;
            self.parked_since_ms = now_ms;
        } else if !parked.is_empty() {
            self.arm_hold(out);
        }
        Self::release(&overdue, out);
        // A reset that is over, or that no held turn waits for, is said no more.
        self.forget_resets(now_ms);
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
        let runs = |attempt: &Attempt| attempt.owned && !attempt.stopped && !self.runs_on(attempt);
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
            Decision::Release { .. } => self.recovered(now_ms),
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
        if std::mem::take(&mut self.login_changed_while_asking) {
            self.next_check_ms = now_ms;
            self.stopped_all(now_ms, &mut out);
            return out;
        }
        // "All" covers the request that was sent. A different model may have
        // joined the pool while the selector ran.
        let verdict = match verdict {
            Verdict::All => Verdict::Only(self.checked_models.iter().cloned().collect()),
            other => other,
        };
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
                if attempt.plan && unserved && self.checked_models.contains(&attempt.model) {
                    attempt.unserved_checks += 1;
                    if let Some(at) = self.at(&attempt.agent) {
                        self.attempts[at].unserved_checks = attempt.unserved_checks;
                    }
                    let most = self.plan_checks.unwrap_or(PLAN_CHECKS);
                    if attempt.ends && attempt.unserved_checks >= most {
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
                self.last_resumed_ms = Some(now_ms);
                // All of them out first, as above.
                for attempt in &pending {
                    self.take(&attempt.agent);
                }
                // The reset of a model whose turns are held stands for them.
                self.recovered(now_ms);
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
                if *reason == PARK_EXPIRED {
                    // Those that waited for a reset they were told may wait for the next.
                    for attempt in &pending {
                        let key = attempt.key();
                        if self.waits_for_reset(attempt, now_ms) {
                            self.released_at_reset.insert(key);
                        } else {
                            self.park_expired.insert(key);
                        }
                    }
                    self.attempts.clear();
                }
                self.recovered(now_ms);
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
            Decision::Park { delay_ms, reason } => {
                let delay_ms = if reason == "no-account" {
                    self.next_asking(&pending, checked, now_ms)
                } else {
                    self.quiet_checks = 0;
                    delay_ms
                };
                out.push(Effect::Timer {
                    timer: Timer::Recovery,
                    after_ms: delay_ms,
                });
            }
            Decision::Kill { .. } | Decision::Check { .. } => {}
        }
    }

    /// The first turn the pool knows that is still its agent's, in the order the agents came
    /// to the pool: the turn a recovery the Quota keeper asks for begins with (the
    /// coordinator's `candidates[0]` of `janitor_autonomy.runtime_recover`). Its agent and trace.
    pub fn first_owned(&self) -> Option<(String, String)> {
        let first = self.attempts.iter().find(|attempt| attempt.owned)?;
        Some((first.agent.clone(), first.trace.clone()))
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
        let mut stood = json!({
            "recovering": self.recovering,
            "waiting": self.waiting(),
            "relapses": self.relapses,
            "next_check_in_ms": (self.next_check_ms - now_ms).max(0),
            "released": released,
            "trials": trials,
            "turns": turns,
        });
        // A recovery that waits for a reset says which and how far its asking is spaced: it
        // may last hours. Absent otherwise, as the Python coordinator has no such fields.
        if !self.resets_by_model.is_empty() || self.quiet_checks > 0 {
            stood["resets_until_ms"] = json!(self.resets_by_model);
            stood["quiet_checks"] = json!(self.quiet_checks);
        }
        // The turns let go at their reset, which run on; absent where there are none.
        if !self.released_at_reset.is_empty() {
            let released: Vec<Value> = self
                .released_at_reset
                .iter()
                .map(|key| json!([key.0, key.1]))
                .collect();
            stood["released_at_reset"] = json!(released);
        }
        stood
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
    fn a_selector_answer_releases_covered_waiters_when_a_new_model_arrives() {
        let mut pool = Pool::new();
        let reset = Some(3_600_000);
        pool.carry_turn(starting("a", "1"), 0, false, reset, 1);
        pool.carry_turn(starting("b", "2"), 0, false, None, 1);
        assert_eq!(
            pool.fired(Timer::Recovery, 2),
            vec![Effect::Ask {
                models: vec!["m1".into()]
            }]
        );
        assert!(pool.register(of_model("c", "3", "m2"), 3).0);
        let effects = pool.answered(Verdict::All, 4);
        assert!(effects.contains(&resume("a")) && effects.contains(&resume("b")));
        assert!(!effects.contains(&resume("c")));
        assert_eq!(pool.waiting(), ["c"]);
        assert!(pool.answered(Verdict::All, 5).is_empty());
    }

    #[test]
    fn a_confirmed_switch_requalifies_waiters_and_discards_the_previous_logins_answer() {
        let mut pool = Pool::new().ending_plans_after(PLAN_CHECKS_HERE);
        pool.carry_turn(starting("a", "1"), 0, false, Some(3_600_000), 1);
        pool.carry_turn(starting("b", "2"), 0, false, None, 1);
        pool.carry_turn(of_model("c", "3", "unsupported"), 0, true, None, 1);
        pool.carry_turn(starting("d", "4"), 0, false, None, 1);
        let first = pool.fired(Timer::Recovery, 2);
        assert!(matches!(first.as_slice(), [Effect::Ask { .. }]));
        pool.answered(Verdict::NoAnswer, 3);
        assert_eq!(pool.recheck_available(true, 4), first);
        assert!(pool.recheck_available(true, 5).is_empty());
        pool.disown("d", 6);
        assert_eq!(pool.answered(Verdict::All, 7), first);
        let effects = pool.answered(Verdict::Only(["m1".into()].into()), 8);
        assert!(effects.contains(&resume("a")) && effects.contains(&resume("b")));
        assert!(effects.contains(&Effect::End { agent: "c".into() }));
        assert!(!effects.contains(&resume("c")) && !effects.contains(&resume("d")));
        assert!(pool.recheck_available(true, 9).is_empty());
    }

    #[test]
    fn a_resumed_turn_refused_again_waits_for_requalification() {
        let mut pool = Pool::new();
        pool.carry_turn(starting("a", "1"), 0, false, None, 1);
        let asking = pool.fired(Timer::Recovery, 2);
        assert_eq!(pool.answered(Verdict::All, 3), [resume("a")]);
        assert!(!pool.register(starting("a", "1"), 4).0);
        assert!(pool.request("a", "1", true, false, 5).0);
        pool.fired(Timer::Recovery, 5);
        pool.stopped("a", 6);
        assert!(pool.recheck_available(false, 7).is_empty());
        assert_eq!(pool.recheck_available(true, 8), asking);
        let again = pool.answered(Verdict::All, 9);
        assert_eq!(
            again,
            [Effect::Resume {
                agent: "a".into(),
                relapsed: true
            }]
        );
        assert!(pool.answered(Verdict::All, 10).is_empty());
    }

    #[test]
    fn on_this_host_a_turn_parked_for_its_plan_ends_on_the_first_answer_without_its_model() {
        let parked = |checks: Option<u32>| {
            let mut pool = match checks {
                Some(checks) => Pool::new().ending_plans_after(checks),
                None => Pool::new(),
            };
            assert!(!pool.register(starting("a", "t-a"), 0).0);
            assert!(pool.request("a", "t-a", true, true, 0).0);
            let stopping = pool.fired(Timer::Recovery, 0);
            assert!(stopping.contains(&stop("a")), "{stopping:?}");
            let asked = pool.stopped("a", 500);
            assert!(
                asked
                    .iter()
                    .any(|effect| matches!(effect, Effect::Ask { .. })),
                "{asked:?}"
            );
            pool
        };
        let ends = |effects: &[Effect]| {
            effects
                .iter()
                .any(|effect| matches!(effect, Effect::End { agent } if agent == "a"))
        };
        let mut here = parked(Some(PLAN_CHECKS_HERE));
        let answered = here.answered(Verdict::NoAccount, 1_000);
        assert!(ends(&answered), "{answered:?}");
        // A run of the selector that gave no verdict (it ended badly, took too long, said its
        // own error, could not be run) is no answer that leaves the model out: it ends nothing.
        for no_verdict in [Verdict::NoAnswer, Verdict::Failed] {
            let mut here = parked(Some(PLAN_CHECKS_HERE));
            let answered = here.answered(no_verdict.clone(), 1_000);
            assert!(!ends(&answered), "{no_verdict:?}: {answered:?}");
        }
        assert_eq!(read(Some(1), "", &[]), Verdict::NoAnswer);
        assert_eq!(read(None, "", &[]), Verdict::NoAnswer);
        assert_eq!(
            read(Some(0), r#"{"available": false, "error": "boom"}"#, &[]),
            Verdict::NoAnswer
        );
        // The Python coordinator's count waits for three such answers.
        let mut there = parked(None);
        let answered = there.answered(Verdict::NoAccount, 1_000);
        assert!(!ends(&answered), "{answered:?}");
    }

    #[test]
    fn a_turn_told_when_its_account_resets_waits_until_then_and_is_asked_for_less_often() {
        let hour = 3_600_000;
        let reset = 5 * hour;
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        let out = || {
            let mut pool = Pool::new();
            assert!(!pool.register(starting("a", "t-a"), 0).0);
            assert!(pool.request("a", "t-a", true, false, 0).0);
            pool.out_until("a", "t-a", reset, 0);
            assert!(pool.fired(Timer::Recovery, 0).contains(&stop("a")));
            assert_eq!(pool.stopped("a", 0), vec![asked.clone()]);
            pool
        };
        // No account has usage, again and again: the turn is not let go at its half hour,
        // and is taken up a minute after the reset its tool said.
        let mut pool = out();
        let (mut now, mut gaps) = (0, Vec::new());
        loop {
            let answered = pool.answered(Verdict::NoAccount, now);
            if let [Effect::Timer { timer, after_ms }] = answered.as_slice() {
                assert_eq!(*timer, Timer::Recovery);
                gaps.push(*after_ms);
                now += after_ms;
                assert_eq!(pool.fired(Timer::Recovery, now), vec![asked.clone()]);
                continue;
            }
            assert_eq!(answered, vec![resume("a")], "at {now}");
            break;
        }
        assert_eq!(now, reset + crate::recovery::RESET_MARGIN_MS);
        assert_eq!(
            gaps[..5],
            [60_000, 120_000, 240_000, 480_000, 480_000],
            "asked a minute later, then twice as far off each time, eight minutes at most"
        );
        assert!(gaps.len() < 45, "{gaps:?}");
        assert!(!pool.recovering());
        // Another account frees up meanwhile: the turn is taken up at once.
        let mut pool = out();
        assert_eq!(pool.answered(Verdict::NoAccount, 0).len(), 1);
        assert_eq!(pool.fired(Timer::Recovery, 60_000), vec![asked.clone()]);
        assert_eq!(pool.answered(Verdict::All, 60_000), vec![resume("a")]);
        // A reset that is over is none: the turn waits its half hour, asked every minute.
        let mut pool = Pool::new();
        assert!(!pool.register(starting("a", "t-a"), 0).0);
        assert!(pool.request("a", "t-a", true, false, 0).0);
        pool.out_until("a", "t-a", -hour, 0);
        pool.fired(Timer::Recovery, 0);
        pool.stopped("a", 0);
        let recheck = Effect::Timer {
            timer: Timer::Recovery,
            after_ms: RECHECK_MS,
        };
        assert_eq!(pool.answered(Verdict::NoAccount, 0), vec![recheck.clone()]);
        pool.fired(Timer::Recovery, RECHECK_MS);
        assert_eq!(pool.answered(Verdict::NoAccount, RECHECK_MS), vec![recheck]);
    }

    #[test]
    fn every_turn_of_the_model_waits_for_the_reset_one_was_told_and_waits_again_if_refused() {
        let reset = 5 * 3_600_000;
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        // Two agents run on one model. A is refused, and says when the account resets; B is
        // stopped with it and was told nothing.
        let mut pool = Pool::new();
        assert!(!pool.register(starting("a", "t-a"), 0).0);
        assert!(!pool.register(starting("b", "t-b"), 0).0);
        assert!(pool.request("a", "t-a", true, false, 0).0);
        pool.out_until("a", "t-a", reset, 0);
        let stops = pool.fired(Timer::Recovery, 0);
        assert!(stops.contains(&stop("a")) && stops.contains(&stop("b")));
        assert_eq!(pool.stopped("a", 0), vec![]);
        assert_eq!(pool.stopped("b", 0), vec![asked.clone()]);
        // No account has usage, again and again. While the selector is asked eight minutes
        // apart, C comes, of another agent: it waits too, and the selector is asked for it
        // within a minute.
        let (mut now, mut came) = (0, false);
        loop {
            let answered = pool.answered(Verdict::NoAccount, now);
            let [Effect::Timer { timer, after_ms }] = answered.as_slice() else {
                // At the reset all three go on together.
                assert_eq!(answered, vec![resume("a"), resume("b"), resume("c")]);
                break;
            };
            assert_eq!(*timer, Timer::Recovery);
            if *after_ms == 480_000 && !came {
                came = true;
                now += 1_000;
                let (parked, told) = pool.register(starting("c", "t-c"), now);
                assert!(parked, "{told:?}");
                let soon = told.iter().find_map(|effect| match effect {
                    Effect::Timer {
                        timer: Timer::Recovery,
                        after_ms,
                    } => Some(*after_ms),
                    _ => None,
                });
                assert!(soon.is_some_and(|after| after <= RECHECK_MS), "{told:?}");
                now += soon.unwrap_or(RECHECK_MS);
            } else {
                now += after_ms;
            }
            assert_eq!(
                pool.fired(Timer::Recovery, now),
                vec![asked.clone()],
                "{now}"
            );
        }
        assert!(came);
        assert_eq!(now, reset + crate::recovery::RESET_MARGIN_MS);
        assert!(!pool.recovering());
        // A runs, and the weekly window refuses it now, until two days on: it waits for that
        // reset too, where a turn that waited for none is never parked again.
        let week = now + 2 * 86_400_000;
        assert!(!pool.register(starting("a", "t-a"), now).0);
        assert!(pool.request("a", "t-a", true, false, now).0);
        pool.out_until("a", "t-a", week, now);
    }

    /// The number of the timer that looks at the held turns, among what the pool wants done.
    fn held_timer(effects: &[Effect]) -> Option<u64> {
        effects.iter().find_map(|effect| match effect {
            Effect::Timer {
                timer: Timer::Held(number),
                ..
            } => Some(*number),
            _ => None,
        })
    }

    #[test]
    fn turns_held_for_a_model_an_answer_does_not_serve_still_wait_for_its_reset() {
        let reset = 5 * 3_600_000;
        let on = |agent, trace, model| Starting {
            agent,
            trace,
            model,
            holds: true,
            ends: true,
        };
        // A and B run on one model, C on another. A is refused and says when the account
        // resets; B is stopped with it, and C too.
        let mut pool = Pool::new();
        assert!(!pool.register(on("a", "t-a", "opus"), 0).0);
        assert!(!pool.register(on("b", "t-b", "opus"), 0).0);
        assert!(!pool.register(on("c", "t-c", "sonnet"), 0).0);
        assert!(pool.request("a", "t-a", true, false, 0).0);
        pool.out_until("a", "t-a", reset, 0);
        pool.fired(Timer::Recovery, 0);
        pool.stopped("a", 0);
        pool.stopped("b", 0);
        let asked = pool.stopped("c", 0);
        assert!(
            asked
                .iter()
                .any(|effect| matches!(effect, Effect::Ask { .. })),
            "{asked:?}"
        );
        // The account now active serves the other model only: C goes on, A and B are held.
        let served = Verdict::Only(BTreeSet::from(["sonnet".to_string()]));
        let answered = pool.answered(served, 1_000);
        assert!(
            answered.contains(&resume("c")) && !answered.contains(&resume("b")),
            "{answered:?}"
        );
        assert!(!pool.register(on("c", "t-c", "sonnet"), 1_000).0, "C runs");
        // They are looked at every minute. B is not let go at its half hour: both go on at
        // the reset A was told.
        let mut held = held_timer(&answered)
            .unwrap_or_else(|| panic!("the held turns are looked at: {answered:?}"));
        let mut now = 1_000;
        let back = loop {
            now += RECHECK_MS;
            let looked = pool.fired(Timer::Held(held), now);
            if looked.contains(&resume("b")) {
                break looked;
            }
            held = held_timer(&looked).unwrap_or_else(|| panic!("at {now}: {looked:?}"));
        };
        assert!(back.contains(&resume("a")), "{back:?}");
        let at = reset + crate::recovery::RESET_MARGIN_MS;
        assert!((at..at + RECHECK_MS).contains(&now), "{now}");
        assert!(pool.stood(now).get("resets_until_ms").is_none());
    }

    #[test]
    fn a_reset_before_the_half_hour_or_of_a_turn_not_taken_lets_no_turn_wait_twice() {
        let asked = Effect::Ask {
            models: vec!["m1".to_string()],
        };
        // A's account resets in ten minutes, B was told nothing. No account has usage: both
        // wait their half hour, and neither is parked again.
        let mut pool = Pool::new();
        assert!(!pool.register(starting("a", "t-a"), 0).0);
        assert!(!pool.register(starting("b", "t-b"), 0).0);
        assert!(pool.request("a", "t-a", true, false, 0).0);
        pool.out_until("a", "t-a", 600_000, 0);
        pool.fired(Timer::Recovery, 0);
        pool.stopped("a", 0);
        assert_eq!(pool.stopped("b", 0), vec![asked.clone()]);
        let mut now = 0;
        let released = loop {
            let answered = pool.answered(Verdict::NoAccount, now);
            let [Effect::Timer { after_ms, .. }] = answered.as_slice() else {
                break answered;
            };
            now += after_ms;
            assert_eq!(pool.fired(Timer::Recovery, now), vec![asked.clone()]);
        };
        assert_eq!(now, MAX_PARK_MS);
        assert_eq!(released, vec![resume("a"), resume("b")]);
        for (agent, trace) in [("a", "t-a"), ("b", "t-b")] {
            assert!(!pool.register(starting(agent, trace), now).0);
            assert!(!pool.request(agent, trace, true, false, now).0, "{agent}");
        }
        // A turn the pool does not take waits for nothing, and tells its model nothing.
        let mut pool = Pool::new();
        assert!(!pool.register(starting("a", "t-a"), 0).0);
        assert!(!pool.request("a", "t-a", false, false, 0).0);
        pool.out_until("a", "t-a", 5 * 3_600_000, 0);
        assert!(pool.stood(0).get("resets_until_ms").is_none());
    }

    /// A turn of the model, of the agent and trace named after it.
    fn of_model<'a>(agent: &'a str, trace: &'a str, model: &'a str) -> Starting<'a> {
        Starting {
            agent,
            trace,
            model,
            holds: true,
            ends: true,
        }
    }

    /// A on opus and B on sonnet are both refused, A's account resetting at two hours and B's
    /// at five; no account has usage. The pool as it stands when the selector is first asked.
    fn two_resets() -> (Pool, i64, i64) {
        let hour = 3_600_000;
        let (opus, sonnet) = (2 * hour, 5 * hour);
        let mut pool = Pool::new();
        assert!(!pool.register(of_model("a", "t-a", "opus"), 0).0);
        assert!(!pool.register(of_model("b", "t-b", "sonnet"), 0).0);
        assert!(pool.request("a", "t-a", true, false, 0).0);
        pool.out_until("a", "t-a", opus, 0);
        assert!(pool.request("b", "t-b", true, false, 0).0);
        pool.out_until("b", "t-b", sonnet, 0);
        let stops = pool.fired(Timer::Recovery, 0);
        assert!(stops.contains(&stop("a")) && stops.contains(&stop("b")));
        assert_eq!(pool.stopped("a", 0), vec![]);
        let asked = Effect::Ask {
            models: vec!["opus".to_string(), "sonnet".to_string()],
        };
        assert_eq!(pool.stopped("b", 0), vec![asked]);
        let margin = crate::recovery::RESET_MARGIN_MS;
        (pool, opus + margin, sonnet + margin)
    }

    /// Answers no account at each asking until an answer takes a turn up or lets it go: that
    /// answer, and when it came.
    fn no_account_until_one_goes_on(pool: &mut Pool, mut now: i64) -> (Vec<Effect>, i64) {
        loop {
            let answered = pool.answered(Verdict::NoAccount, now);
            let [Effect::Timer { timer, after_ms }] = answered.as_slice() else {
                return (answered, now);
            };
            assert_eq!(*timer, Timer::Recovery);
            now += after_ms;
            let asked = pool.fired(Timer::Recovery, now);
            assert!(
                matches!(asked.as_slice(), [Effect::Ask { .. }]),
                "at {now}: {asked:?}"
            );
        }
    }

    #[test]
    fn a_turn_let_go_at_its_reset_runs_on_while_another_model_waits_for_a_later_one() {
        let (mut pool, opus, sonnet) = two_resets();
        // At A's reset A goes on, and the recovery waits on for B's.
        let (answered, now) = no_account_until_one_goes_on(&mut pool, 0);
        assert_eq!(now, opus);
        assert_eq!(answered.first(), Some(&resume("a")), "{answered:?}");
        assert!(!answered.contains(&resume("b")), "{answered:?}");
        assert!(pool.recovering());
        let gap = answered.iter().find_map(|effect| match effect {
            Effect::Timer {
                timer: Timer::Recovery,
                after_ms,
            } => Some(*after_ms),
            _ => None,
        });
        let gap = gap.unwrap_or_else(|| panic!("the recovery goes on: {answered:?}"));
        // A starts again: it is not parked behind the recovery for B's reset.
        let (parked, told) = pool.register(of_model("a", "t-a", "opus"), now);
        assert!(!parked, "{told:?}");
        assert_eq!(pool.waiting(), vec!["b".to_string()]);
        let mut fresh = pool.clone();
        // Nor is it stopped by the passes that follow, and the selector is asked for B's
        // model alone. B goes on at its own reset.
        let only_b = Effect::Ask {
            models: vec!["sonnet".to_string()],
        };
        let (mut now, mut after) = (now, gap);
        let back = loop {
            now += after;
            assert_eq!(
                pool.fired(Timer::Recovery, now),
                vec![only_b.clone()],
                "{now}"
            );
            let answered = pool.answered(Verdict::NoAccount, now);
            let [Effect::Timer { after_ms, .. }] = answered.as_slice() else {
                break answered;
            };
            after = *after_ms;
        };
        assert_eq!(back, vec![resume("b")]);
        assert_eq!(now, sonnet);
        assert!(!pool.recovering() && pool.waiting().is_empty());
        // A new failure of A's on its account, while the recovery still waits for B: it is
        // taken up again for the next reset its tool names, and waits with B.
        let week = now + 2 * 86_400_000;
        let (taken, did) = fresh.request("a", "t-a", true, false, opus);
        assert!(taken);
        assert_eq!(
            did,
            vec![Effect::Pause {
                agent: "a".to_string()
            }]
        );
        fresh.out_until("a", "t-a", week, opus);
        assert_eq!(fresh.waiting(), vec!["a".to_string(), "b".to_string()]);
        let stood = fresh.stood(opus);
        assert!(stood.get("released_at_reset").is_none(), "{stood}");
        let next = opus + gap;
        let stops = fresh.fired(Timer::Recovery, next);
        assert_eq!(stops.first(), Some(&stop("a")), "{stops:?}");
        let both = Effect::Ask {
            models: vec!["opus".to_string(), "sonnet".to_string()],
        };
        assert_eq!(fresh.stopped("a", next), vec![both]);
    }

    #[test]
    fn after_a_restart_a_turn_let_go_at_its_reset_waits_its_own_half_hour_at_most() {
        // A was let go at its reset two hours in and runs; B waits for its reset at five
        // hours. The Host restarts: what it keeps of the pool's memory is the recovery B
        // waited in, taken up with B's turn and reset. A's turn, found running, is parked
        // with B and stopped. It waits no longer than its own half hour from then, not for
        // B's reset, and is not parked again after.
        let hour = 3_600_000;
        let (opus, sonnet) = (2 * hour, 5 * hour);
        let start = opus + 5 * RECHECK_MS;
        let mut pool = Pool::new();
        let (taken, _) = pool.carried(true, 0, &["b".to_string()], start);
        assert!(taken);
        assert!(pool.register(of_model("b", "t-b", "sonnet"), start).0);
        pool.out_until("b", "t-b", sonnet, start);
        assert!(pool.register(of_model("a", "t-a", "opus"), start).0);
        let asked = Effect::Ask {
            models: vec!["opus".to_string(), "sonnet".to_string()],
        };
        assert_eq!(pool.fired(Timer::Recovery, start), vec![asked]);
        let (answered, now) = no_account_until_one_goes_on(&mut pool, start);
        assert_eq!(now, start + MAX_PARK_MS);
        assert_eq!(answered.first(), Some(&resume("a")), "{answered:?}");
        assert!(!pool.register(of_model("a", "t-a", "opus"), now).0);
        assert!(!pool.request("a", "t-a", true, false, now).0);
        assert_eq!(pool.waiting(), vec!["b".to_string()]);
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
