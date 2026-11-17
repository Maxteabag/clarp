//! What a routing model is shown: the packet.
//!
//! The message, the agent it was said to and the one that is open, every agent a person
//! can speak to with what it was last told and said, the messages that are held while
//! their user is asked whom they were for, and the agents whose names sound like a word
//! of the message. An agent's last words are shown for the one the message was said to
//! and for the one that is open; for all of them only when the router asks again with
//! everyone in view (`scope_all`).
//!
//! The rule is the Python Host's (`server/lib/orchestrator_context.py`,
//! `build_context_packet`), held by every packet of
//! `tests/fixtures/router-flow-vectors.json`.

use serde_json::{Value, json};
use sha1::{Digest, Sha1};

use super::{CLARIFY_CONFIDENCE, HIGH_CONFIDENCE, STICKY_CONFIDENCE, Settings};
use crate::{difflib, python};

/// How many of an agent's last messages of each side are shown, how much of each, how
/// many held messages, and how far back an agent counts as recent when an app could
/// place a message by no name.
const SAID_MOST: usize = 5;
const TEXT_MOST: usize = 240;
const HELD_MOST: usize = 3;
pub const RECENT_MS: i64 = 30 * 60 * 1000;
/// How many words of a message are held against the agents' names, and how alike a word
/// and a name have to be.
const WORDS_MOST: usize = 12;
const ALIKE: f64 = 0.62;

/// What an agent was last doing.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct State {
    pub kind: String,
    pub detail: String,
    pub ts: i64,
}

/// Something an agent was told (`user`) or said (`assistant`).
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct Said {
    pub role: String,
    pub text: String,
    pub trace_id: String,
    /// Where it is known from: what the router itself gave the agent, or its conversation.
    pub source: String,
    pub created_at: i64,
}

/// An agent as the router sees it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Member {
    pub agent_id: String,
    pub session: String,
    pub persona: String,
    pub voice_id: String,
    pub cwd: String,
    pub backend: String,
    pub archived: bool,
    /// A person can speak to it: a janitor is none.
    pub voice_target: bool,
    pub created_at: i64,
    pub last_activity: i64,
    pub state: Option<State>,
    /// What it was told and said of late, in any order.
    pub said: Vec<Said>,
}

/// How a message came in, as far as the router has to know.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct Came {
    /// The id its sender gave it.
    pub client_id: String,
    /// Who sent it: `user`, or what an agent's message says of itself.
    pub origin: String,
    pub sender_agent_id: String,
    /// What the Host keeps of how it came in, to give the message later as it would have
    /// given it then. The router reads none of it.
    pub kept: String,
}

/// A message that is held while its user is asked whom it was for.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct Pending {
    pub pending_id: String,
    pub trace_id: String,
    pub utterance: String,
    pub requested_session: String,
    pub candidate_session: String,
    pub speak_as_session: String,
    pub reason: String,
    pub created_at: i64,
    pub expires_at: i64,
    /// `pending` until it is given to an agent, then `sent`.
    pub status: String,
    /// How the message came in, kept for when it is given.
    pub came: Option<Came>,
}

/// What a packet is made from.
#[derive(Debug, Clone)]
pub struct Shown<'a> {
    pub utterance: &'a str,
    pub requested_session: &'a str,
    pub trace_id: &'a str,
    pub hands_free: bool,
    pub settings: &'a Settings,
    pub scope_all: bool,
    pub fallback_request: bool,
    /// The agents of a call: nobody else is shown. `None`: everyone.
    pub only_sessions: Option<&'a [String]>,
    /// The agent that is open, by its id.
    pub focus: Option<&'a str>,
    /// Every agent of the Host, in the order they are listed.
    pub agents: &'a [Member],
    pub pending: &'a [Pending],
    pub now_ms: i64,
}

/// A text on one line, cut to so many signs with three dots where something was left out.
pub fn compact(text: &str, most: usize) -> String {
    let words: Vec<&str> = text
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .collect();
    let text = words.join(" ");
    if text.chars().count() <= most {
        return text;
    }
    let cut = python::first(&text, most.saturating_sub(3));
    format!("{}...", cut.trim_end_matches(python::blank))
}

/// An agent's last messages of one side, at most so many, the oldest first.
pub(super) fn last_said(member: &Member, role: &str, most: usize) -> Vec<Value> {
    let mut said: Vec<&Said> = member
        .said
        .iter()
        .filter(|said| said.role == role)
        .collect();
    said.sort_by_key(|said| std::cmp::Reverse(said.created_at));
    said.truncate(most.max(1));
    said.reverse();
    let shown = said.into_iter().map(|said| {
        json!({
            "role": said.role, "text": compact(&said.text, TEXT_MOST), "trace_id": said.trace_id,
            "source": said.source, "created_at": said.created_at,
        })
    });
    shown.collect()
}

/// How alike two words are, as Python's `difflib.SequenceMatcher(None, a, b).ratio()`.
fn alike(word: &str, name: &str) -> f64 {
    let signs = |text: &str| text.chars().map(String::from).collect::<Vec<String>>();
    let (word, name) = (signs(word), signs(name));
    let word: Vec<&str> = word.iter().map(String::as_str).collect();
    let name: Vec<&str> = name.iter().map(String::as_str).collect();
    difflib::ratio(&word, &name)
}

/// A number to three places as Python's `round(number, 3)` gives it: the nearest, and of
/// two that are as near the even one, judged by the number as it is kept.
fn to_three_places(number: f64) -> f64 {
    format!("{number:.3}").parse().unwrap_or(number)
}

