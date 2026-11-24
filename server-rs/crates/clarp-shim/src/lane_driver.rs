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
    fn end_turn(&mut self) -> io::Result<()>;

    /// The lane's health, as Inspect, Watch and every reply say it.
    fn health(&mut self) -> Self::Health;
}

