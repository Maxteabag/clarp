//! An OpenCode or DeepSeek agent's chat turns on its kept `opencode serve` (issue 763), while
//! the owner's `opencode_chat_lane` is on. No turn waits for the lane: a turn runs on it only
//! when it is ready now, and otherwise on its own server as before, with the lane started
//! beside it for the turns after.
//!
//! One exception (issue 857): a turn whose lane is there and free, but was started under another
//! login alone (a seat switch, a refreshed token: `auth.json` changed and nothing else), or that
//! the lane refused because its login changed, starts the new lane itself in place of its own
//! server's start, which costs the same, and runs on it. Once; with no place for a lane, or a
//! start that fails, it runs on its own server as before.

use std::path::PathBuf;

use clarp_actor::ShimOrder;

use super::{Ask, Told, Worker, launch_permitted, tell};
use crate::lane::opencode::{Lane, Start, Words};
use crate::lane::{LoginChanged, Settings, TURN_LEAVE_WAIT, Tool, Want};

/// What the worker keeps of its OpenCode lane between turns.
#[derive(Default)]
pub(super) struct OpenCode {
    /// The attempt the lane refused before anything reached OpenCode: it runs on its own server.
    own_server: Option<(u64, u32)>,
}

/// A directory of the XDG kind, as the server will see it: the turn's environment first, then
/// the Host's, then the default under the home.
fn xdg(env: &[(String, String)], name: &str, default: &str) -> PathBuf {
    env.iter()
        .rev()
        .find(|(key, _)| key == name)
        .map(|(_, value)| PathBuf::from(value))
        .or_else(|| std::env::var_os(name).map(PathBuf::from))
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| crate::config::home().join(default))
}

impl Worker {
    /// Whether this agent's chat turns may run on its OpenCode lane.
    pub(super) fn opencode_lane(&self) -> bool {
        Tool::of(&self.setup.record.backend) == Some(Tool::OpenCode)
            && Settings::read().chat(Tool::OpenCode)
    }

    fn opencode_lane_of(&self) -> Lane {
        Lane::of(&self.setup.profile.run_dir, &self.setup.record.agent_id)
    }

    /// What the agent's lane is started with for this turn, and its digest.
    fn opencode_start(&self, order: &ShimOrder) -> Option<Start> {
        let named = clarp_backend::program(&self.setup.record.backend)?;
        Start::of(
            &named,
            &order.cwd,
            &order.env,
            &xdg(&order.env, "XDG_CONFIG_HOME", ".config"),
            &xdg(&order.env, "XDG_DATA_HOME", ".local/share"),
        )
    }

