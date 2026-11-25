//! One `muse serve` turn written as the spool of a `muse exec` turn (issue 762), so the Host's
//! one Muse reader takes a lane turn as it takes an exec turn.
//!
//! MSP says a turn in items; exec says it in `payload_type` events. What the reader uses is
//! written: the reply's text (`run.output.delta`, whole, once per message), each finished tool
//! (`tool.result`), the usage and allowance (`run.usage`), the model (`run.model.configured`)
//! and how the turn ended (`run.terminal.*`). Beside them come the lane lines every lane writes
//! ([`crate::lane_spool`]): `steer.open`, `steer.read`, `turn.completed` / `turn.failed`, and
//! `turn.progress` while text is written.

use std::collections::BTreeMap;
use std::io;

use serde_json::{Value, json};

use crate::lane_spool::ClaimSpool;

pub struct TurnSpool {
    spool: ClaimSpool,
    session: String,
    turn: String,
    model: Option<String>,
    /// Steered words Muse accepted for this turn: its `commandId`, then the id the Host steered
    /// them under.
    steered: BTreeMap<String, String>,
    /// The subscription allowance Muse said last, in exec's shape.
    allowance: Option<Value>,
    /// A Stop interrupted the turn: its `cancelled` end is the Stop's, as exec's SIGINT is.
    stopping: bool,
}

impl TurnSpool {
    /// The turn is bound: Muse took the prompt under `turn`. Made durable before anything else.
    pub fn begin(
        mut spool: ClaimSpool,
        session: String,
        turn: String,
        pid: u32,
        model: Option<String>,
        now_ms: i64,
    ) -> io::Result<Self> {
        spool.session(&session, now_ms)?;
        spool.spawned(pid, now_ms)?;
        let mut turn = Self {
            spool,
            session,
            turn,
            model,
            steered: BTreeMap::new(),
            allowance: None,
            stopping: false,
        };
        turn.event("run.lifecycle.started", json!({}), now_ms)?;
        if let Some(model) = turn.model.clone() {
            turn.event("run.model.configured", json!({"model_id": model}), now_ms)?;
        }
        turn.spool.steer_open(now_ms)?;
        turn.spool.sync()?;
        Ok(turn)
    }

    /// Muse's id of the turn.
    pub fn turn(&self) -> &str {
        &self.turn
    }

    /// Muse accepted steered words under its `command`, sent under the Host's `id`.
    pub fn steered(&mut self, command: &str, id: &str) {
        self.steered.insert(command.to_owned(), id.to_owned());
    }

    /// The accepted steered words Muse has not said it read: their `commandId`s.
    pub fn unread(&self) -> Vec<String> {
        self.steered
            .iter()
            .filter(|(_, id)| !self.spool.was_read(id))
            .map(|(command, _)| command.clone())
            .collect()
    }

    /// These words were taken back from Muse: the Host runs them, as words never read.
    pub fn given_back(&mut self, command: &str) {
        self.steered.remove(command);
    }

    /// A Stop interrupted the turn.
    pub fn stopping(&mut self) {
        self.stopping = true;
    }

    /// The allowance Muse said (`usage/changed`), kept for the turn's `run.usage`.
    pub fn allowance(&mut self, usage: &Value) {
        let window = |block: &Value| json!({"resets_at_ms": block["resetsAtMs"], "used_percent": block["usedPercent"]});
        if usage.is_object() {
            self.allowance = Some(json!({
                "as_of_ms": usage["observedAtMs"],
                "source": "stream",
                "tier": usage["tier"],
                "weekly": window(&usage["weekly"]),
                "window": window(&usage["window"]),
            }));
        }
    }

