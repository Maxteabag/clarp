//! Stdio for the app-server that owns one native goal thread.
//!
//! The reader preserves the order of replies and notifications. A lane must
//! consume both while a request is outstanding: Codex can announce a turn
//! before answering the request that prompted it.

use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};

use serde_json::{Value, json};

// A resumed thread uses excludeTurns, so the app-server need not send its
// whole conversation in one reply. The bound also covers large tool items.
const MAX_FRAME: u64 = 16 * 1024 * 1024;

#[derive(Debug, PartialEq)]
pub enum Frame {
    Reply {
        id: u64,
        result: Result<Value, String>,
    },
    Notification {
        method: String,
        params: Value,
    },
    Request {
        id: Value,
    },
}

pub struct AppServer {
    child: Child,
    input: BufWriter<ChildStdin>,
    next_id: u64,
    /// Codex was ended and reaped: its pid may belong to another process now.
    ended: bool,
}

impl AppServer {
    /// The caller configures the command's cwd, environment and stderr as
    /// it would for the thread's ordinary Codex launcher.
    pub fn spawn(command: &mut Command) -> io::Result<(Self, Receiver<io::Result<Frame>>)> {
        // A group of its own: what Codex started for a turn can be ended with it when it does
        // not answer a Stop, and nothing of the supervisor's goes with it.
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .process_group(0)
            .spawn()?;
        let (Some(input), Some(output)) = (child.stdin.take(), child.stdout.take()) else {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::other("Codex app-server has no protocol pipes"));
        };
        let (send, frames) = mpsc::sync_channel(64);
        let server = Self {
            child,
            input: BufWriter::new(input),
            next_id: 1,
            ended: false,
        };
        std::thread::Builder::new()
            .name("codex-app-server-reader".to_owned())
            .spawn(move || {
                let mut reader = BufReader::new(output);
                loop {
                    match read(&mut reader) {
                        Ok(Some(frame)) => {
                            if send.send(Ok(frame)).is_err() {
                                break;
                            }
                        }
                        Ok(None) => break,
                        Err(error) => {
                            let _ = send.send(Err(error));
                            break;
                        }
                    }
                }
            })?;
        Ok((server, frames))
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// End Codex, every process of its group, and every group below it, where they stand.
    pub fn kill_group(&mut self) {
        self.end();
    }

    /// Whether Codex was ended and reaped.
    pub fn is_ended(&self) -> bool {
        self.ended
    }

