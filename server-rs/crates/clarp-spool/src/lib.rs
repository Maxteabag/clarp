//! The spool: the hand-over between the shim and the server.
//!
//! A shim owns one tool process and appends everything that process says to one file, a record
//! per line. The server reads that file from a byte offset it keeps in its own log. Nothing else
//! connects the two for output: if the server restarts, the shim keeps appending and the new
//! server continues from its offset. A reader only ever takes whole lines, so a record that was
//! half written when the reader looked, or when the machine died, is simply not there yet.
//!
//! A record is one line of JSON: `{"t": <ms since the epoch>, "s": "<source>", "d": <data>}`.
//!
//! | source | data |
//! |---|---|
//! | `out`, `err` | one line the tool printed, as a string |
//! | `hook` | `{"name": ..., "data": ...}`: what a hook of the tool reported |
//! | `shim` | a [`ShimEvent`]: the process was spawned, is being stopped, is gone |

use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// What the shim itself has to say about the process it owns.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "kebab-case")]
pub enum ShimEvent {
    /// The conversation assigned to the child, synced before the child can read the prompt.
    Session {
        id: String,
    },
    Spawned {
        pid: u32,
    },
    SpawnFailed {
        error: String,
    },
    /// Someone asked for the process to end; this is the signal it was sent.
    Stopping {
        signal: String,
    },
    /// The process is gone. A process that was killed has a signal and no code.
    Exited {
        code: Option<i32>,
        signal: Option<i32>,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "s", content = "d", rename_all = "kebab-case")]
pub enum Entry {
    Out(String),
    Err(String),
    Hook { name: String, data: Value },
    Shim(ShimEvent),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Record {
    /// Milliseconds since the epoch, on the shim's clock.
    pub t: i64,
    #[serde(flatten)]
    pub entry: Entry,
}

impl Record {
    /// Whether this record says the process is over, one way or the other.
    pub fn is_final(&self) -> bool {
        matches!(
            self.entry,
            Entry::Shim(ShimEvent::Exited { .. } | ShimEvent::SpawnFailed { .. })
        )
    }
}

/// The writing end. One process holds it: the shim.
pub struct SpoolWriter {
    file: File,
}

impl SpoolWriter {
    /// Creates the spool of a new turn, with its directory. Fails with `AlreadyExists` when the
    /// spool is there: a turn has one shim, and the shim that made the file is the one.
    pub fn create(path: &Path) -> io::Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let file = OpenOptions::new()
            .create_new(true)
            .append(true)
            .open(path)?;
        Ok(Self { file })
    }

    /// Opens the spool for appending, creating it and its directory when they are not there.
    pub fn open(path: &Path) -> io::Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Self { file })
    }

    /// Appends one record as one line, in one write.
    pub fn append(&mut self, record: &Record) -> io::Result<()> {
        let mut line = serde_json::to_vec(record).map_err(io::Error::other)?;
        line.push(b'\n');
        self.file.write_all(&line)
    }

    /// Makes what was appended survive a power loss. The shim does this for its own events.
    pub fn sync(&mut self) -> io::Result<()> {
        self.file.sync_data()
    }
}

/// A line of the spool that is not a record. It is kept, not dropped: the reader moves past it and
/// the server can say where the spool was damaged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Damaged {
    pub offset: u64,
    pub why: String,
}

/// What a read found: the records, the damaged lines, and where the next read starts.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Read1 {
    pub records: Vec<Record>,
    /// The offset after each record's line, in the order of the records: where a reader that
    /// takes the records up to one of them goes on.
    pub ends: Vec<u64>,
    pub damaged: Vec<Damaged>,
    /// The offset after the last whole line. The server stores this with what it took.
    pub next_offset: u64,
}

/// The reading end. Any number of processes may read; none of them changes the file.
pub struct SpoolReader {
    path: PathBuf,
}

impl SpoolReader {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Whether the spool is there at all: whether a shim ever took this turn.
    pub fn exists(&self) -> bool {
        self.path.exists()
    }

