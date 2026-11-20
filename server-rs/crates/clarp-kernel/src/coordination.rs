//! Coordination between agents: which turns of a chat answer another agent, and the note an
//! agent leaves for the user.
//!
//! When one agent sends another a message, the turn it starts in the recipient's chat is
//! coordination between agents, and every row of that turn says so (`audience`). The Host
//! decides it from what it can check, never from what a sender says: a message is
//! coordination when another agent sent it (the sender resolves on this Host and is not the
//! recipient), or when it arrived as another agent's with no sender this Host knows (an agent
//! of a peer Host). The user's own word (origin `user` or `oracle`) never is, whatever sender
//! it names: a sender named in a request is a claim. So the rule takes the origin the send
//! CLAIMED and the principal that made it ([`principal`], [`origin`]), before the Host maps
//! the origin to its own kinds.
//!
//! An agent shows something to the user from such a turn only by an explicit act: a note
//! (`POST /agent-notes`, [`asked`] and what follows it), a decision card or an artifact. A
//! note is always the user's ([`row_audience`]) and notifies on its own ([`note_decision`]).
//!
//! These are the Python Host's rules (its PR 482, contract 108: `lib/coordination.py`,
//! `lib/send_request.py`, `lib/agent_notes.py`, `policies/notifications.note_decision` and
//! `user_notifications.classify_note`), held to it by `tests/coordination_vectors.rs` and
//! `tests/agent_note_vectors.rs`. Two things differ on purpose. A turn's audience never
//! loosens here ('mixed' on the Python Host): this Host steers no message into a running turn,
//! so nothing the user says joins a turn that answers another agent. And an agent on Codex can
//! leave a note here ([`can_leave_note`]): every turn of this Host carries a token, Codex's too.

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::notify::{Capabilities, Decision};
use crate::python;

/// A row of a turn that answers another agent: shown only when the user chooses to see it.
pub const AGENTS: &str = "agents";
/// Every other row.
pub const USER: &str = "user";
/// The kind of a note's row.
pub const NOTE_KIND: &str = "note";
/// A note's origin: a word no app draws as anything but an ordinary reply.
pub const NOTE_ORIGIN: &str = "note";
/// The origins by which the user's own word reaches an agent: the user's send from an app,
/// and the Oracle carrying what they said (`origins.USER_WORD_ORIGINS`).
pub const USER_WORD_ORIGINS: [&str; 2] = ["oracle", "user"];
/// The origins a client may name on a send (`origins.CLIENT_SETTABLE_ORIGINS`): what the Host
/// stamps itself (a janitor, a leader's tick) nobody may claim.
pub const CLIENT_SETTABLE_ORIGINS: [&str; 8] = [
    "user",
    "oracle",
    "agent",
    "schedule",
    "automation",
    "watcher",
    "heartbeat",
    "dreaming",
];
/// The backends whose turns let their agent leave a note here: the Python Host's
/// (`turn_identity.IDENTITY_BACKENDS`) and Codex, whose turns carry this Host's token too.
const NOTE_BACKENDS: [&str; 6] = ["claude", "agy", "grok", "opencode", "deepseek", "codex"];
/// The backends a record can name, and the one any other word means
/// (`backend_catalogue.normalize`).
const BACKENDS: [&str; 7] = [
    "claude", "codex", "agy", "grok", "opencode", "deepseek", "muse",
];
const DEFAULT_BACKEND: &str = "claude";

/// Whether a message to `agent_id` is another agent addressing it.
///
/// The user's own word never is, whatever sender it names. Otherwise a resolved sender that
/// is another agent is coordination whatever the origin, and so is origin `agent` with no
/// sender this Host knows. An agent addressing itself is not.
pub fn is_coordination(
    agent_id: &str,
    origin: Option<&str>,
    sender_agent_id: Option<&str>,
) -> bool {
    let origin = python::strip(origin.unwrap_or(""));
    if USER_WORD_ORIGINS.contains(&origin) {
        return false;
    }
    let sender = python::strip(sender_agent_id.unwrap_or(""));
    if !sender.is_empty() {
        return sender != agent_id;
    }
    origin == "agent"
}

