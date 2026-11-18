//! Whether a request asks for a socket.
//!
//! Some of what an app asks of a Host is a conversation held over a WebSocket: a terminal,
//! a stream of speech. Every such route of the Python Host first asks one question of the
//! request's headers (`server/lib/ws.py`, `is_websocket_upgrade`), and refuses in its own
//! words what does not ask for a socket. The question is asked here as it is asked there.

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