/// The agents whose session name or name sounds like one of the message's first words:
/// each with the word and how alike they are, the most alike first.
pub fn sound_alike(utterance: &str, agents: &[Value]) -> Vec<Value> {
    let lowered = utterance.to_lowercase();
    let words = lowered
        .split(|sign: char| !sign.is_ascii_alphanumeric())
        .filter(|word| !word.is_empty());
    let words: Vec<&str> = words.take(WORDS_MOST).collect();
    let mut found: Vec<(f64, Value)> = Vec::new();
    for agent in agents {
        let text = |key: &str| agent[key].as_str().unwrap_or_default();
        let names = [
            text("session").to_lowercase(),
            text("persona").to_lowercase(),
        ];
        let (mut best, mut matched) = (0.0, "");
        for word in &words {
            for name in &names {
                let score = alike(word, name);
                if score > best {
                    (best, matched) = (score, *word);
                }
            }
        }
        if best >= ALIKE {
            let score = to_three_places(best);
            found.push((
                score,
                json!({
                    "session": agent["session"], "persona": agent["persona"],
                    "matched_token": matched, "score": score,
                }),
            ));
        }
    }
    // The most alike first; those equally alike in the order of the agents.
    found.sort_by(|one, other| other.0.total_cmp(&one.0));
    found.into_iter().map(|(_, agent)| agent).collect()
}

/// The packet for one asking of a model.
pub fn packet(shown: &Shown<'_>) -> Value {
    let focus = shown.focus.and_then(|id| {
        let mut all = shown.agents.iter();
        all.find(|agent| agent.agent_id == id)
    });
    let focus_session = focus.map_or(shown.requested_session, |focus| focus.session.as_str());
    let detailed = [shown.requested_session, focus_session];
    let recent_from = shown.now_ms - RECENT_MS;
    let listed = shown.agents.iter().filter(|agent| {
        let spoken_to = !agent.archived && agent.voice_target;
        let in_call = shown
            .only_sessions
            .is_none_or(|only| only.contains(&agent.session));
        // An app that could place the message by no name is shown the agents of the last
        // half hour, and the two the message may have been meant for anyway.
        let active = agent.created_at.max(agent.last_activity);
        let active = active.max(agent.state.as_ref().map_or(0, |state| state.ts));
        let recent = detailed.contains(&agent.session.as_str()) || active >= recent_from;
        spoken_to && in_call && (!shown.fallback_request || recent)
    });
    let mut message_count = 0;
    let mut agents: Vec<Value> = Vec::new();
    for agent in listed {
        let detail = shown.scope_all || detailed.contains(&agent.session.as_str());
        let (told, said) = match detail {
            true => (
                last_said(agent, "user", SAID_MOST),
                last_said(agent, "assistant", SAID_MOST),
            ),
            false => (Vec::new(), Vec::new()),
        };
        message_count += told.len() + said.len();
        let state = agent.state.as_ref().filter(|_| detail).map(|state| {
            json!({ "kind": state.kind, "detail": compact(&state.detail, TEXT_MOST), "ts": state.ts })
        });
        agents.push(json!({
            "session": agent.session, "persona": agent.persona, "voice_id": agent.voice_id,
            "cwd": agent.cwd, "backend": agent.backend,
            "is_requested": agent.session == shown.requested_session,
            "is_sticky_focus": focus.is_some_and(|focus| focus.agent_id == agent.agent_id),
            "state": state, "recent_user_messages": told, "recent_agent_messages": said,
        }));
    }
    // The newest of the messages that are held and whose time is not up.
    let mut held: Vec<&Pending> = shown.pending.iter().collect();
    held.retain(|held| held.status == "pending" && held.expires_at > shown.now_ms);
    held.sort_by_key(|held| std::cmp::Reverse(held.created_at));
    let held: Vec<Value> = held
        .into_iter()
        .take(HELD_MOST)
        .map(|held| {
            json!({
                "pending_id": held.pending_id, "trace_id": held.trace_id,
                "utterance": compact(&held.utterance, TEXT_MOST),
                "requested_session": held.requested_session,
                "candidate_session": held.candidate_session,
                "speak_as_session": held.speak_as_session,
                "reason": compact(&held.reason, TEXT_MOST), "created_at": held.created_at,
                "expires_at": held.expires_at,
            })
        })
        .collect();
    let scope = if shown.scope_all { "all" } else { "focused" };
    let alike = sound_alike(shown.utterance, &agents);
    let agent_count = agents.len();
    let mut whole = json!({
        "utterance": shown.utterance, "requested_session": shown.requested_session,
        "focus_session": focus_session, "context_scope": scope,
        "fallback_request": shown.fallback_request, "agents": agents, "pending": held,
        "candidate_name_matches": alike,
    });
    // The mark of what was shown: the same for the same message among the same agents.
    let mark = Sha1::digest(python::sorted_ascii(&whole).as_bytes());
    let mark: String = mark.iter().map(|byte| format!("{byte:02x}")).collect();
    let window = shown.fallback_request.then_some(30);
    let rest = json!({
        "trace_id": shown.trace_id, "hands_free": shown.hands_free,
        "settings": shown.settings.shown(),
        "context_summary": {
            "hash": &mark[..16], "scope": scope, "agent_count": agent_count,
            "message_count": message_count,
        },
        "routing_policy": {
            "wrong_agent_is_worse_than_clarification": true,
            "sticky_bias_session": focus_session, "high_confidence": HIGH_CONFIDENCE,
            "sticky_confidence": STICKY_CONFIDENCE, "clarify_confidence": CLARIFY_CONFIDENCE,
            "automatic_confidence_threshold": shown.settings.confidence_threshold,
            "recent_agent_window_minutes": window,
        },
    });
    if let (Some(whole), Value::Object(rest)) = (whole.as_object_mut(), rest) {
        whole.extend(rest);
    }
    whole
}
