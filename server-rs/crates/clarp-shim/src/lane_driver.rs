//! The seam between a lane process and its tool (issue 761). What every lane keeps for itself
//! is shared (`lane_own`, `lane_login`, `lane_socket`); what a tool does in its own words is a
//! [`Driver`]. Four are planned: the Codex app-server over JSON-RPC (`codex_lane_runtime`, the
//! only one built), Claude Code's stream-json on stdin, Muse's MSP over stdio, and OpenCode's
//! serve. The Host's side of the same seam is `clarp-host`'s `lane::Driver`.
//!
//! How each planned tool answers the seam, for whoever writes its driver:
//! - send a turn: Codex `turn/start`; Claude a `user` line on stdin; Muse an MSP request on
//!   stdin; OpenCode a prompt request to its server;
//! - steer: Codex `turn/steer`; Claude a `user` line on stdin, read between tool calls; Muse
//!   and OpenCode as their protocol allows, or refused;
//! - Stop: the tool's own interrupt first (Codex `turn/interrupt`, Claude an interrupt
//!   `control_request`, OpenCode its abort request), the process group ended after `grace`
//!   when the turn still runs (decision D26);
//! - end of turn: Codex `turn/completed`, Claude its `result` line, Muse and OpenCode their
//!   own end; the claim is let go and the lane is idle again;
//! - health: the lane's snapshot, which every Host reads as a `clarp_shim::lane::Status`.
//!
//! The warm state, for a driver that never makes a turn wait for a cold lane (`lane::Cold::Beside`
//! in the Host):
//! - **Warm, no session.** The lane spawns and initializes its tool beside a per-turn process
//!   (Muse in 0.35-0.58 s). It holds no conversation and touches none, says `warming: true` in
//!   its status, and takes no claim.
//! - **The hand-over.** Given its conversation at start or by `Control::Take`, the lane waits in
//!   its own process for the conversation's lock as `Holder::Lane` (`lane_own::Take::Waiting`).
//!   It gets the lock once the per-turn process has exited and released it.
//! - **Ready.** It then takes the session up (Muse `session/resume`, OpenCode
//!   `GET /session/:id`), says `warming: false`, and the next turn's claim runs on it.
//!
//! What a driver may leave out: goal requests (`Reserve`, `Goal`, `Recover`, `Probe`) are Codex's
//! own (`codex_lane::Request`), since only its tool keeps goals; another driver's control loop
//! reads `clarp_shim::lane::Control`, and a request it does not know is not answered. Which
//! attempts a lane takes at all, and which changes start it again, the Host's driver says
//! (`lane::Driver::takes`, `restart_for`): an attempt a lane does not take never reaches it.

use std::io;
use std::time::Duration;

use clarp_shim::lane::Claim;
use serde::Serialize;

/// A turn the Host admitted, as a lane is handed it.
pub(super) struct Turn {
    pub(super) claim: Claim,
    /// The message the turn starts with.
    pub(super) text: String,
    pub(super) model: Option<String>,
    pub(super) effort: Option<String>,
    /// The turn's identity for the Clarp tools it calls, kept in the lane's turn file.
    pub(super) token: Option<String>,
}

/// What a lane's tool does in its own words. `attempt` is the claim's (turn, attempt): a request
/// for any other is refused, since the claim it names is no longer the lane's.
pub(super) trait Driver {
    /// The tool's name, in the words of what the lane says.
    const TOOL: &'static str;
    /// What the lane says of itself: its driver's snapshot. It carries every field of
    /// `clarp_shim::lane::Status`.
    type Health: Serialize;

    /// Hands the admitted turn to the tool. Nothing reached the tool when this fails before the
    /// claim's spool was made: the Host may offer the attempt again.
    fn send_turn(&mut self, turn: Turn) -> io::Result<()>;

    /// Hands steered words, under the id their reading is told by, to the running turn.
    fn steer(&mut self, attempt: (u64, u32), id: &str, text: &str) -> io::Result<()>;

    /// Stops the running turn: the tool's own interrupt first, its group ended once `grace` has
    /// passed with the turn still running.
    fn stop(&mut self, attempt: (u64, u32), grace: Duration) -> io::Result<()>;

    /// The tool said the claim's turn is over: the claim is settled and let go, the login
    /// released, the idle clock started again, and what the turn left running ended.
    ///
    /// A tool that runs words steered during its final answer as a follow-up turn of their own
    /// in the same process (Muse 0.08-0.4 s after the answer, with the steer's `commandId` as the
    /// follow-up's id; Claude Code past its `result`) does not end the claim at the first
    /// turn's end. The claim follows the follow-up. The lane writes, in order:
    /// - the first turn's end;
    /// - `{"type":"steer.read","id":<the steer's id>,"separate":true}` when the follow-up starts;
    /// - the follow-up's own lines;
    /// - the follow-up's end.
    /// It calls this only then. The actor takes the follow-up on its separate-turn path
    /// (`clarp_kernel::steering::Observation::Separate`): a turn of its own with its own reply,
    /// neither counted as the first turn nor dropped. Codex reads every steered word inside its
    /// running turn and never writes it.
    fn end_turn(&mut self) -> io::Result<()>;

    /// The claim's turn must end while its tool is still busy with it: a Stop whose grace passed
    /// with the turn still running (today's one cause), or an attempt that is no longer the
    /// actor's. The turn ends now and is told as killed, once. A tool whose own abort is
    /// bounded and sure (OpenCode's `POST /session/:id/abort`, then its idle) may keep its
    /// process; any other ends its process group, and the lane leaves.
    fn abort(&mut self) -> io::Result<()>;

    /// The lane's health, as Inspect, Watch and every reply say it.
    fn health(&mut self) -> Self::Health;
}
