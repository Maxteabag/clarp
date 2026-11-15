//! A request from one agent to another that comes back.
//!
//! A message between agents is one-way: what the other agent answers in its own chat stays
//! there. A request is a message that asks for more. The agent that is asked is told how to
//! answer (`clarp-admin reply`), the answer reaches the one who asked, and the asker's goal
//! can wait for it with a deadline, so that it is woken with the result, or without one
//! when none comes.
//!
//! It is built from what is already there: a request is an ordinary message under a stable
//! id (`u-peer-req-<id>`), which says who asked whom; the wait is a goal's dependency
//! (`peer:<id>`); an answer is a message under a stable id, or the dependency's result.
//! Nothing here is stored beside that.
//!
//! The words are the Python Host's (`server/lib/peer_requests.py`): agents read them and act
//! on them.

use sha2::{Digest, Sha256};

/// What a reply can be: the result, or word that the request is still open.
pub const KINDS: [&str; 3] = ["result", "blocked", "progress"];
/// How many `blocked` and `progress` replies one request may bring: they inform, they do
/// not flood.
pub const MAX_UPDATES: usize = 5;
pub const MAX_DEADLINE_S: i64 = 30 * 86_400;
pub const DEFAULT_DEADLINE_S: i64 = 6 * 3_600;
/// How much of an answer a goal keeps as the evidence of its dependency, in characters.
pub const MAX_EVIDENCE: usize = 8_000;
/// What a goal is told of a request the Host refused to deliver.
pub const NOT_DELIVERED: &str = "was not delivered";

/// An agent as a request names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Who<'a> {
    pub persona: &'a str,
    pub session: &'a str,
}

/// Whether a text is the id of a request: `r` and twelve hex digits.
pub fn is_request_id(given: &str) -> bool {
    given.len() == 13
        && given.starts_with('r')
        && given[1..]
            .chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

/// A new request's id, from twelve hex digits.
pub fn request_id(hex: &str) -> String {
    format!("r{hex}")
}

/// The id a request's message is sent under: what says who asked whom.
pub fn request_client_id(request_id: &str) -> String {
    format!("peer-req-{request_id}")
}

/// The name of the dependency a goal waits on for a request's result.
pub fn dependency_key(request_id: &str) -> String {
    format!("peer:{request_id}")
}

/// What the agent that is asked is told: the request, and how to answer it.
pub fn request_text(request_id: &str, requester: &Who<'_>, text: &str) -> String {
    let Who { persona, session } = requester;
    format!(
        "[Request {request_id} from {persona} ({session}). Your answers in your own chat do not \
         reach {persona}. When you have the result, or are blocked (for example waiting for an \
         approval), say so with: clarp-admin reply --request {request_id} --from <your session> \
         --kind result|blocked|progress --text '...'. No acknowledgement is needed.]\n\n{text}"
    )
}

/// What the agent that asked is told of a reply.
pub fn reply_text(request_id: &str, replier: &Who<'_>, kind: &str, text: &str) -> String {
    let Who { persona, session } = replier;
    let note = if kind == "result" {
        ""
    } else {
        " The request is still open; its result will follow. No reply is needed."
    };
    format!("[Reply to request {request_id} from {persona} ({session}): {kind}.{note}]\n\n{text}")
}

/// What a goal keeps of an answer.
pub fn evidence(said: &str) -> String {
    said.chars().take(MAX_EVIDENCE).collect()
}

/// The id a reply is sent under when it goes as a message: the same reply sent again is the
/// same message. A result has one id for the request, unless another result came first
/// under it: a later, different result is a message of its own.
pub fn reply_client_id(
    request_id: &str,
    kind: &str,
    text: &str,
    another_came_first: bool,
) -> String {
    let digest: String = Sha256::digest(format!("{kind}\0{text}").as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>()
        .chars()
        .take(12)
        .collect();
    match (kind, another_came_first) {
        ("result", false) => format!("peer-res-{request_id}"),
        ("result", true) => format!("peer-res-{request_id}-{digest}"),
        _ => format!("peer-{kind}-{request_id}-{digest}"),
    }
}

/// Whether a message's id is that of an update (`blocked`, `progress`) to this request:
/// what the limit on updates counts.
pub fn is_reply_to(message_id: &str, request_id: &str) -> bool {
    message_id
        .strip_prefix("u-peer-")
        .and_then(|rest| rest.split_once('-'))
        .is_some_and(|(_, rest)| {
            rest.strip_prefix(request_id)
                .is_some_and(|tail| tail.starts_with('-'))
        })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn the_messages_of_a_request_are_told_apart_by_their_ids() {
        let id = "r0123456789ab";
        assert!(is_reply_to(
            "u-peer-progress-r0123456789ab-0a1b2c3d4e5f",
            id
        ));
        assert!(is_reply_to("u-peer-res-r0123456789ab-0a1b2c3d4e5f", id));
        assert!(
            !is_reply_to("u-peer-res-r0123456789ab", id),
            "the first result has no tail"
        );
        assert!(!is_reply_to("u-peer-req-r0123456789ab", id));
        assert!(!is_reply_to(
            "u-peer-progress-rffffffffffff-0a1b2c3d4e5f",
            id
        ));
        assert!(!is_reply_to("u-something-else", id));
    }
}
