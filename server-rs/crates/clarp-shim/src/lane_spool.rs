//! The spool of one claim of a lane, whatever tool the lane keeps (issues 762 and 763): the
//! attempt's ordinary turn spool, written by the lane process instead of a per-turn shim. The
//! driver calls each line; nothing is written by itself.
//!
//! - [`ClaimSpool::create`] makes the spool exclusively and syncs it before anything of the
//!   attempt reaches the tool: a Host that asks again knows by it that the attempt was taken.
//! - Then the records a per-turn shim writes: the session, the tool's pid, its output and
//!   error lines, and one end ([`ClaimSpool::exit`]).
//! - Beside them, the lane lines the actor reads of a lane's turn: `steer.open` (it takes
//!   steered words), `steer.read` (the tool read these: the only evidence; `confirmed` when the
//!   tool's own record says so), `turn.completed` /
//!   `turn.failed`, and `turn.progress` while the model writes with nothing else to say.

use std::collections::BTreeSet;
use std::io;
use std::path::Path;

use clarp_spool::{Entry, Record, ShimEvent, SpoolWriter};
use serde_json::{Value, json};

/// How long the model may write text or reasoning without a line in the spool. The Host counts a
/// turn's silence from its last line (the stall check; the lead's ruling of 24 Nov 2026).
pub const PROGRESS_EVERY_MS: i64 = 30_000;

pub struct ClaimSpool {
    writer: SpoolWriter,
    /// When the last output line was written.
    last_out: i64,
    /// The steered words whose reading is written.
    read: BTreeSet<String>,
    /// Of those, the ones whose reading is written confirmed.
    confirmed: BTreeSet<String>,
    ended: bool,
}

impl ClaimSpool {
    /// Makes the attempt's spool, which must not exist yet, and syncs it.
    pub fn create(path: &Path, now_ms: i64) -> io::Result<Self> {
        let mut writer = SpoolWriter::create(path)?;
        writer.sync()?;
        Ok(Self {
            writer,
            last_out: now_ms,
            read: BTreeSet::new(),
            confirmed: BTreeSet::new(),
            ended: false,
        })
    }

    /// The tool's session the attempt runs in.
    pub fn session(&mut self, id: &str, now_ms: i64) -> io::Result<()> {
        self.record(
            Entry::Shim(ShimEvent::Session { id: id.to_owned() }),
            now_ms,
        )
    }

    /// The tool's process that runs the attempt.
    pub fn spawned(&mut self, pid: u32, now_ms: i64) -> io::Result<()> {
        self.record(Entry::Shim(ShimEvent::Spawned { pid }), now_ms)
    }

    /// A line of the tool's output, as the Host's reader of the tool reads it.
    pub fn out(&mut self, line: &str, now_ms: i64) -> io::Result<()> {
        self.last_out = self.last_out.max(now_ms);
        self.record(Entry::Out(line.to_owned()), now_ms)
    }

    pub fn out_json(&mut self, value: &Value, now_ms: i64) -> io::Result<()> {
        self.out(&value.to_string(), now_ms)
    }

    /// A line of the tool's error output.
    pub fn err(&mut self, text: &str, now_ms: i64) -> io::Result<()> {
        self.record(Entry::Err(text.to_owned()), now_ms)
    }

    /// The model still writes: said only after [`PROGRESS_EVERY_MS`] with no output line.
    pub fn progress(&mut self, now_ms: i64) -> io::Result<()> {
        if now_ms.saturating_sub(self.last_out) < PROGRESS_EVERY_MS {
            return Ok(());
        }
        self.out_json(&json!({"type": "turn.progress"}), now_ms)
    }

    /// The turn takes steered words while it runs.
    pub fn steer_open(&mut self, now_ms: i64) -> io::Result<()> {
        self.out_json(&json!({"type": "steer.open"}), now_ms)
    }

    /// The tool read the steered words of `id`, by its own record of them. Written once, and
    /// durable before anything is said of it: a restarted Host reads the same answer. The line
    /// says `confirmed`: such a reading counts also when it is found after a Stop (issue 933).
    /// A reading assumed before is written again, once, confirmed: after a Stop the Host did not
    /// count the assumption, and only this keeps the words from running again.
    pub fn steer_read(&mut self, id: &str, now_ms: i64) -> io::Result<()> {
        self.reading(id, true, now_ms)
    }

    /// The lane counts the steered words of `id` as read without the tool's own record of them:
    /// they could not be taken back, or their reading is inferred from other words. It counts as
    /// a reading only while the turn was not stopped: after a Stop the Host gives such words back,
    /// as it always did.
    pub fn steer_assumed(&mut self, id: &str, now_ms: i64) -> io::Result<()> {
        self.reading(id, false, now_ms)
    }

