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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// Write these bytes to the tool's standard input.
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
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Reply {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Reply {
    pub fn ok(pid: Option<u32>) -> Self {
        Self {
            ok: true,
            pid,
            error: None,
        }
    }

    pub fn failed(error: impl Into<String>) -> Self {
        Self {
            ok: false,
            pid: None,
            error: Some(error.into()),
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
mod tests {
    use super::*;

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