/// A row's audience from its turn's (`None`: the turn is not marked). A note is always the
/// user's.
pub fn row_audience(kind: Option<&str>, turn_audience: Option<&str>) -> &'static str {
    if kind.unwrap_or("") == NOTE_KIND {
        return USER;
    }
    if turn_audience == Some(AGENTS) {
        AGENTS
    } else {
        USER
    }
}

/// The backend a record's word names: its aliases, and Claude for any word no backend has
/// (`backend_catalogue.normalize`).
pub fn backend(raw: Option<&str>) -> &'static str {
    let word = python::strip(raw.unwrap_or("")).to_lowercase();
    let word = match word.as_str() {
        "antigravity" => "agy",
        "open-code" | "opencode-ai" => "opencode",
        "deep-seek" => "deepseek",
        "muse-code" | "musecode" => "muse",
        other => other,
    };
    BACKENDS
        .into_iter()
        .find(|known| *known == word)
        .unwrap_or(DEFAULT_BACKEND)
}

/// Whether the agent can leave a note for the user: its backend's turns carry the Host's
/// token, and the Host can read its key (without it no turn has a token). The one place this
/// is decided. An agent that cannot is never hidden: the failure of this feature is shown,
/// never lost. Its turns are still marked, so they hide on the day it can.
pub fn can_leave_note(agent_id: &str, backend_word: Option<&str>, key_readable: bool) -> bool {
    !agent_id.is_empty() && key_readable && NOTE_BACKENDS.contains(&backend(backend_word))
}

/// A refusal: the status and the words a client is told.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub status: u16,
    pub words: String,
}

fn refused(status: u16, words: impl Into<String>) -> Refusal {
    Refusal {
        status,
        words: words.into(),
    }
}

/// Who made a send, as far as it decides the send's origin and sender.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Principal {
    /// The send's sender: the one it named (resolved on this Host), else the agent whose turn
    /// made it; `""` for none.
    pub sender_agent_id: String,
    /// It was made from another agent's own turn (its token): never the user's word.
    pub own_turn: bool,
    /// It was made from the target's own turn: an agent waking itself.
    pub self_prompt: bool,
}

/// The words of a bookkeeping delegate's send to its principal, refused (403).
pub const DELEGATE_REFUSED: &str = "a bookkeeping delegate does not message its principal";

/// Who made a send. `sender_agent_id` is the sender the send named, resolved on this Host
/// (`""` for none or one this Host does not know); `caller_agent_id` the agent of the turn
/// token it presented (`""` for none); `caller_session` that agent's session when this Host
/// still has it. A token of a turn of an agent that is not the sender it named is refused:
/// a fork or a copied conversation speaking as its source. Whether the sender then keeps the
/// books of the target ([`DELEGATE_REFUSED`]) is the Host's to ask next.
pub fn principal(
    target_agent_id: &str,
    sender_agent_id: &str,
    caller_agent_id: &str,
    caller_session: Option<&str>,
) -> Result<Principal, Refusal> {
    if !caller_agent_id.is_empty()
        && !sender_agent_id.is_empty()
        && caller_agent_id != sender_agent_id
        && let Some(own) = caller_session
    {
        let own = if own.is_empty() { caller_agent_id } else { own };
        return Err(refused(
            403,
            format!("this turn belongs to {own}, not to the sender it named; send as --from {own}"),
        ));
    }
    let mut made = Principal {
        sender_agent_id: sender_agent_id.to_string(),
        ..Principal::default()
    };
    if !caller_agent_id.is_empty() {
        made.self_prompt = caller_agent_id == target_agent_id;
        if !made.self_prompt {
            made.own_turn = true;
            if made.sender_agent_id.is_empty() && caller_session.is_some() {
                made.sender_agent_id = caller_agent_id.to_string();
            }
        }
    }
    Ok(made)
}

