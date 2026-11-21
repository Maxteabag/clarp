//! Stdio for the app-server that owns one native goal thread.
//!
//! The reader preserves the order of replies and notifications. A lane must
//! consume both while a request is outstanding: Codex can announce a turn
//! before answering the request that prompted it.

use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
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
}

impl AppServer {
    /// The caller configures the command's cwd, environment and stderr as
    /// it would for the thread's ordinary Codex launcher.
    pub fn spawn(command: &mut Command) -> io::Result<(Self, Receiver<io::Result<Frame>>)> {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
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
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
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
