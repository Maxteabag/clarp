//! The client against a fake ACP server: a thread on the other end of two pipes that answers
//! as a script says.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{self, BufRead, BufReader, PipeReader, PipeWriter, Write};
use std::sync::mpsc::{self, Sender};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use clarp_acp::client::{self, Answer, Client, Ended, Handler, Inbound};
use clarp_acp::schema::{self, SessionUpdate};
use clarp_acp::{code, method};
use serde_json::{Value, json};

/// The server's side: what it reads from the client and where it writes.
struct Server {
    from: BufReader<PipeReader>,
    to: PipeWriter,
    /// Everything the client sent, in order.
    seen: Vec<Value>,
}

impl Server {
    fn read(&mut self) -> Option<Value> {
        let mut line = String::new();
        if self.from.read_line(&mut line).ok()? == 0 {
            return None;
        }
        let message: Value = serde_json::from_str(&line).unwrap();
        self.seen.push(message.clone());
        Some(message)
    }

    fn send(&mut self, message: Value) {
        writeln!(self.to, "{message}").unwrap();
        self.to.flush().unwrap();
    }

    fn update(&mut self, update: Value) {
        self.send(json!({"jsonrpc": "2.0", "method": "session/update",
                         "params": {"sessionId": "s", "update": update}}));
    }
}

/// A client wired to a server that runs `script` on a thread; the sender interrupts waits.
fn wired(
    script: impl FnOnce(&mut Server) + Send + 'static,
) -> (Client<PipeWriter>, Sender<Inbound>, JoinHandle<Vec<Value>>) {
    let (client_in, server_out) = io::pipe().unwrap();
    let (server_in, client_out) = io::pipe().unwrap();
    let (inbound, received) = mpsc::channel();
    client::read_into(client_in, inbound.clone());
    let server = std::thread::spawn(move || {
        let mut server = Server {
            from: BufReader::new(server_in),
            to: server_out,
            seen: Vec::new(),
        };
        script(&mut server);
        // Read on until the client closes, so that nothing it sends is lost.
        while server.read().is_some() {}
        server.seen
    });
    (Client::new(client_out, received), inbound, server)
}

/// A handler that keeps the notifications and allows by policy.
#[derive(Default)]
struct Kept {
    notified: Vec<Value>,
    asked: Vec<String>,
}

impl Handler for Kept {
    fn notification(&mut self, message: &Value) -> io::Result<()> {
        self.notified.push(message.clone());
        Ok(())
    }

    fn request(&mut self, asked: &str, params: &Value) -> io::Result<Result<Value, (i64, String)>> {
        self.asked.push(asked.to_string());
        if asked != method::REQUEST_PERMISSION {
            return Ok(Err((code::METHOD_NOT_FOUND, "Method not found".into())));
        }
        let options = schema::permission_options(params);
        Ok(Ok(schema::permission_outcome(schema::chosen(&options))))
    }
}

const WAIT: Duration = Duration::from_secs(10);

#[test]
fn a_request_is_answered_and_what_comes_before_the_answer_is_handled() {
    let (mut client, _interrupt, server) = wired(|server| {
        let asked = server.read().unwrap();
        server.update(json!({"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": "Hi"}}));
        server.send(json!({"jsonrpc": "2.0", "id": "p-1", "method": "session/request_permission",
            "params": {"sessionId": "s", "toolCall": {"toolCallId": "t"},
                       "options": [{"optionId": "ok", "name": "Allow", "kind": "allow_once"}]}}));
        // The answer to the permission comes before the answer to the request.
        let answered = server.read().unwrap();
        assert_eq!(answered["id"], "p-1");
        server.send(json!({"jsonrpc": "2.0", "id": "x-1", "method": "fs/read_text_file", "params": {}}));
        server.read().unwrap();
        server.send(json!({"jsonrpc": "2.0", "id": asked["id"], "result": {"stopReason": "end_turn"}}));
    });
    let mut kept = Kept::default();
    let answer = client
        .request(method::SESSION_PROMPT, json!({"sessionId": "s"}), WAIT, &mut kept)
        .unwrap();
    assert_eq!(answer, Ok(Answer::Result(json!({"stopReason": "end_turn"}))));
    assert_eq!(kept.notified.len(), 1);
    assert!(matches!(
        SessionUpdate::of_notification(&kept.notified[0]),
        Some(SessionUpdate::AgentMessageChunk { .. })
    ));
    assert_eq!(kept.asked, ["session/request_permission", "fs/read_text_file"]);
    client.close();
    let seen = server.join().unwrap();
    assert_eq!(seen[0]["method"], "session/prompt");
    assert_eq!(seen[0]["jsonrpc"], "2.0");
    assert_eq!(seen[1]["result"]["outcome"], json!({"outcome": "selected", "optionId": "ok"}));
    assert_eq!(seen[2]["error"]["code"], code::METHOD_NOT_FOUND);
}

