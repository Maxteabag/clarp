//! Runs an agent's Claude chat turns on its kept Claude Code while the owner's
//! `claude_chat_lane` is on (issue 761). No turn waits for a cold lane: a turn runs on the lane
//! that is there for its conversation, started as the turn would start Claude Code, or, where
//! none is, starts its Claude Code as the lane and runs on that (issue 833): a queued turn, and
//! an idle agent's first, start one process and keep it. A turn for which no lane may start
//! (no place) runs in a process of its own, and a lane may be started once that process is
//! gone. A turn the lane refused because
//! the login changed (a seat switch: the lane leaves, and nothing of the turn reached Claude Code)
//! starts the lane anew under the current login the same way, once, and runs on it (issue 849).

use std::path::PathBuf;
use std::sync::mpsc;

use clarp_actor::ShimOrder;
use clarp_kernel::turn::{COMPACTION, TurnId};

use clarp_shim::claude_lane::Snapshot;

use crate::lane::claude::{self, Fit, Lane, Mcp};

use super::{Action, Ask as WorkerAsk, Told, Worker, launch_permitted, launcher, tell};

/// What a Claude lane's launch asks of the agent's thread.
pub(super) enum Ask {
    /// The lane did not take the attempt: it runs in a process of its own.
    Missed(Box<ShimOrder>),
    /// Whether a lane may be started now: the agent runs no turn, has no terminal open on its
    /// tool and is not moving, and the owner's switch is still on.
    Permit(mpsc::SyncSender<bool>),
    /// The lane's first attempt: its background tasks are its own from here.
    Boundary {
        turn: TurnId,
        attempt: u32,
        answer: mpsc::SyncSender<Result<(), String>>,
    },
}

/// What starts an agent's lane, carried to the thread of a turn of its own process.
pub(super) struct Start {
    pub(super) lane: Lane,
    pub(super) run: PathBuf,
    tx: mpsc::Sender<WorkerAsk>,
    login: claude::Login,
}

/// What a turn with no lane there needs to start its Claude Code as the lane.
struct AsLane {
    start: Start,
    launcher: std::sync::Arc<crate::launcher::Launcher>,
}

impl AsLane {
    /// Starts the turn's Claude Code as the lane, with what the turn would start it with
    /// (`order`), within the limits and the reserve, under the login there now: the lane reads
    /// it as it starts. The lane, once it is there and free; `None`: none started (no place, or
    /// the start failed), and the turn runs in a process of its own.
    fn begin(&self, order: &ShimOrder, fit: &Fit, mcp: &Mcp) -> Option<Snapshot> {
        self.warm(self.start(order, fit, mcp), fit)
    }

    /// The same in place of a lane that has just left for its login: a start that still finds
    /// the old lane's owner there is tried once more once it has let go (issue 857).
    fn begin_after_leaving(&self, order: &ShimOrder, fit: &Fit, mcp: &Mcp) -> Option<Snapshot> {
        let endpoint = self.start.lane.endpoint();
        let started = crate::lane::start_once_more(endpoint, crate::lane::LEAVE_WAIT, || {
            self.start(order, fit, mcp)
        });
        self.warm(started, fit)
    }

    /// Whether a lane was started: `false` where none was (no place within the limits).
    fn start(&self, order: &ShimOrder, fit: &Fit, mcp: &Mcp) -> std::io::Result<bool> {
        let start = &self.start;
        let child = start
            .lane
            .start(&self.launcher, &start.run, order, fit, mcp, &start.login)?;
        let started = child.is_some();
        reap(child);
        Ok(started)
    }

    /// The lane a start made, once it is there and free.
    fn warm(&self, started: std::io::Result<bool>, fit: &Fit) -> Option<Snapshot> {
        match started {
            Ok(true) => self.start.lane.warm(fit),
            Ok(false) => None,
            Err(error) => {
                eprintln!("claudeLaneStartFail: {error}");
                None
            }
        }
    }
}

