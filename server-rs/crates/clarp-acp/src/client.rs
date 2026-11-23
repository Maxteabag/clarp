//! The JSON-RPC client of the protocol, over an agent's standard input and output.
//!
//! What the agent says is read on a thread of its own ([`read_into`]) and arrives, with
//! whatever else may end a wait (an interrupt the client's owner sends), on one channel
//! ([`Inbound`]). A request is sent ([`Client::send_request`]) and its answer waited for
//! ([`Client::wait`]); what the agent says meanwhile goes to the caller's [`Handler`]: a
//! notification to be kept, a request of the agent's to be answered. A wait ends with the
//! answer, or with why there is none ([`Ended`]): so an interrupt can be acted on (a prompt
//! cancelled) and the same answer waited for again.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

/// The longest line read from an agent. A longer one is no message of the protocol, and the
/// agent that writes one is taken to be gone.
pub const LINE_MOST: usize = 64 * 1024 * 1024;

/// What reaches the client: a message of the agent's, the end of its output, or an interrupt
/// from the client's owner.
#[derive(Debug, Clone, PartialEq)]
pub enum Inbound {
    Message(Value),
    Closed,
    Interrupt,
}

/// The answer to a request.
#[derive(Debug, Clone, PartialEq)]
pub enum Answer {
    Result(Value),
    Error(Value),
}

/// Why a wait ended without its answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ended {
    /// The agent's output closed: it is gone.
    Closed,
    /// The client's owner interrupted the wait. The answer may still come.
    Interrupted,
    /// The agent said nothing for longer than the wait, or the deadline passed.
    Silent,
}

/// What the client's caller does with what the agent says that answers nothing it asked.
pub trait Handler {
    /// A notification (`session/update` and others).
    fn notification(&mut self, message: &Value) -> io::Result<()>;
    /// A request of the agent's: its result, or an error code and message.
    fn request(&mut self, method: &str, params: &Value)
    -> io::Result<Result<Value, (i64, String)>>;
}

pub struct Client<W: Write> {
    output: Option<W>,
    next: u64,
    inbound: Receiver<Inbound>,
}

impl<W: Write> Client<W> {
    pub fn new(output: W, inbound: Receiver<Inbound>) -> Self {
        Self {
            output: Some(output),
            next: 1,
            inbound,
        }
    }

    fn send(&mut self, message: &Value) -> io::Result<()> {
        let output = self.output.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::BrokenPipe, "the agent's input is closed")
        })?;
        let mut line = serde_json::to_vec(message)?;
        line.push(b'\n');
        output.write_all(&line)?;
        output.flush()
    }

    /// Closes the agent's input: an agent of the protocol leaves when it is read to its end.
    pub fn close(&mut self) {
        self.output = None;
    }

    pub fn notify(&mut self, method: &str, params: Value) -> io::Result<()> {
        self.send(&json!({"jsonrpc": "2.0", "method": method, "params": params}))
    }

    /// Sends a request: the id its answer will carry.
    pub fn send_request(&mut self, method: &str, params: Value) -> io::Result<u64> {
        let id = self.next;
        self.next += 1;
        self.send(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        Ok(id)
    }

    /// Waits for the answer to the request `id`. `silence` bounds the time between two things
    /// the agent says, `deadline` the whole wait. What else the agent says goes to `handler`.
    pub fn wait(
        &mut self,
        id: u64,
        silence: Duration,
        deadline: Option<Instant>,
        handler: &mut impl Handler,
    ) -> io::Result<Result<Answer, Ended>> {
        loop {
            let bound = match deadline {
                Some(deadline) => deadline
                    .saturating_duration_since(Instant::now())
                    .min(silence),
                None => silence,
            };
            match self.inbound.recv_timeout(bound) {
                Ok(Inbound::Message(message)) => {
                    if message.get("method").is_none() && message["id"] == json!(id) {
                        return Ok(Ok(match message.get("error") {
                            Some(error) if !error.is_null() => Answer::Error(error.clone()),
                            _ => Answer::Result(message["result"].clone()),
                        }));
                    }
                    self.other(&message, handler)?;
                }
                Ok(Inbound::Interrupt) => return Ok(Err(Ended::Interrupted)),
                Ok(Inbound::Closed) | Err(RecvTimeoutError::Disconnected) => {
                    return Ok(Err(Ended::Closed));
                }
                Err(RecvTimeoutError::Timeout) => return Ok(Err(Ended::Silent)),
            }
        }
    }

    /// Sends a request and waits for its answer.
    pub fn request(
        &mut self,
        method: &str,
        params: Value,
        silence: Duration,
        handler: &mut impl Handler,
    ) -> io::Result<Result<Answer, Ended>> {
        let id = self.send_request(method, params)?;
        self.wait(id, silence, None, handler)
    }

    /// A message that answers nothing this client asked: a notification, or a request.
    fn other(&mut self, message: &Value, handler: &mut impl Handler) -> io::Result<()> {
        let method = message["method"].as_str().unwrap_or_default();
        if method.is_empty() {
            return Ok(()); // an answer to nothing asked
        }
        let Some(id) = message.get("id").cloned() else {
            return handler.notification(message);
        };
        let answer = match handler.request(method, &message["params"])? {
            Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
            Err((code, said)) => {
                json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": said}})
            }
        };
        // An agent that cannot be written to has gone, which its closed output says next.
        let _ = self.send(&answer);
        Ok(())
    }
}

/// Reads the agent's output on a thread of its own: each JSON object a message, until the
/// output closes. What is not a JSON object is no message of the protocol and is passed over.
pub fn read_into(output: impl Read + Send + 'static, inbound: Sender<Inbound>) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(output);
        let mut line = Vec::new();
        loop {
            line.clear();
            let read = (&mut reader)
                .take(LINE_MOST as u64 + 1)
                .read_until(b'\n', &mut line);
            match read {
                Ok(0) | Err(_) => break,
                Ok(_) if line.len() > LINE_MOST => break,
                Ok(_) => {
                    if let Ok(message) = serde_json::from_slice::<Value>(&line)
                        && message.is_object()
                        && inbound.send(Inbound::Message(message)).is_err()
                    {
                        return;
                    }
                }
            }
        }
        let _ = inbound.send(Inbound::Closed);
    });
}