#[test]
fn an_error_is_an_answer_and_ids_count_up() {
    let (mut client, _interrupt, server) = wired(|server| {
        let first = server.read().unwrap();
        server.send(json!({"jsonrpc": "2.0", "id": first["id"],
                           "error": {"code": -32000, "message": "Authentication required"}}));
        let second = server.read().unwrap();
        server.send(json!({"jsonrpc": "2.0", "id": second["id"], "result": {"sessionId": "s"}}));
    });
    let mut kept = Kept::default();
    let refused = client.request(method::SESSION_NEW, json!({}), WAIT, &mut kept).unwrap();
    assert_eq!(
        refused,
        Ok(Answer::Error(json!({"code": -32000, "message": "Authentication required"})))
    );
    let opened = client.request(method::SESSION_NEW, json!({}), WAIT, &mut kept).unwrap();
    assert_eq!(opened, Ok(Answer::Result(json!({"sessionId": "s"}))));
    client.close();
    let seen = server.join().unwrap();
    assert_eq!((&seen[0]["id"], &seen[1]["id"]), (&json!(1), &json!(2)));
}

#[test]
fn an_interrupt_ends_a_wait_and_the_answer_is_waited_for_again_after_a_cancel() {
    let (mut client, interrupt, server) = wired(|server| {
        let prompt = server.read().unwrap();
        server.update(json!({"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": "working"}}));
        let cancel = server.read().unwrap();
        assert_eq!(cancel["method"], "session/cancel");
        server.send(json!({"jsonrpc": "2.0", "id": prompt["id"], "result": {"stopReason": "cancelled"}}));
    });
    struct Interrupting(Kept, Sender<Inbound>);
    impl Handler for Interrupting {
        fn notification(&mut self, message: &Value) -> io::Result<()> {
            // Once the agent is at work, the Stop arrives.
            let _ = self.1.send(Inbound::Interrupt);
            self.0.notification(message)
        }
        fn request(&mut self, asked: &str, params: &Value) -> io::Result<Result<Value, (i64, String)>> {
            self.0.request(asked, params)
        }
    }
    let mut handler = Interrupting(Kept::default(), interrupt);
    let id = client
        .send_request(method::SESSION_PROMPT, json!({"sessionId": "s"}))
        .unwrap();
    assert_eq!(client.wait(id, WAIT, None, &mut handler).unwrap(), Err(Ended::Interrupted));
    client
        .notify(method::SESSION_CANCEL, json!({"sessionId": "s"}))
        .unwrap();
    let deadline = Some(Instant::now() + WAIT);
    let answer = client.wait(id, WAIT, deadline, &mut handler).unwrap();
    assert_eq!(answer, Ok(Answer::Result(json!({"stopReason": "cancelled"}))));
    client.close();
    server.join().unwrap();
}

#[test]
fn silence_and_a_closed_agent_end_a_wait() {
    let (mut client, _interrupt, server) = wired(|server| {
        server.read().unwrap();
    });
    let mut kept = Kept::default();
    let silent = client
        .request(method::INITIALIZE, json!({}), Duration::from_millis(50), &mut kept)
        .unwrap();
    assert_eq!(silent, Err(Ended::Silent));
    // A deadline that has passed ends the wait however long the silence may be.
    let passed = client
        .wait(1, WAIT, Some(Instant::now()), &mut kept)
        .unwrap();
    assert_eq!(passed, Err(Ended::Silent));
    client.close();
    server.join().unwrap();
    // The server is gone: its output closed.
    assert_eq!(client.wait(1, WAIT, None, &mut kept).unwrap(), Err(Ended::Closed));
    // Nothing more can be sent to it.
    assert!(client.notify(method::SESSION_CANCEL, json!({})).is_err());
}