impl Start {
    /// Starts the lane for the next turn, with what this turn's process was started with, once
    /// that process is gone. Nothing is started where the agent's thread says no.
    pub(super) fn warm(
        self,
        launcher: &crate::launcher::Launcher,
        order: &ShimOrder,
        fit: &Fit,
        mcp: &Mcp,
    ) {
        let hold = self.lane.hold();
        let _held = claude::held(&hold);
        let (answer, permit) = mpsc::sync_channel(1);
        let asked = self
            .tx
            .send(WorkerAsk::ClaudeLane(Ask::Permit(answer)))
            .is_ok();
        if !(asked && permit.recv().unwrap_or(false)) {
            return;
        }
        // The conversation is there by now: the lane resumes what this turn made.
        let order = claude::resuming(order);
        match self
            .lane
            .start(launcher, &self.run, &order, fit, mcp, &self.login)
        {
            Ok(child) => reap(child),
            Err(error) => eprintln!("claudeLaneStartFail: {error}"),
        }
    }
}

impl Worker {
    pub(super) fn claude_lane(&self) -> Lane {
        Lane::of(&self.setup.profile.run_dir, &self.setup.record.agent_id)
    }

    pub(super) fn claude_lane_start(&self) -> Start {
        Start {
            lane: self.claude_lane(),
            run: self.setup.profile.run_dir.clone(),
            tx: self.tx.clone(),
            login: claude::Login {
                lock: self
                    .setup
                    .pool()
                    .and_then(|pool| pool.login_lock())
                    .map(std::path::Path::to_owned),
                file: Some(claude::login_file(&crate::config::home())),
                credentials: self.setup.backend.login_file(&crate::config::home()),
            },
        }
    }

    /// Whether this launch goes to the agent's Claude lane: a Claude agent's turn, while the
    /// owner's switch is on, that is no opening of a call and no compaction (the lane leaves
    /// before one, and the turn after it starts a new process on the shortened conversation),
    /// and that the lane has not already turned away.
    pub(super) fn claude_lane_turn(&self, order: &ShimOrder) -> bool {
        self.setup.record.backend == "claude"
            && order.opening.is_none()
            && self.claude_bypass != Some((order.turn, order.attempt))
            && !self
                .actor
                .as_ref()
                .and_then(|actor| actor.status().live)
                .is_some_and(|live| live.origin.name() == COMPACTION)
            && self.chat_lane()
    }

