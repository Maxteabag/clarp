//! Runs an agent's Muse chat turns on its kept `muse serve` while the owner's `muse_chat_lane` is
//! on (issue 762). No turn waits for a cold lane: a turn goes to the lane when one holds its
//! session, ready. A turn whose session is there but held by no lane (a turn queued behind an
//! exec turn, whose lane could not take the session while the next turn was already live) has
//! the lane take it first, starting the lane where none runs (issue 833). Any other runs on exec
//! (a conversation's first turn, which names the session), while a lane starts beside it on a
//! thread of its own, without the session. Muse refuses a second process on a session it holds (its own
//! `.session.lock`), so who holds the session next is decided in one place: the agent's thread,
//! or the live turn's own launch, which no other take can run beside (the agent's thread defers
//! launches while it takes, and takes only while no turn is live):
//!
//! - once an exec turn's process is gone, its thread asks the agent's thread ([`Ask::MuseTake`]);
//!   the agent's thread lets the lane take the session only while no turn runs, no terminal is
//!   open and the agent is not moving, and holds every launch of the agent until the take has
//!   answered ([`Ask::MuseTaken`]): a turn never starts exec while the lane is taking the session;
//! - before any exec of the session, a lane that holds it leaves, waited for within
//!   [`LEAVE_WAIT`]; one that does not let go in time fails the attempt in words, since exec
//!   could not have the session anyway. A lane that runs a turn is never ended for it.
//!
//! A turn the lane refused because the login changed (it leaves, and nothing of the turn reached
//! Muse) has a lane start under the login there now and take the session, as a turn no lane holds
//! does, once, and runs on it (issue 849).

use clarp_actor::{Action, ShimOrder};
use clarp_kernel::turn::TurnId;

use crate::lane::muse::Lane;
use crate::lane::{LEAVE_WAIT, TURN_LEAVE_WAIT};
use crate::lane::{Settings, Tool};

use super::{Ask, Told, Worker, launch_permitted, launcher, tell};

/// The agent's thread's part in its Muse lane.
#[derive(Default)]
pub(super) struct State {
    /// The attempt the lane gave back, which runs on exec.
    bypass: Option<(TurnId, u32)>,
    /// The lane is taking the session: no launch of the agent's goes ahead until it answers.
    taking: bool,
    /// The launch that waits for the take.
    waiting: Option<Box<ShimOrder>>,
}

/// What an exec turn of a Muse agent does about the agent's lane, carried to the turn's thread.
pub(super) struct Beside {
    lane: Lane,
    profile: clarp_actor::Profile,
    /// The session exec runs: the one a lane would take.
    session: Option<String>,
    tx: std::sync::mpsc::Sender<Ask>,
}

impl Beside {
    /// Before exec starts: a lane that holds the session leaves, within [`LEAVE_WAIT`]. An error
    /// says why exec cannot have the session.
    pub(super) fn make_way(&self) -> Result<(), String> {
        let holds = self
            .lane
            .ask(&clarp_shim::muse_lane::Request::Inspect)
            .is_ok_and(|state| state.loaded);
        if !holds {
            return Ok(());
        }
        match self.lane.endpoint().retire_within(LEAVE_WAIT) {
            Ok(true) => Ok(()),
            Ok(false) => Err(format!(
                "the agent's Muse lane did not let go of its session within {} s",
                LEAVE_WAIT.as_secs()
            )),
            Err(error) => Err(format!(
                "the agent's Muse lane holds its session and did not leave: {error}"
            )),
        }
    }

    /// With the owner's switch on, a lane starts on a thread of its own beside the exec turn,
    /// without the session: the turn does not wait for it.
    pub(super) fn warm(
        &self,
        launcher: &crate::launcher::Launcher,
    ) -> Option<std::thread::JoinHandle<()>> {
        let session = self.switched_on()?.to_owned();
        let (lane, profile, launcher) = (self.lane.clone(), self.profile.clone(), launcher.clone());
        std::thread::Builder::new()
            .name("muse-lane-warm".into())
            .spawn(move || match lane.warm(&launcher, &profile, &session) {
                Ok(child) => reap(child),
                Err(error) => eprintln!("clarp-host: the Muse lane did not start: {error}"),
            })
            .ok()
    }