    /// Codex runs each command in a process group of its own, and a terminal command in a
    /// session of its own (seen with 0.167.1, 23 Nov 2026). A signal to Codex's group reaches
    /// neither, and Codex's death leaves them running. So every process below Codex is read
    /// first, with its start time. Each one that is still that same process is then ended by
    /// pid, then each group one of them leads, and Codex's own group last (the lead's ruling of
    /// 23 Nov 2026: by pid and start time, before the group; no cgroups). A process that left
    /// on purpose (a daemon that forked away from its parent) is not below Codex and is not
    /// touched.
    fn end(&mut self) {
        if std::mem::replace(&mut self.ended, true) {
            return;
        }
        let below = processes_below(self.child.id());
        let kill = nix::sys::signal::Signal::SIGKILL;
        let own = nix::unistd::getpgrp().as_raw();
        let still: Vec<&Below> = below.iter().filter(|process| process.is_still()).collect();
        for process in &still {
            let _ = nix::sys::signal::kill(nix::unistd::Pid::from_raw(process.pid), kill);
        }
        for process in &still {
            if process.group == process.pid && process.group > 1 && process.group != own {
                let _ = nix::sys::signal::killpg(nix::unistd::Pid::from_raw(process.group), kill);
            }
        }
        if let Ok(pid) = i32::try_from(self.child.id()) {
            let _ = nix::sys::signal::killpg(nix::unistd::Pid::from_raw(pid), kill);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    pub fn initialize(&mut self) -> io::Result<u64> {
        self.request(
            "initialize",
            json!({
                "clientInfo": {"name": "clarp", "title": "Clarp", "version": "1"},
                "capabilities": {"experimentalApi": true},
            }),
        )
    }

    pub fn initialized(&mut self) -> io::Result<()> {
        self.send(&json!({"method": "initialized", "params": {}}))
    }

    pub fn next_request(&self) -> u64 {
        self.next_id
    }

    pub fn request(&mut self, method: &str, params: Value) -> io::Result<u64> {
        let id = self.next_id;
        self.next_id += 1;
        self.send(&json!({"id": id, "method": method, "params": params}))?;
        Ok(id)
    }

    /// The lane starts Codex under a noninteractive approval policy.
    pub fn refuse_server_request(&mut self, id: Value) -> io::Result<()> {
        self.send(&json!({
            "id": id,
            "error": {"code": -32601, "message": "Interactive requests are not supported"},
        }))
    }

    fn send(&mut self, message: &Value) -> io::Result<()> {
        serde_json::to_writer(&mut self.input, message).map_err(io::Error::other)?;
        self.input.write_all(b"\n")?;
        self.input.flush()
    }
}

impl Drop for AppServer {
    /// A lane that leaves ends what its Codex still runs: a turn of exec leaves nothing of
    /// its commands running when its process is gone either.
    fn drop(&mut self) {
        self.end();
    }
}

/// A process below Codex, as `/proc` said when it was read.
struct Below {
    pid: i32,
    group: i32,
    start: u64,
}

impl Below {
    /// The pid still names the process that was read, not one that took its number since.
    fn is_still(&self) -> bool {
        u32::try_from(self.pid)
            .ok()
            .and_then(stat_of)
            .is_some_and(|(_, _, start)| start == self.start)
    }
}

/// A process's parent, group and start time, from its `stat`. The name is in brackets and may
/// hold anything: the fields after the last bracket are read (state, parent, group, ...; the
/// start time is the twentieth of them).
fn stat_of(pid: u32) -> Option<(u32, i32, u64)> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let after: Vec<&str> = stat[stat.rfind(')')? + 1..].split_whitespace().collect();
    Some((
        after.get(1)?.parse().ok()?,
        after.get(2)?.parse().ok()?,
        after.get(19)?.parse().ok()?,
    ))
}

/// Every process below `root`, read from `/proc`. Empty where there is none.
fn processes_below(root: u32) -> Vec<Below> {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return Vec::new();
    };
    let mut known = Vec::new();
    for entry in entries.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        if let Some((ppid, group, start)) = stat_of(pid) {
            known.push((pid, ppid, group, start));
        }
    }
    let mut found = Vec::new();
    let mut parents = vec![root];
    let mut seen = std::collections::HashSet::from([root]);
    while let Some(parent) = parents.pop() {
        for &(pid, ppid, group, start) in &known {
            if ppid == parent && seen.insert(pid) {
                if let Ok(id) = i32::try_from(pid) {
                    found.push(Below {
                        pid: id,
                        group,
                        start,
                    });
                }
                parents.push(pid);
            }
        }
    }
    found
}

fn read(reader: &mut impl BufRead) -> io::Result<Option<Frame>> {
    let mut line = Vec::new();
    let n = reader.take(MAX_FRAME).read_until(b'\n', &mut line)?;
    if n == 0 {
        return Ok(None);
    }
    if line.last() != Some(&b'\n') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Codex app-server sent an incomplete or oversized frame",
        ));
    }
    let mut value: Value = serde_json::from_slice(&line).map_err(io::Error::other)?;
    if let Some(method) = value["method"].as_str() {
        return Ok(Some(if value.get("id").is_some() {
            Frame::Request {
                id: value["id"].take(),
            }
        } else {
            Frame::Notification {
                method: method.to_owned(),
                params: value["params"].take(),
            }
        }));
    }
    let id = value["id"].as_u64().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidData, "Codex reply has no request id")
    })?;
    let result = if let Some(error) = value.get("error") {
        Err(error["message"]
            .as_str()
            .unwrap_or("Codex app-server refused the request")
            .to_owned())
    } else {
        Ok(value
            .get_mut("result")
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "Codex reply has no result"))?
            .take())
    };
    Ok(Some(Frame::Reply { id, result }))
}
