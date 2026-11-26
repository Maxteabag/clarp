//! One app-server execution written in the ordinary Codex turn spool format.
//!
//! Two lines are a lane's own. `steer.open` says the turn takes more of the user's words while
//! it runs; an exec turn never prints it. `steer.read` names the id of words Codex took into
//! the turn: it is the only evidence that a steered message was read. `turn.progress` says the
//! model is still writing, at most every [`PROGRESS_EVERY_MS`].

use std::collections::BTreeSet;
use std::io;
use std::path::Path;

use clarp_spool::{Entry, Record, ShimEvent, SpoolWriter};
use serde_json::{Value, json};

/// How long the model may write text or reasoning without a line in the spool. Text is not kept
/// as it is written, so a long reply with nothing else in it would leave the spool silent, and
/// the Host counts a turn's silence from its last line (the stall check; the lead's ruling of
/// 24 Nov 2026).
pub const PROGRESS_EVERY_MS: i64 = 30_000;

pub struct TurnSpool {
    writer: SpoolWriter,
    thread: String,
    turn: String,
    total: Option<(u64, u64)>,
    /// The ids of steered words Codex accepted for this turn.
    steered: BTreeSet<String>,
    /// The ids whose reading is in the spool.
    read: BTreeSet<String>,
    /// When the last line of Codex's output was written.
    last_out: i64,
}

impl TurnSpool {
    /// The Host has already reserved the turn and supplied its attempt's path.
    /// Exclusive spool creation is the durable receipt that the claim was taken.
    pub fn begin(
        path: &Path,
        thread: String,
        turn: String,
        pid: u32,
        now_ms: i64,
    ) -> io::Result<Self> {
        Self::bind(SpoolWriter::create(path)?, thread, turn, pid, now_ms, false)
    }

    /// `started` says the lane started the thread for this turn: only then is its name told
    /// to the conversation, as exec tells a thread it made.
    pub fn bind(
        writer: SpoolWriter,
        thread: String,
        turn: String,
        pid: u32,
        now_ms: i64,
        started: bool,
    ) -> io::Result<Self> {
        let mut spool = Self {
            writer,
            thread,
            turn,
            total: None,
            steered: BTreeSet::new(),
            read: BTreeSet::new(),
            last_out: now_ms,
        };
        spool.record(
            Entry::Shim(ShimEvent::Session {
                id: spool.thread.clone(),
            }),
            now_ms,
        )?;
        spool.record(Entry::Shim(ShimEvent::Spawned { pid }), now_ms)?;
        // As exec names a thread it made. A resumed thread is the conversation's already, and a
        // goal's turn says nothing of it, as before (review of 23 Nov 2026).
        if started {
            spool.output(
                json!({"type": "thread.started", "thread_id": spool.thread}),
                now_ms,
            )?;
        }
        spool.output(json!({"type": "turn.started"}), now_ms)?;
        spool.output(json!({"type": "steer.open"}), now_ms)?;
        spool.writer.sync()?;
        Ok(spool)
    }

    /// Codex accepted steered words under this id.
    pub fn steered(&mut self, id: &str) {
        self.steered.insert(id.to_owned());
    }

    /// The accepted ids whose reading is not in the spool.
    pub fn unread(&self) -> Vec<String> {
        self.steered.difference(&self.read).cloned().collect()
    }

    /// Codex took the words of this id into the turn, by its own record (a `userMessage` item
    /// with that `clientId`). Written once, and made durable before anything is said of it: a
    /// restarted Host reads the same answer. `confirmed`: it counts also when it is found after
    /// a Stop (issue 933).
    pub fn receipt(&mut self, id: &str, now_ms: i64) -> io::Result<()> {
        if id.is_empty() || !self.read.insert(id.to_owned()) {
            return Ok(());
        }
        self.output(
            json!({"type": "steer.read", "id": id, "confirmed": true}),
            now_ms,
        )?;
        self.writer.sync()
    }

    /// The lane ended Codex where it stood after a Stop it did not answer in time.
    pub fn killed(&mut self, now_ms: i64) -> io::Result<()> {
        self.exit(None, Some(nix::libc::SIGKILL), now_ms)
    }

    /// Store an event only for the claimed execution. Returns true once the
    /// terminal record is durable and the lane can let go of this claim.
    pub fn notification(&mut self, method: &str, params: &Value, now_ms: i64) -> io::Result<bool> {
        let thread = params["threadId"]
            .as_str()
            .or(params["turn"]["threadId"].as_str());
        let turn = params["turnId"].as_str().or(params["turn"]["id"].as_str());
        if thread != Some(self.thread.as_str()) || turn != Some(self.turn.as_str()) {
            return Ok(false);
        }
        match method {
            // The user's words in the turn are no item of the reply. Their id, where they
            // carry one, is the reading of steered words (23 Nov 2026: Codex 0.167.1 says a
            // steered input as a `userMessage` whose `clientId` is the id it was sent under,
            // as soon as it takes it into the turn).
            "item/started" | "item/completed" if params["item"]["type"] == "userMessage" => {
                if let Some(id) = params["item"]["clientId"].as_str() {
                    self.receipt(id, now_ms)?;
                }
            }
            "item/started" | "item/updated" | "item/completed" => {
                if let Some(item) = normalize_item(&params["item"]) {
                    let step = method.replace('/', ".");
                    self.output(json!({"type": step, "item": item}), now_ms)?;
                }
            }
            // A reply is kept whole when it is complete. The apps do not show text as it is
            // written (the owner's decision of 23 Nov 2026), and a line per delta that carried
            // the whole text so far made a spool grow with the square of the reply. Only that the
            // model still writes is said, now and then.
            "item/agentMessage/delta"
            | "item/reasoning/textDelta"
            | "item/reasoning/summaryTextDelta"
            | "item/reasoning/summaryPartAdded" => {
                if now_ms.saturating_sub(self.last_out) >= PROGRESS_EVERY_MS {
                    self.output(json!({"type": "turn.progress"}), now_ms)?;
                }
            }
            "thread/tokenUsage/updated" => {
                let total = &params["tokenUsage"]["total"];
                self.total = total["inputTokens"]
                    .as_u64()
                    .zip(total["outputTokens"].as_u64())
                    .or(self.total);
            }
            "turn/completed" => {
                self.finish(&params["turn"], now_ms)?;
                return Ok(true);
            }
            _ => {}
        }
        Ok(false)
    }