    /// Exec's process is gone and the lane started beside it is ready: the agent's thread decides
    /// whether the lane takes the session now.
    pub(super) fn after(&self, warming: Option<std::thread::JoinHandle<()>>) {
        if let Some(warming) = warming {
            let _ = warming.join();
        }
        if self.switched_on().is_some() {
            let _ = self.tx.send(Ask::MuseTake);
        }
    }

    fn switched_on(&self) -> Option<&str> {
        self.session
            .as_deref()
            .filter(|_| Settings::read().chat(Tool::Muse))
    }
}

impl Worker {
    pub(super) fn muse_lane(&self) -> Lane {
        Lane::of(&self.setup.profile.run_dir, &self.setup.record.agent_id)
    }

    /// What the exec turn `order` of a Muse agent does about its lane: `None` for another tool.
    pub(super) fn muse_beside(&self, order: &ShimOrder) -> Option<Beside> {
        (self.setup.record.backend == "muse").then(|| Beside {
            lane: self.muse_lane(),
            profile: self.setup.profile.clone(),
            session: order.named_session.clone(),
            tx: self.tx.clone(),
        })
    }

    /// A launch of a Muse agent while its lane takes the session waits for the take.
    pub(super) fn muse_holds(&mut self, order: Box<ShimOrder>) -> Option<Box<ShimOrder>> {
        if self.setup.record.backend == "muse" && self.muse.taking {
            self.muse.waiting = Some(order);
            return None;
        }
        Some(order)
    }

    /// The launch that waits for the take, when its turn is stopped: it is gone at once.
    pub(super) fn muse_stopped_waiting(&mut self, turn: TurnId) -> bool {
        if self
            .muse
            .waiting
            .as_ref()
            .is_none_or(|order| order.turn != turn)
        {
            return false;
        }
        let attempt = self.muse.waiting.take().map_or(1, |order| order.attempt);
        tell(&self.tx, turn, attempt, Told::Gone);
        true
    }

    /// Whether `order` is offered to the agent's Muse lane: a Muse chat turn, the switch on, not
    /// an opening, and not an attempt the lane already gave back.
    pub(super) fn muse_lane_turn(&self, order: &ShimOrder) -> bool {
        self.setup.record.backend == "muse"
            && order.opening.is_none()
            && self.muse.bypass != Some((order.turn, order.attempt))
            && self.chat_lane()
    }

    /// An exec turn's thread asks that the lane take the session: only while no turn runs, no
    /// terminal is open and the agent is not moving, and the switch is still on. Launches wait
    /// for the take's answer.
    pub(super) fn muse_take(&mut self) {
        let free = self.terminals == 0
            && self.behind_terminal.is_none()
            && self
                .actor
                .as_ref()
                .is_some_and(|actor| actor.moved().is_none() && actor.status().live.is_none());
        if self.muse.taking || !free || !self.chat_lane() {
            return;
        }
        self.muse.taking = true;
        let lane = self.muse_lane();
        let tx = self.tx.clone();
        self.setup.runtime.spawn_blocking(move || {
            if let Err(error) = lane.take() {
                eprintln!("clarp-host: the Muse lane did not take its session: {error}");
            }
            let _ = tx.send(Ask::MuseTaken);
        });
    }

    /// The take answered: a launch that waited for it goes on.
    pub(super) fn muse_taken(&mut self) {
        self.muse.taking = false;
        if let Some(order) = self.muse.waiting.take() {
            self.carry_out_now(Action::Launch(order));
        }
    }

