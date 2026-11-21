//! The control protocol between the server and a shim.
//!
//! The server talks to a shim over the shim's Unix socket, one JSON object per line. Output never
//! travels this way: output is the spool. The socket is for the few things the server needs to
//! say to a running turn, and for the tool's hooks to report through the one writer of the spool.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The environment variable that tells a hook where its shim listens.
pub const SOCKET_ENV: &str = "CLARP_SHIM_SOCKET";

/// The exit code of a shim that found the turn's spool already there: another shim has the turn.
pub const EXIT_TURN_TAKEN: u8 = 125;

/// The exit code of a shim whose program could not be started.
pub const EXIT_NOT_STARTED: u8 = 127;

/// The longest a hold lasts, whatever was asked: past it the shim lets the tool go by itself, so a
/// Host that is gone cannot keep a turn standing (decision D26: a shim never waits for the Host).
pub const HOLD_MOST_MS: u64 = 2_000;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// Write these bytes to the tool's standard input. Success acknowledges a flushed write.
    Stdin {
        data: String,
    },
    /// Close the tool's standard input: for a tool that reads its prompt until the end.
    CloseStdin,
    /// End the tool: a polite signal to its process group, then a kill after `grace_ms`.
    Stop {
        grace_ms: u64,
    },
    /// A hook of the tool reports what it saw.
    Hook {
        name: String,
        data: Value,
    },
    /// Tell me when there is more in the spool. After the reply, this connection carries nothing
    /// but a newline whenever the spool has grown, and it closes when the shim is gone. A newline
    /// is a hint to read the spool, never the news itself.
    Follow,
    Ping,
    /// Stop the tool where it stands, every process of its group, and say how far the spool goes
    /// with everything it printed before it stood still. The shim lets it go by itself after the
    /// smaller of `ms` and [`HOLD_MOST_MS`]. A turn is held at most once in its shim's life.
    Hold {
        ms: u64,
    },
    /// End the tool only under an acknowledged, unexpired hold at this spool fence.
    ConsumeHold {
        fence: u64,
        replaced_by: u64,
    },
    /// Let a held tool go on. A tool that is not held is left as it is.
    Release,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Reply {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// The answer to a hold that holds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub held: Option<Held>,
    /// The answer to a release: whether it was this release that let the tool go.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub let_go: Option<bool>,
    /// Whether this consume request ended its held tool.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub killed: Option<bool>,
}

/// What a hold found once the tool stood still and all it had printed was in the spool.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Held {
    /// The spool's length in bytes: the offset after its last whole record, everything the tool
    /// printed before it stood still included.
    pub spool: u64,
    /// The bytes the tool printed on its standard output after its last whole line: it had begun
    /// to print a line.
    pub partial_out: u64,
    /// The same of its standard error.
    pub partial_err: u64,
    /// The reports of the tool's hooks begun and not whole: a hook stands still in the middle of
    /// one, and it had begun to act.
    pub partial_reports: u64,
    /// What was left of the hold's bound when the answer was written.
    pub left_ms: u64,
}

impl Reply {
    pub fn ok(pid: Option<u32>) -> Self {
        Self {
            ok: true,
            pid,
            error: None,
            held: None,
            let_go: None,
            killed: None,
        }
    }

    pub fn failed(error: impl Into<String>) -> Self {
        Self {
            ok: false,
            pid: None,
            error: Some(error.into()),
            held: None,
            let_go: None,
            killed: None,
        }
    }
}