    /// The turn ended for the reason given, as a failure of its own, with one terminal record:
    /// the lane ended Codex for running on another provider than its agent's.
    pub fn refused(&mut self, why: &str, now_ms: i64) -> io::Result<()> {
        self.output(json!({"type": "turn.failed", "error": why}), now_ms)?;
        self.exit(Some(1), None, now_ms)
    }

    /// A lost app-server is a failed ordinary turn, with one terminal record.
    pub fn lost(&mut self, now_ms: i64) -> io::Result<()> {
        self.output(
            json!({"type": "turn.failed", "error": "Codex app-server disconnected"}),
            now_ms,
        )?;
        self.exit(Some(1), None, now_ms)
    }

    fn finish(&mut self, turn: &Value, now_ms: i64) -> io::Result<()> {
        match turn["status"].as_str() {
            Some("completed") => {
                let usage = self
                    .total
                    .map(|(input, output)| json!({"input_tokens": input, "output_tokens": output}));
                self.output(json!({"type": "turn.completed", "usage": usage}), now_ms)?;
                self.exit(Some(0), None, now_ms)
            }
            Some("interrupted") => self.exit(None, Some(nix::libc::SIGINT), now_ms),
            _ => {
                let error = turn["error"]["message"]
                    .as_str()
                    .unwrap_or("Codex turn failed");
                self.output(json!({"type": "turn.failed", "error": error}), now_ms)?;
                self.exit(Some(1), None, now_ms)
            }
        }
    }

    fn output(&mut self, value: Value, now_ms: i64) -> io::Result<()> {
        self.last_out = self.last_out.max(now_ms);
        self.record(Entry::Out(value.to_string()), now_ms)
    }

    fn record(&mut self, entry: Entry, now_ms: i64) -> io::Result<()> {
        self.writer.append(&Record { t: now_ms, entry })
    }

    fn exit(&mut self, code: Option<i32>, signal: Option<i32>, now_ms: i64) -> io::Result<()> {
        self.record(Entry::Shim(ShimEvent::Exited { code, signal }), now_ms)?;
        self.writer.sync()
    }
}

fn normalize_item(raw: &Value) -> Option<Value> {
    let kind = match raw["type"].as_str()? {
        "agentMessage" => "agent_message",
        "commandExecution" => "command_execution",
        "fileChange" => "file_change",
        "mcpToolCall" => "mcp_tool_call",
        "dynamicToolCall" => "dynamic_tool_call",
        "collabToolCall" => "collab_tool_call",
        "collabAgentToolCall" => "collab_agent_tool_call",
        "webSearch" => "web_search_call",
        "imageView" => "image_view",
        "imageGeneration" => "image_generation",
        "contextCompaction" => "context_compaction",
        kind => kind,
    };
    let mut item = raw.as_object()?.clone();
    item.insert("type".into(), json!(kind));
    for (native, shown) in [
        ("aggregatedOutput", "aggregated_output"),
        ("exitCode", "exit_code"),
    ] {
        if let Some(value) = item.remove(native) {
            item.insert(shown.into(), value);
        }
    }
    Some(Value::Object(item))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use clarp_spool::SpoolReader;

    #[test]
    fn a_long_reply_says_it_is_still_written_at_most_every_thirty_seconds() {
        let dir = std::env::temp_dir().join(format!("clarp-turn-progress-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("turn.jsonl");
        let mut spool = TurnSpool::begin(&path, "thread".into(), "turn".into(), 1, 0).unwrap();
        let params = json!({"threadId": "thread", "turnId": "turn", "itemId": "m", "delta": "x"});
        for (method, at) in [
            ("item/agentMessage/delta", 1_000),
            ("item/reasoning/textDelta", 10_000),
            ("item/agentMessage/delta", 31_500),
            ("item/reasoning/summaryTextDelta", 40_000),
            ("item/agentMessage/delta", 62_000),
        ] {
            assert!(!spool.notification(method, &params, at).unwrap());
        }
        let item = json!({"threadId": "thread", "turnId": "turn",
            "item": {"type": "agentMessage", "id": "m", "text": "done"}});
        spool.notification("item/completed", &item, 70_000).unwrap();
        // Another turn's deltas say nothing of this one.
        let other = json!({"threadId": "thread", "turnId": "other", "itemId": "m", "delta": "x"});
        spool
            .notification("item/agentMessage/delta", &other, 200_000)
            .unwrap();
        spool
            .notification("item/agentMessage/delta", &params, 90_000)
            .unwrap();

        let progress: Vec<i64> = SpoolReader::new(path)
            .read_from(0)
            .unwrap()
            .records
            .iter()
            .filter(|record| matches!(&record.entry, Entry::Out(line) if line.contains("turn.progress")))
            .map(|record| record.t)
            .collect();
        // At 31.5 s and 62 s; not at 90 s, 20 s after the whole reply was written.
        assert_eq!(progress, [31_500, 62_000]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