    /// A notification of Muse's. Returns true once the turn's end is durable and the lane may let
    /// go of the claim. What belongs to another session or turn is not this turn's.
    pub fn notification(&mut self, method: &str, params: &Value, now_ms: i64) -> io::Result<bool> {
        if params["sessionId"].as_str() != Some(self.session.as_str()) {
            return Ok(false);
        }
        let item = &params["item"];
        let turn = params["turnId"].as_str().or(item["turnId"].as_str());
        // A delta names only its item; a lane runs one turn at a time, so its session's
        // deltas are this turn's.
        if method != "item/delta" && turn != Some(self.turn.as_str()) {
            return Ok(false);
        }
        match (method, item["kind"].as_str()) {
            // Steered words are read when Muse puts them in the turn as the user's message
            // with the command they were sent under (Muse 1.4.25, 24 Nov 2026).
            ("item/started" | "item/completed", Some("userMessage")) => {
                if item["steered"] == true
                    && let Some(id) = item["commandId"]
                        .as_str()
                        .and_then(|command| self.steered.get(command))
                        .cloned()
                {
                    self.spool.steer_read(&id, now_ms)?;
                }
            }
            ("item/completed", Some("agentMessage")) => {
                let text = item["text"].as_str().unwrap_or_default();
                if !text.is_empty() {
                    self.event("run.output.delta", json!({"text": text}), now_ms)?;
                }
            }
            ("item/completed", Some("toolCall")) => self.tool(item, now_ms)?,
            // The reply is kept whole when it is complete (as the Codex lane keeps it, the
            // owner's decision of 23 Nov 2026); only that the model still writes is said.
            ("item/delta", _) => self.spool.progress(now_ms)?,
            // The answer is whole, and Muse's reminder agents now hold the turn until they settle
            // (1.7-3.4 s, Muse 1.4.25). Said, so the turn ends for the user here and its claim at
            // `turn/completed`, as other tools end at their own end line and linger.
            ("turn/foregroundCompleted", _) => {
                self.spool
                    .out_json(&json!({"type": "turn.answered"}), now_ms)?;
            }
            ("turn/completed", _) => {
                self.finish(params, now_ms)?;
                return Ok(true);
            }
            _ => {}
        }
        Ok(false)
    }

    /// Muse's serve went away while the turn ran: a failed turn, with one terminal record.
    pub fn lost(&mut self, why: &str, now_ms: i64) -> io::Result<()> {
        self.event(
            "run.terminal.failed",
            json!({"terminal": "failed", "reason": why}),
            now_ms,
        )?;
        self.spool.turn_failed(why, now_ms)?;
        self.spool.exit(Some(1), None, now_ms)
    }

    /// The lane ended Muse where it stood after a Stop it did not answer in time.
    pub fn killed(&mut self, now_ms: i64) -> io::Result<()> {
        self.spool.exit(None, Some(nix::libc::SIGKILL), now_ms)
    }

    fn tool(&mut self, item: &Value, now_ms: i64) -> io::Result<()> {
        let name = item["tool"].as_str().unwrap_or("tool");
        let status = item["status"].as_str().unwrap_or("completed");
        let outcome = if status == "completed" {
            "success"
        } else {
            status
        };
        let text = item["visibleOutput"]
            .as_str()
            .or(item["failureReason"].as_str())
            .unwrap_or_default();
        let call = item["callId"]
            .as_str()
            .or(item["itemId"].as_str())
            .unwrap_or_default();
        self.event(
            "tool.result",
            json!({
                "call_id": call,
                "tool_name": name,
                "text": text,
                "correlation_facts": {"tool_name": name, "outcome": outcome},
            }),
            now_ms,
        )
    }

    fn finish(&mut self, params: &Value, now_ms: i64) -> io::Result<()> {
        let usage = &params["usage"];
        let mut row = json!({
            "input_tokens": usage["inputTokens"],
            "output_tokens": usage["outputTokens"],
        });
        if let Some(model) = &self.model {
            row["model"] = json!(model);
        }
        let mut said = json!({"models": [row]});
        if let Some(allowance) = &self.allowance {
            said["allowance"] = allowance.clone();
        }
        if usage.is_object() || self.allowance.is_some() {
            self.event("run.usage", said, now_ms)?;
        }
        match params["terminal"].as_str().unwrap_or_default() {
            "completed" => {
                self.event(
                    "run.terminal.completed",
                    json!({"terminal": "completed"}),
                    now_ms,
                )?;
                self.spool.turn_completed(now_ms)?;
                self.spool.exit(Some(0), None, now_ms)
            }
            // A Stop's end is what exec gives on SIGINT: no terminal line, the signal.
            "cancelled" if self.stopping => self.spool.exit(None, Some(nix::libc::SIGINT), now_ms),
            "cancelled" => {
                self.event(
                    "run.terminal.cancelled",
                    json!({"terminal": "cancelled"}),
                    now_ms,
                )?;
                self.spool.exit(Some(0), None, now_ms)
            }
            end => {
                let reason = params["error"]["message"]
                    .as_str()
                    .or(params["reason"].as_str())
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("Muse turn {end}"));
                self.event(
                    "run.terminal.failed",
                    json!({"terminal": "failed", "reason": reason}),
                    now_ms,
                )?;
                self.spool.turn_failed(&reason, now_ms)?;
                self.spool.exit(Some(1), None, now_ms)
            }
        }
    }

    /// One line in exec's shape: `{"payload_type": kind, "payload": payload}`.
    fn event(&mut self, kind: &str, payload: Value, now_ms: i64) -> io::Result<()> {
        self.spool
            .out_json(&json!({"payload_type": kind, "payload": payload}), now_ms)
    }
}

#[cfg(test)]
#[path = "muse_turn_tests.rs"]
mod tests;