    /// Reads the whole lines from `offset` on. A spool that does not exist yet is an empty one.
    pub fn read_from(&self, offset: u64) -> io::Result<Read1> {
        self.read_window(offset, u64::MAX, usize::MAX)
    }

    /// Reads whole lines that start at or after `from` and before `until`, at most `max_lines`
    /// of them. A long spool is read in windows, so that no read holds more than a window of it.
    pub fn read_window(&self, from: u64, until: u64, max_lines: usize) -> io::Result<Read1> {
        let mut found = Read1 {
            next_offset: from,
            ..Read1::default()
        };
        let file = match File::open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(found),
            Err(error) => return Err(error),
        };
        let mut reader = BufReader::new(file);
        reader.seek(SeekFrom::Start(from))?;
        let mut line = Vec::new();
        let mut lines = 0usize;
        while found.next_offset < until && lines < max_lines {
            line.clear();
            let read = reader
                .by_ref()
                .take(MAX_LINE)
                .read_until(b'\n', &mut line)?;
            if read == 0 || line.last() != Some(&b'\n') {
                // The end, or a line still being written (or torn by a crash): not ours yet.
                // A line longer than MAX_LINE without an end is treated the same way.
                break;
            }
            match serde_json::from_slice::<Record>(&line[..line.len() - 1]) {
                Ok(record) => {
                    found.records.push(record);
                    found.ends.push(found.next_offset + read as u64);
                }
                Err(error) => found.damaged.push(Damaged {
                    offset: found.next_offset,
                    why: error.to_string(),
                }),
            }
            found.next_offset += read as u64;
            lines += 1;
        }
        Ok(found)
    }
}

/// No record is longer than this. A tool that prints a longer line has it split by the shim.
pub const MAX_LINE: u64 = 16 * 1024 * 1024;

