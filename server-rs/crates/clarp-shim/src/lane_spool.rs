//! A claim's spool, as every lane admits it: made, and made durable, before anything of the
//! attempt can reach the tool (decision D16), so an attempt whose spool is not there is known to
//! have reached nothing, and one whose spool is there is followed, never started again.
//!
//! What the tool's turn prints is the driver's to write: Codex's lines go through
//! `clarp_shim::codex_turn::TurnSpool`, which takes the writer once the turn is bound
//! ([`ClaimSpool::into_writer`]). A driver that copies its tool's own lines (OpenCode's events,
//! Claude's stream-json, Muse's MSP translated) writes `Session`, `Spawned`, its `Out` lines and
//! one `Exited` the same way. A claim that never bound a turn of the tool ends here, once.

use std::io;

use clarp_spool::{Entry, Record, ShimEvent, SpoolWriter};

use clarp_shim::lane::Claim;

pub(super) struct ClaimSpool {
    writer: SpoolWriter,
}

impl ClaimSpool {
    /// Makes the claim's spool, synced. Fails when it is already there: the attempt is someone
    /// else's.
    pub(super) fn admit(claim: &Claim) -> io::Result<Self> {
        let mut writer = SpoolWriter::create(&claim.spool)?;
        writer.sync()?;
        Ok(Self { writer })
    }

    /// The writer, for the driver's lines of the bound turn.
    pub(super) fn into_writer(self) -> SpoolWriter {
        self.writer
    }

    /// The turn the claim runs is bound: its conversation and the process that runs it, said
    /// before any line of it, for a driver that copies its tool's own lines into the spool
    /// (OpenCode's events, Claude's stream-json, Muse's MSP translated). Codex's turn spool says
    /// these itself.
    #[allow(
        dead_code,
        reason = "the spool writer of drivers that copy their tool's lines (issue 761); held by its tests"
    )]
    pub(super) fn bound(&mut self, session: &str, pid: u32, now_ms: i64) -> io::Result<()> {
        for entry in [
            Entry::Shim(ShimEvent::Session {
                id: session.to_owned(),
            }),
            Entry::Shim(ShimEvent::Spawned { pid }),
        ] {
            self.writer.append(&Record { t: now_ms, entry })?;
        }
        self.writer.sync()
    }

    /// One line of the tool's own, before the end (a failure in its words).
    pub(super) fn out(&mut self, line: String, now_ms: i64) -> io::Result<()> {
        self.writer.append(&Record {
            t: now_ms,
            entry: Entry::Out(line),
        })
    }

    /// Ends a claim that bound no turn of the tool: how it ended, synced.
    pub(super) fn end(
        mut self,
        code: Option<i32>,
        signal: Option<i32>,
        now_ms: i64,
    ) -> io::Result<()> {
        self.writer.append(&Record {
            t: now_ms,
            entry: Entry::Shim(ShimEvent::Exited { code, signal }),
        })?;
        self.writer.sync()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::ClaimSpool;
    use clarp_shim::lane::Claim;
    use clarp_spool::{Entry, ShimEvent};

    #[test]
    fn a_claim_is_admitted_once_and_ends_once() {
        let dir = std::env::temp_dir().join(format!("lane-spool-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let claim = Claim {
            turn: 1,
            attempt: 1,
            spool: dir.join("t.spool"),
            socket: dir.join("t.sock"),
        };
        let mut spool = ClaimSpool::admit(&claim).unwrap();
        // The spool is there before anything reaches the tool, and a second admission fails.
        assert!(claim.spool.exists());
        assert!(ClaimSpool::admit(&claim).is_err());
        spool.bound("s1", 42, 4).unwrap();
        spool.out("{\"type\":\"turn.failed\"}".into(), 5).unwrap();
        spool.end(Some(1), None, 6).unwrap();
        let read = clarp_spool::SpoolReader::new(&claim.spool)
            .read_from(0)
            .unwrap();
        let entries: Vec<_> = read
            .records
            .into_iter()
            .map(|record| record.entry)
            .collect();
        assert_eq!(
            entries,
            [
                Entry::Shim(ShimEvent::Session { id: "s1".into() }),
                Entry::Shim(ShimEvent::Spawned { pid: 42 }),
                Entry::Out("{\"type\":\"turn.failed\"}".into()),
                Entry::Shim(ShimEvent::Exited {
                    code: Some(1),
                    signal: None
                }),
            ]
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