    fn reading(&mut self, id: &str, confirmed: bool, now_ms: i64) -> io::Result<()> {
        let written = if confirmed {
            self.confirmed.contains(id)
        } else {
            self.read.contains(id)
        };
        if id.is_empty() || written {
            return Ok(());
        }
        self.read.insert(id.to_owned());
        if confirmed {
            self.confirmed.insert(id.to_owned());
        }
        let line = if confirmed {
            json!({"type": "steer.read", "id": id, "confirmed": true})
        } else {
            json!({"type": "steer.read", "id": id})
        };
        self.out_json(&line, now_ms)?;
        self.sync()
    }

    /// Whether the reading of `id` is written, confirmed or assumed.
    pub fn was_read(&self, id: &str) -> bool {
        self.read.contains(id)
    }

    /// Whether the reading of `id` is written confirmed.
    pub fn was_confirmed(&self, id: &str) -> bool {
        self.confirmed.contains(id)
    }

    pub fn turn_completed(&mut self, now_ms: i64) -> io::Result<()> {
        self.out_json(&json!({"type": "turn.completed"}), now_ms)
    }

    pub fn turn_failed(&mut self, why: &str, now_ms: i64) -> io::Result<()> {
        self.out_json(&json!({"type": "turn.failed", "error": why}), now_ms)
    }

    /// The attempt's end, written once and synced; any call after it does nothing, so a lane
    /// that leaves can end a live claim on its way out without a second end.
    pub fn exit(&mut self, code: Option<i32>, signal: Option<i32>, now_ms: i64) -> io::Result<()> {
        if self.ended {
            return Ok(());
        }
        self.record(Entry::Shim(ShimEvent::Exited { code, signal }), now_ms)?;
        self.ended = true;
        self.sync()
    }

    pub fn ended(&self) -> bool {
        self.ended
    }

    pub fn sync(&mut self) -> io::Result<()> {
        self.writer.sync()
    }

    fn record(&mut self, entry: Entry, now_ms: i64) -> io::Result<()> {
        self.writer.append(&Record { t: now_ms, entry })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::ClaimSpool;
    use clarp_spool::{Entry, ShimEvent, SpoolReader};

    #[test]
    fn a_claim_is_made_once_written_as_called_and_ended_once() {
        let dir = std::env::temp_dir().join(format!("clarp-lane-spool-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("turn.jsonl");
        let mut spool = ClaimSpool::create(&path, 0).unwrap();
        assert!(
            ClaimSpool::create(&path, 0).is_err(),
            "an attempt is taken once"
        );
        spool.session("s", 1).unwrap();
        spool.spawned(7, 1).unwrap();
        spool.steer_open(1).unwrap();
        spool.out("line", 2).unwrap();
        spool.err("oops", 3).unwrap();
        spool.progress(10_000).unwrap();
        spool.progress(32_001).unwrap();
        spool.steer_read("w", 33_000).unwrap();
        spool.steer_read("w", 33_001).unwrap();
        assert!(spool.was_read("w") && !spool.was_read("v"));
        // An assumed reading is written once, and a confirmed one after it once more (issue
        // 933); nothing is written after a confirmed one.
        spool.steer_assumed("v", 33_002).unwrap();
        spool.steer_assumed("v", 33_003).unwrap();
        assert!(spool.was_read("v") && !spool.was_confirmed("v"));
        spool.steer_read("v", 33_004).unwrap();
        spool.steer_read("v", 33_005).unwrap();
        spool.steer_assumed("w", 33_006).unwrap();
        assert!(spool.was_confirmed("v"));
        spool.turn_completed(34_000).unwrap();
        spool.exit(Some(0), None, 34_000).unwrap();
        spool.exit(None, Some(9), 35_000).unwrap();
        assert!(spool.ended());
        let said: Vec<Entry> = SpoolReader::new(path)
            .read_from(0)
            .unwrap()
            .records
            .into_iter()
            .map(|record| record.entry)
            .collect();
        let out = |text: &str| Entry::Out(text.into());
        assert_eq!(
            said,
            [
                Entry::Shim(ShimEvent::Session { id: "s".into() }),
                Entry::Shim(ShimEvent::Spawned { pid: 7 }),
                out(r#"{"type":"steer.open"}"#),
                out("line"),
                Entry::Err("oops".into()),
                out(r#"{"type":"turn.progress"}"#),
                out(
                    &serde_json::json!({"type": "steer.read", "id": "w", "confirmed": true})
                        .to_string()
                ),
                out(&serde_json::json!({"type": "steer.read", "id": "v"}).to_string()),
                out(
                    &serde_json::json!({"type": "steer.read", "id": "v", "confirmed": true})
                        .to_string()
                ),
                out(r#"{"type":"turn.completed"}"#),
                Entry::Shim(ShimEvent::Exited {
                    code: Some(0),
                    signal: None
                }),
            ]
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