    /// Offers the attempt to the lane. One the lane does not take, with nothing of it reaching
    /// Muse, comes back ([`Ask::MuseMissed`]) and runs on exec.
    pub(super) fn launch_on_muse_lane(&mut self, order: Box<ShimOrder>) {
        let lane = self.muse_lane();
        let profile = self.setup.profile.clone();
        // A lane may start for this turn, or take its session: only once a turn has named the
        // session (a conversation's first turn names it on exec, as Muse opens no other).
        let named = self
            .actor
            .as_ref()
            .and_then(|actor| actor.status().backend_session)
            .is_some_and(|session| order.named_session.as_deref() == Some(session.as_str()));
        let launcher = named.then(|| self.setup.launcher.clone());
        let token = self
            .setup
            .turns
            .token(&self.setup.record.agent_id, order.turn.0);
        let tx = self.tx.clone();
        self.setup.runtime.spawn_blocking(move || {
            let (turn, attempt) = (order.turn, order.attempt);
            let ready = |lane: &Lane| {
                order
                    .named_session
                    .as_deref()
                    .is_some_and(|session| lane.ready(&profile, session, order.model.as_deref()))
            };
            // No lane holds the session: the turn before this one ran on exec and its process
            // is gone, so the lane takes the session now, started first where none runs. A
            // session that is not there yet, or one a process still holds, is refused, and the
            // turn runs on exec as before.
            let begin = || {
                let (Some(launcher), Some(session)) = (&launcher, order.named_session.as_deref())
                else {
                    return false;
                };
                match lane.warm(launcher, &profile, session) {
                    Ok(child) => reap(child),
                    Err(error) => eprintln!("clarp-host: the Muse lane did not start: {error}"),
                }
                lane.take().is_ok() && ready(&lane)
            };
            if !(ready(&lane) || begin()) {
                let _ = tx.send(Ask::MuseMissed(order));
                return;
            }
            if !launch_permitted(&tx, turn, attempt) {
                tell(
                    &tx,
                    turn,
                    attempt,
                    Told::Failed("the actor no longer admits this attempt".into()),
                );
                return;
            }
            let mut submitted = lane.submit(&order, token.clone());
            let reached = || order.spool.try_exists().unwrap_or(true);
            // Refused because the login changed, with nothing of the attempt reaching Muse: the
            // lane is on its way out. Once it has gone, a lane starts under the login there now and
            // takes the session, as for a turn no lane holds, and the attempt is offered to it;
            // once, never again (issue 849).
            if let Err(error) = &submitted
                && crate::lane::LoginChanged::is(error)
                && launcher.is_some()
                && !reached()
            {
                // At most TURN_LEAVE_WAIT, never ending it: one that has not let go by then is
                // left, and the turn runs on exec at once (issue 860).
                let gone = lane.endpoint().gone_within(TURN_LEAVE_WAIT);
                if gone.unwrap_or(false) && begin() {
                    if !launch_permitted(&tx, turn, attempt) {
                        tell(
                            &tx,
                            turn,
                            attempt,
                            Told::Failed("the actor no longer admits this attempt".into()),
                        );
                        return;
                    }
                    submitted = lane.submit(&order, token);
                }
            }
            // The lane makes the spool before anything reaches Muse: with no spool, nothing did,
            // and the attempt runs on exec instead of waiting for another lane.
            if reached() {
                launcher::follow(&order.socket, &|out| tell(&tx, turn, attempt, out));
            } else {
                if let Err(error) = submitted {
                    eprintln!("clarp-host: the Muse lane did not take a turn: {error}");
                }
                let _ = tx.send(Ask::MuseMissed(order));
            }
        });
    }

    /// The lane did not take the attempt: it runs on exec, past what already admitted it (the
    /// account pool), and behind a terminal opened meanwhile, as Claude's lane does it.
    pub(super) fn muse_missed(&mut self, order: Box<ShimOrder>) {
        self.muse.bypass = Some((order.turn, order.attempt));
        if self.terminals > 0 {
            self.behind_terminal = Some(order);
        } else {
            self.carry_out_now(Action::Launch(order));
        }
    }

    /// The agent's Muse lane leaves, bounded as the terminal's own wait for an idle lane is: before
    /// a terminal opens on the conversation or the conversation moves to another Host. One
    /// writer per session.
    pub(super) fn end_muse_lane(&self) {
        if self.setup.record.backend != "muse" {
            return;
        }
        let lane = self.muse_lane();
        let _ = std::thread::Builder::new()
            .name("muse-lane-end".into())
            .spawn(move || match lane.endpoint().retire_within(LEAVE_WAIT) {
                Ok(true) => {}
                Ok(false) => eprintln!("clarp-host: the Muse lane did not leave in time"),
                Err(error) => eprintln!("clarp-host: the Muse lane did not leave: {error}"),
            });
    }
}

fn reap(child: Option<std::process::Child>) {
    if let Some(mut child) = child {
        let _ = std::thread::Builder::new()
            .name("muse-lane-reaper".into())
            .spawn(move || {
                let _ = child.wait();
            });
    }
}
