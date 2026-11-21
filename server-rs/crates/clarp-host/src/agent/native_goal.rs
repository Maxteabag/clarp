//! Connects the agent's normal launch orders to its persistent Codex lane.

use clarp_actor::native_goal::{NativeGoal, Ownership};
use clarp_kernel::agent_goal::{Goal, Status as GoalStatus};
use clarp_shim::codex_lane::{GoalAction, Request, Snapshot};

use crate::codex_goal_lane::{Lane, Login, Submission};

use super::{
    ActorError, Answer, Ask, ShimOrder, Told, Unavailable, Worker, launch_permitted, tell,
};

pub(super) type GoalAnswer = Answer<Result<Option<Goal>, String>>;

pub(super) struct Activation {
    action: GoalAction,
    objective: String,
    answer: GoalAnswer,
}

#[derive(Default)]
pub(super) struct Native {
    pub(super) record: Option<NativeGoal>,
    pending: Option<Activation>,
    claimed: Option<Activation>,
    watching: bool,
    retiring: bool,
    reconciling: bool,
    mutation: bool,
    retirement_failed: bool,
    /// Set only by a lane snapshot after the previous provider claim ended.
    idle_lane: bool,
    observed_lane: Option<(u32, u64)>,
    after_retire: Option<ShimOrder>,
}

impl Native {
    pub(super) fn owns(&self) -> bool {
        self.record
            .as_ref()
            .is_some_and(|record| record.lane != Ownership::Exec)
    }

    pub(super) fn goal(&self) -> Option<Goal> {
        self.record.as_ref().and_then(|record| record.goal.clone())
    }
}

impl Worker {
    fn lane(&self) -> Lane {
        Lane::new(
            self.setup
                .profile
                .run_dir
                .join(format!("{}-cxg.sock", self.setup.record.agent_id)),
        )
    }

    fn native_login(&self) -> Login {
        Login {
            lock: self
                .setup
                .pool()
                .and_then(|pool| pool.login_lock())
                .map(std::path::Path::to_owned),
            file: self.setup.backend.login_file(&crate::config::home()),
        }
    }

    pub(super) fn request_native_goal(
        &mut self,
        action: GoalAction,
        objective: String,
        answer: GoalAnswer,
    ) -> Result<(), ActorError> {
        let Some(actor) = self.actor.as_ref() else {
            let _ = answer.send(Err(Unavailable::new(
                "the agent could not be opened".into(),
            )));
            return Ok(());
        };
        if let Err(error) = actor.require_here() {
            let _ = answer.send(Err(super::unavailable(&error)));
            return Ok(());
        }
        let status = actor.status();
        let Some(thread) = status.backend_session.filter(|thread| !thread.is_empty()) else {
            let _ = answer.send(Ok(Err(
                "Send the agent a message first; a goal needs a conversation to run in.".into(),
            )));
            return Ok(());
        };
        if self.native.retiring
            || self.native.pending.is_some()
            || self.native.claimed.is_some()
            || self.native.mutation
            || self.native.reconciling
        {
            let _ = answer.send(Ok(Err("the Codex lane is changing its goal".into())));
            return Ok(());
        }
        if matches!(action, GoalAction::Start | GoalAction::Resume) {
            if status.paused {
                let _ = answer.send(Ok(Err(
                    "resume the agent's queue before activating the goal".into(),
                )));
                return Ok(());
            }
            self.native.pending = Some(Activation {
                action,
                objective,
                answer,
            });
            return self.advance_native_goal();
        }

        if !self.native.owns() {
            if status.live.is_some() {
                let _ = answer.send(Ok(Err("the Codex thread is busy".into())));
                return Ok(());
            }
            self.keep_native(NativeGoal {
                thread: thread.clone(),
                goal: self.native.goal(),
                lane: Ownership::Acquiring,
            })?;
        }
        if action == GoalAction::Pause && self.native.owns() {
            self.mark_native_stop()?;
        }
        self.native.mutation = true;
        let lane = self.lane();
        let launcher = self.setup.launcher.clone();
        let profile = self.setup.profile.clone();
        let login = self.native_login();
        let tx = self.tx.clone();
        self.setup.runtime.spawn_blocking(move || {
            let result = lane
                .start(&launcher, &profile, &login, &thread)
                .and_then(|child| {
                    reap(child);
                    lane.ask(Request::Goal { action, objective })
                });
            let _ = tx.send(Ask::NativeGoalReply {
                result: result.map_err(|e| e.to_string()),
                answer: Some(answer),
            });
        });
        Ok(())
    }