/// The longest piece of tool output the shim puts in one record.
pub const MAX_OUTPUT_CHUNK: usize = 4 * 1024 * 1024;

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("clarp-spool-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        dir.join("spool").join("turn-1.jsonl")
    }

    fn out(t: i64, text: &str) -> Record {
        Record {
            t,
            entry: Entry::Out(text.to_string()),
        }
    }

    #[test]
    fn what_is_appended_is_read_back_in_order_and_the_offset_resumes() {
        let path = scratch("order");
        let reader = SpoolReader::new(&path);
        assert_eq!(
            reader.read_from(0).unwrap(),
            Read1::default(),
            "a spool that is not there yet is empty"
        );

        let mut writer = SpoolWriter::open(&path).unwrap();
        let spawned = Record {
            t: 1,
            entry: Entry::Shim(ShimEvent::Spawned { pid: 42 }),
        };
        writer.append(&spawned).unwrap();
        writer
            .append(&out(2, r#"{"type":"system","subtype":"init"}"#))
            .unwrap();
        let first = reader.read_from(0).unwrap();
        assert_eq!(
            first.records,
            vec![spawned, out(2, r#"{"type":"system","subtype":"init"}"#)]
        );

        let hook = Record {
            t: 3,
            entry: Entry::Hook {
                name: "PreToolUse".to_string(),
                data: serde_json::json!({"tool_name": "Bash"}),
            },
        };
        let gone = Record {
            t: 4,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        };
        writer.append(&hook).unwrap();
        writer
            .append(&Record {
                t: 3,
                entry: Entry::Err("a warning".to_string()),
            })
            .unwrap();
        writer.append(&gone).unwrap();
        writer.sync().unwrap();
        let second = reader.read_from(first.next_offset).unwrap();
        assert_eq!(second.records.len(), 3, "only what came after the offset");
        assert_eq!(second.records[0], hook);
        assert!(second.records[2].is_final() && !second.records[1].is_final());
        assert_eq!(
            reader.read_from(second.next_offset).unwrap().records,
            vec![],
            "and nothing twice"
        );
        assert_eq!(second.next_offset, std::fs::metadata(&path).unwrap().len());
    }

    #[test]
    fn the_wire_form_is_one_line_with_t_s_and_d() {
        let line = serde_json::to_string(&out(7, "hello")).unwrap();
        assert_eq!(line, r#"{"t":7,"s":"out","d":"hello"}"#);
        let shim = serde_json::to_string(&Record {
            t: 8,
            entry: Entry::Shim(ShimEvent::Exited {
                code: None,
                signal: Some(9),
            }),
        })
        .unwrap();
        assert_eq!(
            shim,
            r#"{"t":8,"s":"shim","d":{"event":"exited","code":null,"signal":9}}"#
        );
    }

    #[test]
    fn a_torn_last_line_is_not_read_until_it_is_whole() {
        let path = scratch("torn");
        let mut writer = SpoolWriter::open(&path).unwrap();
        writer.append(&out(1, "whole")).unwrap();
        // Half a record, as a crash or a reader arriving mid-write would see it.
        let mut raw = OpenOptions::new().append(true).open(&path).unwrap();
        raw.write_all(br#"{"t":2,"s":"out","d":"ha"#).unwrap();
        let reader = SpoolReader::new(&path);
        let found = reader.read_from(0).unwrap();
        assert_eq!(found.records, vec![out(1, "whole")]);
        // The rest of the line arrives; the same offset now yields it.
        raw.write_all(b"lf\"}\n").unwrap();
        assert_eq!(
            reader.read_from(found.next_offset).unwrap().records,
            vec![out(2, "half")]
        );
    }

    #[test]
    fn a_damaged_line_is_reported_and_passed() {
        let path = scratch("damaged");
        let mut writer = SpoolWriter::open(&path).unwrap();
        writer.append(&out(1, "before")).unwrap();
        OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"this is not a record\n")
            .unwrap();
        writer.append(&out(3, "after")).unwrap();
        let found = SpoolReader::new(&path).read_from(0).unwrap();
        assert_eq!(found.records, vec![out(1, "before"), out(3, "after")]);
        assert_eq!(found.damaged.len(), 1);
        assert_eq!(
            found.damaged[0].offset,
            serde_json::to_string(&out(1, "before")).unwrap().len() as u64 + 1
        );
    }

    #[test]
    fn a_spool_is_read_in_windows_that_join_up() {
        let path = scratch("windows");
        let mut writer = SpoolWriter::open(&path).unwrap();
        for n in 0..10 {
            writer.append(&out(n, &format!("line {n}"))).unwrap();
        }
        let reader = SpoolReader::new(&path);
        let whole = reader.read_from(0).unwrap();
        let mut joined = Vec::new();
        let mut offset = 0;
        loop {
            let window = reader.read_window(offset, u64::MAX, 3).unwrap();
            if window.records.is_empty() {
                break;
            }
            assert!(
                window.records.len() <= 3,
                "a window is no longer than asked"
            );
            offset = window.next_offset;
            joined.extend(window.records);
        }
        assert_eq!(joined, whole.records, "the windows are the spool, once");
        // Up to a known position and no further: how a restart rebuilds what it had read.
        let third = reader.read_window(0, u64::MAX, 3).unwrap().next_offset;
        let upto = reader.read_window(0, third, usize::MAX).unwrap();
        assert_eq!(upto.records, whole.records[..3].to_vec());
        assert_eq!(upto.next_offset, third);
        assert!(
            reader
                .read_window(third, third, 10)
                .unwrap()
                .records
                .is_empty()
        );
    }

    #[test]
    fn the_spool_of_a_turn_is_made_once() {
        let path = scratch("once");
        assert!(!SpoolReader::new(&path).exists());
        let mut first = SpoolWriter::create(&path).unwrap();
        first.append(&out(1, "mine")).unwrap();
        let second = SpoolWriter::create(&path);
        assert_eq!(
            second.err().map(|error| error.kind()),
            Some(io::ErrorKind::AlreadyExists),
            "a second shim for the same turn is refused"
        );
        assert!(SpoolReader::new(&path).exists());
    }
}