/// Takes the records off the front of what a tool has printed so far, and leaves what is not
/// a whole line yet. A record is a line without its end (`\n`, or `\r\n`). A line longer than
/// `max` bytes is cut into records of `max` bytes, so one runaway line cannot hold the shim's
/// memory or its reader's. `each` is given every record in order; when it says no, nothing more
/// is taken and the answer is no.
pub fn take_lines(held: &mut Vec<u8>, max: usize, mut each: impl FnMut(String) -> bool) -> bool {
    let mut taken = 0;
    let mut went = true;
    loop {
        let rest = &held[taken..];
        let window = &rest[..rest.len().min(max)];
        let end = match window.iter().position(|byte| *byte == b'\n') {
            Some(at) => at + 1,
            None if rest.len() >= max && max > 0 => max,
            None => break,
        };
        let mut line = &rest[..end];
        if let Some(without) = line.strip_suffix(b"\n") {
            line = without.strip_suffix(b"\r").unwrap_or(without);
        }
        taken += end;
        if !each(String::from_utf8_lossy(line).into_owned()) {
            went = false;
            break;
        }
    }
    held.drain(..taken);
    went
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn text(value: &impl Serialize) -> String {
        serde_json::to_string(value).unwrap()
    }

    #[test]
    fn a_reply_is_written_as_before_and_a_hold_adds_its_own() {
        assert_eq!(text(&Reply::ok(Some(5))), r#"{"ok":true,"pid":5}"#);
        assert_eq!(text(&Reply::failed("no")), r#"{"ok":false,"error":"no"}"#);
        assert_eq!(
            text(&Request::Hold { ms: 250 }),
            r#"{"op":"hold","ms":250}"#
        );
        assert_eq!(text(&Request::Release), r#"{"op":"release"}"#);
        let held = Reply {
            held: Some(Held {
                spool: 10,
                partial_out: 1,
                partial_err: 2,
                partial_reports: 3,
                left_ms: 5,
            }),
            ..Reply::ok(Some(5))
        };
        assert_eq!(
            text(&held),
            r#"{"ok":true,"pid":5,"held":{"spool":10,"partial_out":1,"partial_err":2,"partial_reports":3,"left_ms":5}}"#
        );
        let released = Reply {
            let_go: Some(false),
            ..Reply::ok(Some(5))
        };
        assert_eq!(text(&released), r#"{"ok":true,"pid":5,"let_go":false}"#);
        // What an older shim answers is still read.
        let old: Reply = serde_json::from_str(r#"{"ok":true,"pid":5}"#).unwrap();
        assert_eq!(old, Reply::ok(Some(5)));
    }

    fn lines(held: &mut Vec<u8>, max: usize) -> Vec<String> {
        let mut out = Vec::new();
        assert!(take_lines(held, max, |line| {
            out.push(line);
            true
        }));
        out
    }

    #[test]
    fn whole_lines_are_taken_and_the_rest_is_left() {
        let mut held = b"one\ntwo\r\n\nthr".to_vec();
        assert_eq!(lines(&mut held, 100), ["one", "two", ""]);
        assert_eq!(held, b"thr");
        held.extend_from_slice(b"ee\n");
        assert_eq!(lines(&mut held, 100), ["three"]);
        assert!(held.is_empty());
        assert!(lines(&mut held, 100).is_empty());
    }

    #[test]
    fn a_line_too_long_is_cut_into_records() {
        // Ten letters and the end of the line, four to a record.
        let mut held = b"abcdefghij\nk".to_vec();
        assert_eq!(lines(&mut held, 4), ["abcd", "efgh", "ij"]);
        assert_eq!(held, b"k");
        // A line that fills a record exactly ends in the record after it.
        let mut held = b"abcd\nxy".to_vec();
        assert_eq!(lines(&mut held, 4), ["abcd", ""]);
        assert_eq!(held, b"xy");
        // And one whose end is the record's last byte is one record.
        let mut held = b"abc\nxy".to_vec();
        assert_eq!(lines(&mut held, 4), ["abc"]);
        // What is not a whole line and not a record's worth waits.
        let mut held = b"abc".to_vec();
        assert!(lines(&mut held, 4).is_empty());
        assert_eq!(held, b"abc");
    }

    #[test]
    fn what_is_not_text_is_recorded_as_far_as_it_can_be_read() {
        let mut held = vec![b'a', 0xff, b'b', b'\n'];
        assert_eq!(lines(&mut held, 100), ["a\u{fffd}b"]);
    }

    #[test]
    fn nothing_more_is_taken_once_a_record_is_refused() {
        let mut held = b"one\ntwo\nthree\n".to_vec();
        let mut seen = Vec::new();
        assert!(!take_lines(&mut held, 100, |line| {
            seen.push(line);
            false
        }));
        assert_eq!(seen, ["one"]);
        assert_eq!(held, b"two\nthree\n");
    }
}