/// A send's origin from the one it claims and who made it. A word no client may name is the
/// user's, or an agent's when there is a sender. Neither an agent's own turn nor a peer Host
/// speaks the user's word: its message is an agent's. An agent waking itself from its own
/// turn with the user's word is automation (and not coordination).
pub fn origin(claimed: Option<&str>, principal: &Principal, peer: bool) -> String {
    let claimed = python::strip(claimed.unwrap_or("")).to_lowercase();
    let origin = if CLIENT_SETTABLE_ORIGINS.contains(&claimed.as_str()) {
        claimed
    } else if principal.sender_agent_id.is_empty() {
        "user".to_string()
    } else {
        "agent".to_string()
    };
    let user_word = USER_WORD_ORIGINS.contains(&origin.as_str());
    if user_word && (principal.own_turn || peer) {
        "agent".to_string()
    } else if user_word && principal.self_prompt {
        "automation".to_string()
    } else {
        origin
    }
}

/// Whether a send's answer is spoken: as it asks (`synthesize_audio`), where the user sent it
/// only unless it asks for silence, and otherwise only when it asks for speech.
pub fn synthesize_audio(origin: &str, asked: Option<&Value>) -> bool {
    match asked {
        None => origin == "user",
        Some(asked) if origin == "user" => *asked != Value::Bool(false),
        Some(asked) => *asked == Value::Bool(true),
    }
}

/// The command an agent leaves a note with, named in the words of a refusal.
pub const COMMAND: &str = "clarp-admin tell-user";
/// The most signs a note has.
pub const MAX_TEXT: usize = 8000;
/// The most signs a note's own id has.
const MAX_NOTE_ID: usize = 64;
/// The reason a note asked again is told: the notification of its first asking stands.
pub const AGAIN: &str = "duplicate-completion";

/// What a note asks for, as the request's body says it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Asked {
    pub session: String,
    pub text: String,
    /// The note's own id, lower-cased; `None` when the request named none: the Host mints one.
    pub note_id: Option<String>,
}

/// A value as Python's `str(value or "")` reads it.
fn said(value: Option<&Value>) -> String {
    match value {
        Some(value) if python::truthy(value) => match value {
            Value::String(text) => text.clone(),
            other => python::repr(other),
        },
        _ => String::new(),
    }
}

/// What a note asks for, or why the body is refused, in the order the Python Host checks: not
/// an object, no session, no words, too many words, an id of other signs.
pub fn asked(body: &Value) -> Result<Asked, Refusal> {
    let Value::Object(fields) = body else {
        return Err(refused(400, "bad json"));
    };
    let session = python::strip(&said(fields.get("session"))).to_string();
    let text = python::strip(&said(fields.get("text"))).to_string();
    if session.is_empty() {
        return Err(refused(400, "session required"));
    }
    if text.is_empty() {
        return Err(refused(
            400,
            format!("a note needs words: {COMMAND} --from {session} --text '...'"),
        ));
    }
    if text.chars().count() > MAX_TEXT {
        return Err(refused(
            413,
            format!("a note is at most {MAX_TEXT} characters"),
        ));
    }
    let note_id = python::strip(&said(fields.get("note_id"))).to_lowercase();
    let note_id = if note_id.is_empty() {
        None
    } else if note_id.chars().count() <= MAX_NOTE_ID
        && note_id
            .chars()
            .all(|sign| matches!(sign, 'a'..='z' | '0'..='9' | '-' | '_'))
    {
        Some(note_id)
    } else {
        return Err(refused(400, "note_id: up to 64 of a-z, 0-9, '-' and '_'"));
    };
    Ok(Asked {
        session,
        text,
        note_id,
    })
}

/// The words of a note asked for an agent this Host does not have (404).
pub fn no_such_agent() -> Refusal {
    refused(404, "no such agent")
}

/// The turn a note's request presents, as the Host reads its token.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Caller<'a> {
    /// No token of a turn of this Host: none, or a text that is none.
    Nobody,
    /// A turn of the agent the note is for.
    Own,
    /// A turn of another agent: its session, or `None` when this Host no longer has it.
    Other(Option<&'a str>),
}