    pub(super) fn advance_native_goal(&mut self) -> Result<(), ActorError> {
        self.retire_native();
        if self.native.retiring
            || self.native.mutation
            || self.native.reconciling
            || self.setup.record.backend != "codex"
        {
            return Ok(());
        }
        if self
            .native
            .pending
            .as_ref()
            .is_some_and(|pending| pending.answer.is_closed())
        {
            self.native.pending = None;
        }
        let pending = self.native.pending.is_some();
        let continue_goal = self.native.record.as_ref().is_some_and(|record| {
            (record.lane == Ownership::AccountHold
                || (matches!(record.lane, Ownership::Owned | Ownership::SchedulerHold)
                    && self.native.idle_lane))
                && record
                    .goal
                    .as_ref()
                    .is_some_and(|goal| goal.status == GoalStatus::Active)
        });
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        if actor.moved().is_some() || self.native.claimed.is_some() || !(pending || continue_goal) {
            return Ok(());
        }
        let Some(thread) = actor.status().backend_session else {
            return Ok(());
        };
        let record = NativeGoal {
            thread,
            goal: self.native.goal(),
            lane: if pending {
                Ownership::Acquiring
            } else {
                self.native
                    .record
                    .as_ref()
                    .map_or(Ownership::Owned, |record| record.lane)
            },
        };
        let now = self.setup.clock.now_ms();
        if let Some((_, out)) = actor.reserve_native_goal(&record, now)? {
            self.native.record = Some(record);
            self.native.idle_lane = false;
            self.native.claimed = self.native.pending.take();
            self.after(out);
        }
        Ok(())
    }

    pub(super) fn launch_on_native_lane(&mut self, order: ShimOrder) {
        self.native.idle_lane = false;
        if self.native.retiring {
            self.native.after_retire = Some(order);
            return;
        }
        let Some(record) = self.native.record.as_ref() else {
            return;
        };
        let thread = record.thread.clone();
        let stopping = record.lane == Ownership::Stopping;
        let restore = matches!(
            record.lane,
            Ownership::AccountHold | Ownership::SchedulerHold
        );
        let private = self
            .actor
            .as_ref()
            .and_then(|actor| actor.status().live)
            .is_some_and(|live| live.origin == clarp_kernel::turn::Origin::NativeGoal);
        let activation = if private {
            self.native.claimed.take()
        } else {
            None
        };
        self.native.mutation |= activation.is_some();
        let (submission, answer) = match activation {
            Some(Activation {
                action,
                objective,
                answer,
            }) => (Submission::Activate { action, objective }, Some(answer)),
            None if private && restore => (
                Submission::Activate {
                    action: GoalAction::Resume,
                    objective: String::new(),
                },
                None,
            ),
            None if private => (Submission::Recover, None),
            None => (
                Submission::Prompt {
                    model: self.setup.profile.model.clone(),
                    effort: self.setup.profile.effort.clone(),
                },
                None,
            ),
        };
        let (turn, attempt, socket) = (order.turn, order.attempt, order.socket.clone());
        let spool = order.spool.clone();
        let lane = self.lane();
        let launcher = self.setup.launcher.clone();
        let profile = self.setup.profile.clone();
        let login = self.native_login();
        let tx = self.tx.clone();
        self.setup.runtime.spawn_blocking(move || {
            let result = (|| {
                if !launch_permitted(&tx, turn, attempt) {
                    return Err(std::io::Error::other(
                        "the actor no longer admits this attempt",
                    ));
                }
                reap(lane.start(&launcher, &profile, &login, &thread)?);
                if stopping {
                    lane.recover_stop()?;
                }
                if !launch_permitted(&tx, turn, attempt) {
                    return Err(std::io::Error::other("the actor stopped this attempt"));
                }
                lane.submit(order, submission)
            })();
            // An absent reply cannot prove that Codex did not take the attempt.
            // The supervisor creates the spool before handing the request over.
            let pending = spool.try_exists().unwrap_or(true)
                || result.as_ref().is_ok_and(|state| {
                    state
                        .claim
                        .as_ref()
                        .is_some_and(|claim| (claim.turn, claim.attempt) == (turn.0, attempt))
                });
            let error = result.as_ref().err().map(ToString::to_string);
            let _ = tx.send(Ask::NativeGoalReply {
                result: result.map_err(|e| e.to_string()),
                answer,
            });
            if pending {
                super::launcher::follow(&socket, &|out| tell(&tx, turn, attempt, out));
            } else {
                // The lane may have completed between the reply and attaching.
                // Read its ordinary spool before trying to settle the claim.
                tell(&tx, turn, attempt, Told::SpoolChanged);
                if let Some(error) = error {
                    tell(&tx, turn, attempt, Told::Failed(error));
                } else {
                    tell(&tx, turn, attempt, Told::Gone);
                }
            }
        });
    }