    pub(super) fn launch_on_claude_lane(&mut self, order: Box<ShimOrder>) {
        // The turn as its own process would be started with it, before it is wrapped for one.
        let mut lane_order = (*order).clone();
        let agent_id = self.setup.record.agent_id.clone();
        if let Some(settings) = self.setup.launcher.claude_settings(&agent_id) {
            crate::conversation_forks::add_settings(&mut lane_order.args, &settings);
        }
        // Who the turn is, for the Clarp tools it calls: the lane keeps it in its turn file.
        let token = self.setup.turns.token(&agent_id, order.turn.0);
        let mcp: Mcp = (self.setup.mcp_strict, self.setup.record.mcp_servers.clone());
        let lane = self.claude_lane();
        // Where no lane is there for it, the turn starts its Claude Code as the lane.
        let as_lane = AsLane {
            start: self.claude_lane_start(),
            launcher: self.setup.launcher.clone(),
        };
        let (versions, runtime) = (self.setup.versions.clone(), self.setup.runtime.clone());
        let tx = self.tx.clone();
        let (turn, attempt) = (order.turn, order.attempt);
        let spawned = std::thread::Builder::new()
            .name(format!("turn-{}", turn.0))
            .spawn(move || {
                let missed = |order| {
                    let _ = tx.send(WorkerAsk::ClaudeLane(Ask::Missed(order)));
                };
                runtime.block_on(launcher::gate_steering(&mut lane_order, &versions));
                let Some(fit) = claude::fits(&lane_order, &mcp) else {
                    return missed(order);
                };
                let hold = lane.hold();
                let held = claude::held(&hold);
                let state = match lane.warm(&fit) {
                    Some(state) => Some(state),
                    // None there: the turn's own Claude Code starts as the lane.
                    None => as_lane.begin(&lane_order, &fit, &mcp),
                };
                let Some(state) = state else {
                    drop(held);
                    return missed(order);
                };
                // The attempt offered to a lane that is there: `None` once the turn has been told
                // what became of it.
                let offer = |state: Snapshot| {
                    if state.claims == 0 {
                        let (answer, marked) = mpsc::sync_channel(1);
                        let boundary = Ask::Boundary {
                            turn,
                            attempt,
                            answer,
                        };
                        let mark = if tx.send(WorkerAsk::ClaudeLane(boundary)).is_ok() {
                            marked
                                .recv()
                                .unwrap_or_else(|_| Err("the agent has closed".into()))
                        } else {
                            Err("the agent has closed".into())
                        };
                        if let Err(why) = mark {
                            let why = format!("could not record provider turn boundary: {why}");
                            tell(&tx, turn, attempt, Told::Failed(why));
                            return None;
                        }
                    }
                    if !launch_permitted(&tx, turn, attempt) {
                        return None;
                    }
                    Some(lane.submit(&lane_order, token.clone()))
                };
                let Some(mut submitted) = offer(state) else {
                    return;
                };
                let reached = || order.spool.try_exists().unwrap_or(true);
                // Refused because the login changed (a seat switch), with nothing of the attempt
                // reaching Claude Code: the lane is on its way out. Once it has gone, the turn
                // starts its Claude Code as the lane under the login there now, still within the
                // hold, and runs on it; once, never again for this attempt.
                if let Err(error) = &submitted
                    && crate::lane::LoginChanged::is(error)
                    && !reached()
                {
                    let until = std::time::Instant::now() + crate::lane::LEAVE_WAIT;
                    if lane.endpoint().let_go_within(until, true).unwrap_or(false)
                        && let Some(state) = as_lane.begin_after_leaving(&lane_order, &fit, &mcp)
                    {
                        let Some(again) = offer(state) else {
                            return;
                        };
                        submitted = again;
                    }
                }
                drop(held);
                match submitted {
                    // Nothing of the attempt reached Claude Code (no spool): the lane left
                    // between the look and the offer, or no lane could start under the new
                    // login. It runs in a process of its own, and the lane is started again
                    // after it.
                    Err(_) if !reached() => missed(order),
                    _ => launcher::follow(&order.socket, &|told| tell(&tx, turn, attempt, told)),
                }
            });
        if let Err(error) = spawned {
            tell(
                &self.tx,
                turn,
                attempt,
                Told::Failed(format!("could not start the turn's thread: {error}")),
            );
        }
    }

    pub(super) fn claude_lane_ask(&mut self, ask: Ask) {
        match ask {
            Ask::Missed(order) => {
                self.claude_bypass = Some((order.turn, order.attempt));
                if self.terminals > 0 {
                    self.behind_terminal = Some(order);
                } else {
                    self.carry_out_now(Action::Launch(order));
                }
            }
            Ask::Permit(answer) => {
                let free = self.terminals == 0
                    && self.behind_terminal.is_none()
                    && self.held.is_none()
                    && self.actor.as_ref().is_some_and(|actor| {
                        actor.moved().is_none() && actor.status().live.is_none()
                    })
                    && self.chat_lane();
                let _ = answer.send(free);
            }
            Ask::Boundary {
                turn,
                attempt,
                answer,
            } => {
                let _ = answer.send(self.provider_turn_boundary(turn, attempt));
            }
        }
    }

    /// Ends the agent's Claude lane, once idle, before something else takes its conversation
    /// (a terminal on its tool, or a move) or when the tool's login changed. Taken in turn
    /// with a lane's start, so one that is starting now is ended too.
    pub(super) fn end_claude_lane(&self) {
        if self.setup.record.backend != "claude" {
            return;
        }
        let lane = self.claude_lane();
        let _ = std::thread::Builder::new()
            .name("claude-lane-end".into())
            .spawn(move || {
                let hold = lane.hold();
                let _held = claude::held(&hold);
                // Bounded as every retirement is: a lane that does not let go in time is ended
                // by its process id.
                match lane.retire_within(crate::lane::LEAVE_WAIT) {
                    Ok(true) => {}
                    Ok(false) => eprintln!("claudeLaneEndFail: the Claude lane did not let go"),
                    Err(error) => eprintln!("claudeLaneEndFail: {error}"),
                }
            });
    }
}

fn reap(child: Option<std::process::Child>) {
    if let Some(mut child) = child {
        let _ = std::thread::Builder::new()
            .name("claude-lane-reaper".into())
            .spawn(move || {
                let _ = child.wait();
            });
    }
}
