//! Whether a request asks for a socket, and what an agent's tool is started with in a
//! terminal.
//!
//! Some of what an app asks of a Host is a conversation held over a WebSocket: a terminal,
//! a stream of speech. Every such route of the Python Host first asks one question of the
//! request's headers (`server/lib/ws.py`, `is_websocket_upgrade`), and refuses in its own
//! words what does not ask for a socket. The question is asked here as it is asked there.
//!
//! One of those conversations is a terminal on an agent's own tool. What the tool is
//! started with there is each backend's word in the Python Host (`terminal_argv`), and the
//! same word here.

use crate::python;

/// Whether a request asks for a socket, by its headers as they were sent, in their order:
/// it says `Upgrade: websocket`, names `upgrade` among what its `Connection` lists, and
/// has a key. A name counts in any letters, and of a name that came twice the one that
/// came last, as the Python Host's routes read a request's headers.
pub fn asked_for(headers: &[(String, String)]) -> bool {
    let said = |name: &str| {
        let mut sent = headers.iter().rev();
        let found = sent.find(|(known, _)| known.to_lowercase() == name);
        found.map_or("", |(_, value)| value.as_str())
    };
    if python::strip(said("upgrade")).to_lowercase() != "websocket" {
        return false;
    }
    let connection = said("connection").to_lowercase();
    if !connection
        .split(',')
        .any(|named| python::strip(named) == "upgrade")
    {
        return false;
    }
    !python::strip(said("sec-websocket-key")).is_empty()
}

/// What an agent's tool is started with in a terminal, after its program: taken up on the
/// conversation the agent has with it (`session`), or anew where it has none yet. A tool
/// that asks before it acts is told not to, as in the agent's own turns. `Err`: the tool
/// has no way of being spoken to in a terminal, in the Python Host's words for that.
pub fn terminal_of(tool: &str, session: &str) -> Result<Vec<String>, String> {
    let (always, taken_up_with): (&[&str], &str) = match tool {
        "claude" => (&["--dangerously-skip-permissions"], "--resume"),
        "codex" => (&[], "resume"),
        "agy" => (&["--dangerously-skip-permissions"], "--conversation"),
        "muse" => (&["--yolo"], "resume"),
        _ => return Err(format!("no interactive terminal for the {tool} backend")),
    };
    let mut started_with: Vec<String> = always.iter().map(|word| (*word).to_string()).collect();
    if !session.is_empty() {
        started_with.extend([taken_up_with.to_string(), session.to_string()]);
    }
    Ok(started_with)
}