    pub(super) fn keep_native(&mut self, record: NativeGoal) -> Result<(), ActorError> {
        if self.native.record.as_ref() == Some(&record) {
            return Ok(());
        }
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        actor.keep_native_goal(&record, now)?;
        let changed = self.native.goal() != record.goal;
        self.native.record = Some(record);
        self.publish_view();
        if changed {
            self.setup.hub.publish(
                serde_json::json!({
                    "type": "goal-updated",
                    "agent_id": self.setup.record.agent_id,
                    "session": self.setup.record.session,
                    "goal": self.native.goal().as_ref().map(Goal::public),
                }),
                now,
            );
        }
        Ok(())
    }

    pub(super) fn stop_native_goal(&mut self) -> Result<(), ActorError> {
        for activation in self
            .native
            .pending
            .take()
            .into_iter()
            .chain(self.native.claimed.take())
        {
            let _ = activation
                .answer
                .send(Ok(Err("the goal request was stopped".into())));
        }
        let active = self
            .actor
            .as_ref()
            .is_some_and(|actor| actor.status().live.is_some());
        if self.native.owns() {
            self.mark_native_stop()?;
            if !active {
                let lane = self.lane();
                let tx = self.tx.clone();
                self.setup.runtime.spawn_blocking(move || {
                    let result = lane.ask(Request::Goal {
                        action: GoalAction::Pause,
                        objective: String::new(),
                    });
                    let _ = tx.send(Ask::NativeGoalReply {
                        result: result.map_err(|e| e.to_string()),
                        answer: None,
                    });
                });
            }
        }
        Ok(())
    }

    pub(super) fn mark_native_stop(&mut self) -> Result<(), ActorError> {
        if self.native.owns()
            && let Some(mut record) = self.native.record.clone()
        {
            record.lane = Ownership::Stopping;
            self.keep_native(record)?;
        }
        Ok(())
    }

    pub(super) fn hold_native_for_account(&mut self) -> Result<(), ActorError> {
        if let Some(mut record) = self.native.record.clone()
            && record.lane == Ownership::Owned
            && record
                .goal
                .as_ref()
                .is_some_and(|goal| goal.status == GoalStatus::Active)
        {
            record.lane = Ownership::AccountHold;
            self.keep_native(record)?;
        }
        Ok(())
    }

    pub(super) fn observe_native(&mut self, state: Snapshot) -> Result<(), ActorError> {
        let Some(mut record) = self.native.record.clone() else {
            return Ok(());
        };
        if record.thread != state.thread || record.lane == Ownership::Exec || !state.known_goal {
            return Ok(());
        }
        if self
            .native
            .observed_lane
            .is_some_and(|(instance, revision)| {
                instance == state.instance && revision >= state.revision
            })
        {
            return Ok(());
        }
        self.native.observed_lane = Some((state.instance, state.revision));
        self.native.idle_lane = state.available;
        let mut next = match state.goal {
            None => None,
            Some(raw) => {
                let Some(goal) = Goal::from_codex(&raw, record.goal.as_ref(), self.now_ms()) else {
                    return Ok(());
                };
                Some(goal)
            }
        };
        if (matches!(
            record.lane,
            Ownership::AccountHold | Ownership::SchedulerHold
        ) || (state.deferred_goal && record.lane != Ownership::Stopping))
            && let Some(goal) = next.as_mut()
            && (goal.status == GoalStatus::Paused || state.deferred_goal)
        {
            if record.lane != Ownership::AccountHold {
                record.lane = Ownership::SchedulerHold;
            }
            // The account pool paused the tool on its way to the next login.
            // Its reserved next attempt keeps the user's active objective.
            goal.status = GoalStatus::Active;
        } else if record.lane != Ownership::Stopping
            || next
                .as_ref()
                .is_none_or(|goal| goal.status != GoalStatus::Active)
        {
            record.lane = Ownership::Owned;
        }
        if record.goal != next {
            self.native.retirement_failed = false;
        }
        record.goal = next;
        self.keep_native(record)?;
        self.watch_native();
        Ok(())
    }

    pub(super) fn finish_native_reply(
        &mut self,
        result: Result<Snapshot, String>,
        answer: Option<GoalAnswer>,
    ) -> Result<(), ActorError> {
        if answer.is_some() {
            self.native.mutation = false;
        }
        let result = match result {
            Ok(state) => {
                self.observe_native(state)?;
                Ok(self.native.goal())
            }
            Err(error) => {
                self.reconcile_native();
                Err(error)
            }
        };
        if let Some(answer) = answer {
            let _ = answer.send(Ok(result));
        }
        Ok(())
    }