    /// Runs the turn on the agent's lane when the lane is ready for it now. `Some`: it is not,
    /// and the turn runs on its own server; the lane is started beside it.
    pub(super) fn opencode_on_lane(&mut self, order: Box<ShimOrder>) -> Option<Box<ShimOrder>> {
        if self.opencode.own_server.take() == Some((order.turn.0, order.attempt)) {
            return Some(order);
        }
        // No lane can be kept for this turn (no program, a configuration folder past what is
        // read at each turn, an input that cannot be read): it runs on its own server.
        let Some(start) = self.opencode_start(&order) else {
            return Some(order);
        };
        let Ok(words) = Words::of(&order) else {
            return Some(order);
        };
        let lane = self.opencode_lane_of();
        // The lane there differs from the one this turn needs by the login alone: the turn
        // starts the new one itself. Any other difference runs it on its own server, with the
        // lane started beside it, so a turn of other options never ends a lane the next needs.
        let ready = lane.ready(&start.digest, &words.session);
        let renew = !ready && lane.another_login(&start.digest, &words.session);
        if !(ready || renew) {
            return Some(order);
        }
        let token = self
            .setup
            .turns
            .token(&self.setup.record.agent_id, order.turn.0);
        let tx = self.tx.clone();
        let (launcher, profile) = (self.setup.launcher.clone(), self.setup.profile.clone());
        self.setup.runtime.spawn_blocking(move || {
            let (turn, attempt, socket) = (order.turn, order.attempt, order.socket.clone());
            let session = words.session.clone();
            // The lane under the login there now, in place of the turn's own server: the one
            // there, idle, leaves first (`place`). Whether it is ready.
            let begin = || {
                // In place of a lane that has just left: a start that still finds its owner there
                // is tried once more once it has let go, all within the turn's bound.
                let started =
                    crate::lane::start_once_more(lane.endpoint(), TURN_LEAVE_WAIT, || {
                        lane.start(&launcher, &profile, &start, &session, Want::Optional)
                    });
                match started {
                    Ok(Some(mut child)) => {
                        let _ = std::thread::Builder::new()
                            .name("opencode-lane-reaper".into())
                            .spawn(move || {
                                let _ = child.wait();
                            });
                    }
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!("clarp-host: the OpenCode lane did not start: {error}");
                    }
                }
                lane.ready(&start.digest, &session)
            };
            if renew && !begin() {
                // No place, or the start failed: the turn runs on its own server.
                let _ = tx.send(Ask::OpenCodeOwnServer(order));
                return;
            }
            let permitted = || {
                let admitted = launch_permitted(&tx, turn, attempt);
                if !admitted {
                    tell(
                        &tx,
                        turn,
                        attempt,
                        Told::Failed("the actor no longer admits this attempt".into()),
                    );
                }
                admitted
            };
            if !permitted() {
                return;
            }
            let reached = || order.spool.try_exists().unwrap_or(true);
            let mut result = lane.submit(&order, words.clone(), token.clone());
            // Refused because the login changed, with nothing of the turn reaching OpenCode: the
            // lane is on its way out. Once it has gone, the turn starts the new lane, as above,
            // and is offered to it; once, never again.
            if let Err(error) = &result
                && LoginChanged::is(error)
                && !reached()
            {
                // At most TURN_LEAVE_WAIT, never ending it: one that has not let go by then is
                // left, and the turn runs on its own server at once (issue 860).
                let gone = lane.endpoint().gone_within(TURN_LEAVE_WAIT);
                if gone.unwrap_or(false) && begin() {
                    if !permitted() {
                        return;
                    }
                    result = lane.submit(&order, words, token);
                }
            }
            // The lane makes the turn's spool before anything reaches OpenCode.
            if reached() {
                super::launcher::follow(&socket, &|out| tell(&tx, turn, attempt, out));
            } else if result.is_err() {
                // Refused before it began (the session was still held, the lane left): the
                // same attempt runs on its own server, at once.
                let _ = tx.send(Ask::OpenCodeOwnServer(order));
            } else {
                tell(&tx, turn, attempt, Told::Gone);
            }
        });
        None
    }

    /// The worker's answer to a lane that refused an attempt before it began.
    /// It passed the terminal and the pool's gates once already: it waits only for a terminal
    /// opened meanwhile, as a Codex attempt that found no lane place does.
    pub(super) fn opencode_own_server(&mut self, order: Box<ShimOrder>) {
        self.opencode.own_server = Some((order.turn.0, order.attempt));
        if self.terminals > 0 {
            self.behind_terminal = Some(order);
        } else {
            self.carry_out_now(clarp_actor::Action::Launch(order));
        }
    }

    /// For a turn that runs on its own server: what it must do first, and what the lane does
    /// beside it. A lane that holds this session lets it go first (its lock would refuse the
    /// turn); with the switch on, a lane is then started with no session, for the next turn.
    /// Blocking: runs on the turn's thread, before its launch.
    pub(super) fn opencode_beside(&self, order: &ShimOrder) -> Option<Beside> {
        if Tool::of(&self.setup.record.backend) != Some(Tool::OpenCode) {
            return None;
        }
        Some(Beside {
            lane: self.opencode_lane_of(),
            start: self
                .opencode_lane()
                .then(|| self.opencode_start(order))
                .flatten(),
            launcher: self.setup.launcher.clone(),
            profile: self.setup.profile.clone(),
            want: crate::lane::Want::Optional,
        })
    }
}

/// What a turn on its own server does with the agent's lane.
pub(super) struct Beside {
    lane: Lane,
    /// The lane to start, with the switch on.
    start: Option<Start>,
    launcher: std::sync::Arc<crate::launcher::Launcher>,
    profile: clarp_actor::Profile,
    /// Whether the lane may be started, or only one that runs already used.
    want: crate::lane::Want,
}

impl Beside {
    /// Before the turn starts: a lane that holds `session` lets it go.
    pub(super) fn make_way(&self, session: &str) {
        if !session.is_empty() && self.lane.holds(session) {
            let _ = self.lane.retire();
        }
    }

    /// As the turn starts: the lane is started beside it, without the session, on a thread of
    /// its own so that nothing of the turn waits for it.
    pub(super) fn start(self, session: String) {
        let Some(start) = self.start else {
            return;
        };
        let _ = std::thread::Builder::new()
            .name("opencode-lane-start".into())
            .spawn(move || {
                match self
                    .lane
                    .start(&self.launcher, &self.profile, &start, &session, self.want)
                {
                    Ok(Some(mut child)) => {
                        let _ = child.wait();
                    }
                    Ok(None) => {}
                    Err(error) => {
                        eprintln!("clarp-host: the OpenCode lane did not start: {error}");
                    }
                }
            });
    }
}