/// Whether the request may leave a note for `session`: only that agent's own turn may speak
/// for it, not a device, a peer Host or the Host's own token, which every agent holds.
pub fn may_note(session: &str, caller: Caller<'_>) -> Result<(), Refusal> {
    match caller {
        Caller::Own => Ok(()),
        Caller::Nobody => Err(refused(
            403,
            format!(
                "{COMMAND} works only from a turn of {session}: this call carries no turn of an agent"
            ),
        )),
        Caller::Other(Some(own)) => Err(refused(
            403,
            format!("this turn belongs to {own}, not to {session}; run {COMMAND} --from {own}"),
        )),
        Caller::Other(None) => Err(refused(
            403,
            format!(
                "this turn belongs to an agent that no longer exists, not to {session}; \
                 {COMMAND} works only from its own turn"
            ),
        )),
    }
}

/// The words of a note from a turn that does not run (409): one that ended or settled, an
/// older turn's, or one parked waiting for an account.
pub fn not_running(session: &str) -> Refusal {
    refused(
        409,
        format!("{COMMAND} works only inside a running turn of {session}; this call is not in one"),
    )
}

/// A note asked again under an id its agent already has: `Ok(true)` when it holds the same
/// words (the first one stands, nobody is told again), `Ok(false)` for an id it does not have,
/// refused when the words differ.
pub fn again(
    session: &str,
    note_id: &str,
    held: Option<&str>,
    text: &str,
) -> Result<bool, Refusal> {
    match held {
        None => Ok(false),
        Some(held) if held == text => Ok(true),
        Some(_) => Err(refused(
            409,
            format!(
                "note_id {note_id} is already a note of {session} with other words; \
                 send the new words with a new note_id"
            ),
        )),
    }
}

/// The words of a note of an agent that has no conversation yet (409).
pub fn no_conversation(session: &str) -> Refusal {
    refused(
        409,
        format!("{session} has no conversation to write a note into yet"),
    )
}

/// A note's row id: of its agent and its own id, so a note asked again is the same row.
pub fn note_message_id(agent_id: &str, note_id: &str) -> String {
    format!("note-{agent_id}-{note_id}")
}

/// A note's notification id: of its row, never of the millisecond, so a turn's completion in
/// the same millisecond keeps the id that millisecond gives it.
pub fn note_notification_id(message_id: &str) -> String {
    let mark = Sha256::digest(format!("note\0{message_id}").as_bytes());
    let mark: String = mark.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("pn-{}", &mark[..20])
}

/// The millisecond a notification is filed under: `at`, or the first one after it that no
/// notification of the agent has (one notification per agent and millisecond).
pub fn free_ms(at: i64, taken: impl Fn(i64) -> bool) -> i64 {
    let mut at = at;
    while taken(at) {
        at += 1;
    }
    at
}

/// Whether an agent's note pages the user. The note is the agent's explicit act addressed to
/// the user, so the turn it was written in (another agent's, often) does not silence it. A
/// quiet agent stays quiet; mute and the helper rule take only the push, as for a reply.
pub fn note_decision(agent: &Capabilities) -> Decision {
    if agent.quiet || (agent.present && !agent.can_chat) {
        return Decision::Suppress {
            reason: "janitor-maintenance".to_string(),
        };
    }
    let reason = if agent.muted {
        format!("{NOTE_REASON}-muted")
    } else {
        NOTE_REASON.to_string()
    };
    Decision::Notify {
        reason,
        kind: NOTE_REASON.to_string(),
        push: !agent.muted,
        muted: agent.muted,
    }
}

/// The reason a note pages the user under.
pub const NOTE_REASON: &str = "agent-note";

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_notification_is_filed_at_the_first_millisecond_no_other_has() {
        assert_eq!(free_ms(10, |_| false), 10);
        assert_eq!(free_ms(10, |at| at == 10), 11);
        assert_eq!(free_ms(10, |at| (10..13).contains(&at)), 13);
        // One taken before or after it moves nothing.
        assert_eq!(free_ms(10, |at| at == 9 || at == 11), 10);
    }
}