    pub(super) fn lost_native_watch(&mut self) {
        self.native.watching = false;
        self.native.idle_lane = false;
        self.reconcile_native();
    }

    /// Resolve unknown activation by a provider read. A recorded Stop can
    /// repeat its pause after observing that the provider still reports active.
    pub(super) fn reconcile_native(&mut self) {
        let Some(record) = self.native.record.as_ref() else {
            return;
        };
        if self.native.reconciling
            || self.native.mutation
            || !(matches!(record.lane, Ownership::Acquiring | Ownership::Stopping)
                || (matches!(record.lane, Ownership::Owned | Ownership::SchedulerHold)
                    && record
                        .goal
                        .as_ref()
                        .is_some_and(|goal| goal.status == GoalStatus::Active)
                    && !self.native.watching))
        {
            return;
        }
        self.native.reconciling = true;
        let stopping = record.lane == Ownership::Stopping;
        let thread = record.thread.clone();
        let lane = self.lane();
        let launcher = self.setup.launcher.clone();
        let profile = self.setup.profile.clone();
        let login = self.native_login();
        let tx = self.tx.clone();
        self.setup.runtime.spawn_blocking(move || {
            let result = lane
                .start(&launcher, &profile, &login, &thread)
                .and_then(|child| {
                    reap(child);
                    if stopping {
                        lane.recover_stop()
                    } else {
                        lane.ask(Request::Probe)
                    }
                });
            let _ = tx.send(Ask::NativeGoalReconciled(
                result.map_err(|error| error.to_string()),
            ));
        });
    }

    pub(super) fn finish_native_reconcile(
        &mut self,
        result: Result<Snapshot, String>,
    ) -> Result<(), ActorError> {
        self.native.reconciling = false;
        if let Ok(state) = result {
            self.observe_native(state)?;
        }
        Ok(())
    }

    pub(super) fn retire_native(&mut self) {
        let Some(record) = self.native.record.as_ref() else {
            return;
        };
        if self.native.retiring
            || self.native.retirement_failed
            || record.lane != Ownership::Owned
            || record
                .goal
                .as_ref()
                .is_some_and(|goal| goal.status != GoalStatus::Complete)
            || self
                .actor
                .as_ref()
                .is_some_and(|actor| actor.status().live.is_some())
        {
            return;
        }
        self.native.retiring = true;
        let lane = self.lane();
        let tx = self.tx.clone();
        let thread = record.thread.clone();
        self.setup.runtime.spawn_blocking(move || {
            let result = lane.ask(Request::Retire).or_else(|error| {
                if lane.released()? {
                    Ok(Snapshot {
                        thread: thread.clone(),
                        instance: 0,
                        revision: 0,
                        known_goal: false,
                        deferred_goal: false,
                        available: false,
                        claim: None,
                        native_turn: None,
                        goal: None,
                    })
                } else {
                    Err(error)
                }
            });
            let _ = tx.send(Ask::NativeGoalRetired {
                thread,
                released: result.is_ok(),
            });
        });
    }

    pub(super) fn finish_native_retirement(
        &mut self,
        thread: &str,
        released: bool,
    ) -> Result<(), ActorError> {
        self.native.retiring = false;
        self.native.retirement_failed = !released;
        if released
            && let Some(mut record) = self.native.record.clone()
            && record.thread == thread
        {
            record.lane = Ownership::Exec;
            self.keep_native(record)?;
        }
        if let Some(order) = self.native.after_retire.take() {
            self.carry_out(clarp_actor::Action::Launch(order));
        }
        Ok(())
    }

    pub(super) fn watch_native(&mut self) {
        if !self.native.owns() || self.native.watching {
            return;
        }
        self.native.watching = true;
        let lane = self.lane();
        let tx = self.tx.clone();
        let watcher = std::thread::Builder::new()
            .name("codex-lane-watch".into())
            .spawn(move || {
                let _ = lane.watch(|state| tx.send(Ask::NativeGoalSeen(state)).is_ok());
                let _ = tx.send(Ask::NativeGoalWatchLost);
            });
        if watcher.is_err() {
            self.native.watching = false;
        }
    }
}

fn reap(child: Option<std::process::Child>) {
    if let Some(mut child) = child {
        let _ = std::thread::Builder::new()
            .name("codex-lane-reaper".into())
            .spawn(move || {
                let _ = child.wait();
            });
    }
}
