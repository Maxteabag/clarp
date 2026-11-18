//! The tools of the fleet, as the Oracle's router may use them.
//!
//! The voice of a call holds no tools. What a router makes of the user's words is done with
//! the tools of the fleet: the agents there are, work handed to one of them or to the
//! Oracle's contact, an agent stopped (and other work put in the place of what was stopped),
//! what an agent is doing, and what it said, read without asking it. An agent is named by
//! its session or by what it is called, whatever the case of the letters, and must be the
//! only one of that name.
//!
//! Here is what each tool means and what it answers ([`plan`] says what a Host is to do, and
//! the answers are made of what the Host found); the Host does it with its fleet. The Python
//! Host's `oracle_calls_stable.py` (`AgentTools` and the readers beside it), held to it by
//! `tests/fixtures/oracle-tools-vectors.json`.

use serde_json::{Map, Value, json};

use super::said;
use super::threads::operation_id;
use crate::python;

/// How long a request to an agent may be, in signs.
pub const REQUEST_SIGNS: usize = 16_000;
/// How many messages of an agent a router is read, and how much of them: for a sentence
/// about them, and for their own words.
pub const MESSAGES_READ: usize = 10;
const MESSAGE_SIGNS: usize = 2000;
const MESSAGES_SIGNS: usize = 12_000;
pub const TRANSCRIPT_USUAL: usize = 10;
pub const TRANSCRIPT_MOST: usize = 30;
const TRANSCRIPT_MESSAGE_SIGNS: usize = 6000;
const TRANSCRIPT_SIGNS: usize = 16_000;
/// How many of an agent's newest messages are looked at for what it last said.
pub const STATUS_MESSAGES: usize = 20;
/// How much is told of an agent's personality, of what it is doing, and of what it last said.
const PERSONALITY_SIGNS: usize = 500;
const STEP_SIGNS: usize = 200;
const LAST_SAID_SIGNS: usize = 400;

pub const NOBODY: &str = "Unknown or ambiguous agent; use a session from list_agents";
pub const NO_REQUEST: &str = "Request must contain 1 to 16000 characters";
pub const NO_TOOL: &str = "Unknown Oracle tool";
const NO_CONTACT: &str = "No Oracle contact selected; ask which contact should investigate";
const ROSTER_NOTE: &str =
    "These are the agents you can reach; oracle_contact is your main colleague.";
const HANDED: &str = "Handed off, not finished.";
const STOPPED: &str = "Confirm the cancellation in one sentence.";
const STATUS_NOTE: &str =
    "Say what they are doing in one sentence; mention the age if it is more than an hour.";
const MESSAGES_NOTE: &str =
    "Summarize in one natural sentence and say how old the newest message is.";
const TRANSCRIPT_NOTE: &str = "Read without prompting the agent. Read it word for word when the \
    user asks for their words.";

/// An agent of the Host, as the tools of the fleet know it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Agent<'a> {
    pub agent_id: &'a str,
    pub persona: &'a str,
    pub session: &'a str,
    pub backend: &'a str,
    pub personality: &'a str,
    pub archived: bool,
    pub janitor: bool,
}

/// The agents work can be handed to: those that are not put away and are no janitors.
fn roster<'a, 'b>(agents: &'b [Agent<'a>]) -> impl Iterator<Item = &'b Agent<'a>> {
    agents
        .iter()
        .filter(|agent| !agent.archived && !agent.janitor)
}

/// The one agent of this name: by its session or by what it is called, whatever the case.
pub fn resolve<'a, 'b>(
    agents: &'b [Agent<'a>],
    name: &Value,
) -> Result<&'b Agent<'a>, &'static str> {
    let name = python::casefold(python::strip(&said(name)));
    let named = |agent: &&Agent| {
        name == python::casefold(agent.session) || name == python::casefold(agent.persona)
    };
    let mut found = roster(agents).filter(named);
    match (found.next(), found.next()) {
        (Some(agent), None) => Ok(agent),
        _ => Err(NOBODY),
    }
}

/// The agents there are, and the Oracle's contact: what a router and a voice are shown.
pub fn list_agents(agents: &[Agent], contact: &str) -> Value {
    let shown: Vec<Value> = roster(agents)
        .map(|agent| {
            json!({
                "name": agent.persona,
                "session": agent.session,
                "backend": agent.backend,
                "personality": python::first(agent.personality, PERSONALITY_SIGNS),
            })
        })
        .collect();
    let contact = Some(contact).filter(|contact| !contact.is_empty());
    json!({ "agents": shown, "oracle_contact": contact, "note": ROSTER_NOTE })
}

/// What a Host is to do for a tool.
#[derive(Debug, Clone, PartialEq)]
pub enum Plan<'a, 'b> {
    /// Nothing: this is the answer.
    Answer(Value),
    /// Say what the agent is doing ([`status`]).
    Status(&'b Agent<'a>),
    /// Read what the agent said, for a sentence about it ([`messages`]).
    Messages(&'b Agent<'a>),
    /// Read what the agent said, for its own words ([`transcript`]): so many messages.
    Transcript(&'b Agent<'a>, usize),
    /// Stop the agent, and call off what this owner handed it. `follow`: then hand it this
    /// instead, under this name.
    Stop {
        agent: &'b Agent<'a>,
        follow: Option<(String, String)>,
    },
    /// Hand the agent this request, under this name.
    Hand {
        agent: &'b Agent<'a>,
        operation_id: String,
        request: String,
    },
}

/// A request as it is handed on: without the blanks around it, and within its bound.
fn request(arguments: &Value) -> Result<String, &'static str> {
    let asked = said(&arguments["request"]);
    let asked = python::strip(&asked);
    if asked.chars().count() > REQUEST_SIGNS {
        return Err(NO_REQUEST);
    }
    Ok(asked.to_string())
}

/// What a tool means: for the one who asks (`principal`), under the name of the action
/// (`call_id`), with the agents there are and the Oracle's contact.
pub fn plan<'a, 'b>(
    (name, arguments, call_id): (&str, &Value, &str),
    principal: &str,
    agents: &'b [Agent<'a>],
    contact: &str,
) -> Result<Plan<'a, 'b>, &'static str> {
    if name == "list_agents" {
        return Ok(Plan::Answer(list_agents(agents, contact)));
    }
    let agent = if name == "investigate_with_oracle" {
        if contact.is_empty() {
            return Ok(Plan::Answer(json!({ "error": NO_CONTACT })));
        }
        resolve(agents, &json!(contact))?
    } else {
        resolve(agents, &arguments["agent"])?
    };
    match name {
        "get_agent_status" => Ok(Plan::Status(agent)),
        "read_agent_messages" => Ok(Plan::Messages(agent)),
        "read_agent_transcript" => {
            // A number of messages that is none is the usual number.
            let wanted = arguments["limit"].as_i64().filter(|limit| *limit >= 1);
            let wanted = wanted.map_or(TRANSCRIPT_USUAL, |limit| {
                usize::try_from(limit).unwrap_or(TRANSCRIPT_MOST)
            });
            Ok(Plan::Transcript(agent, wanted.min(TRANSCRIPT_MOST)))
        }
        "cancel_agent" => {
            let follow = request(arguments)?;
            let named = || operation_id(principal, call_id, true);
            Ok(Plan::Stop {
                agent,
                follow: Some(follow)
                    .filter(|follow| !follow.is_empty())
                    .map(|follow| (named(), follow)),
            })
        }
        "delegate_to_agent" | "investigate_with_oracle" => {
            let request = request(arguments)?;
            if request.is_empty() {
                return Err(NO_REQUEST);
            }
            Ok(Plan::Hand {
                agent,
                operation_id: operation_id(principal, call_id, false),
                request,
            })
        }
        _ => Err(NO_TOOL),
    }
}

/// What a tool that handed work on answers: where the work stands, and under what name.
pub fn handed(status: &str, operation_id: &str, session: &str) -> Value {
    json!({ "status": status, "operation_id": operation_id, "session": session, "note": HANDED })
}

/// What a stop answers that hands nothing on instead.
pub fn stopped() -> Value {
    json!({ "cancelled": true, "note": STOPPED })
}

/// How long ago a moment was, in a few words; nothing for a moment that is not known.
pub fn age_text(then_ms: Option<i64>, now_ms: i64) -> String {
    let Some(then_ms) = then_ms else {
        return String::new();
    };
    let seconds = (now_ms - then_ms).max(0) / 1000;
    for (unit, size) in [("day", 86_400), ("hour", 3600), ("minute", 60)] {
        if seconds >= size {
            let count = seconds / size;
            let many = if count == 1 { "" } else { "s" };
            return format!("{count} {unit}{many} ago");
        }
    }
    "just now".to_string()
}

/// A message of an agent's conversation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Message<'a> {
    pub id: &'a str,
    /// `user`, `assistant`, or something that is no part of the talk.
    pub role: &'a str,
    pub text: &'a str,
    /// When it was said: as it is written with the message, and as a moment.
    pub timestamp: &'a str,
    pub at_ms: i64,
}

/// What an agent last did, as its states say.
#[derive(Debug, Clone, Copy)]
pub struct State<'a> {
    pub kind: &'a str,
    /// What it is about: a record, where there is one.
    pub detail: &'a Value,
    pub at_ms: Option<i64>,
}

/// What an agent is doing right now, for a sentence of the voice. `rows`: its newest
/// messages ([`STATUS_MESSAGES`] of them), the oldest first.
pub fn status(agent: &Agent, latest: Option<State>, rows: &[Message], now_ms: i64) -> Value {
    let kind = latest.map_or("", |state| state.kind);
    let kind = if kind.is_empty() { "idle" } else { kind };
    let busy = matches!(kind, "thinking" | "tool" | "spawned");
    let step = latest.and_then(|state| {
        let about = ["summary", "tool", "message"].map(|name| &state.detail[name]);
        about
            .into_iter()
            .find(|about| python::truthy(about))
            .map(said)
    });
    let said_last = rows
        .iter()
        .rev()
        .find(|row| row.role == "assistant" && !python::strip(row.text).is_empty());
    json!({
        "agent": agent.persona,
        "session": agent.session,
        "state": if busy { "working" } else { kind },
        "since": age_text(latest.and_then(|state| state.at_ms), now_ms),
        "current_step": python::first(&step.unwrap_or_default(), STEP_SIGNS),
        "last_said": said_last.map_or("", |row| python::first(row.text, LAST_SAID_SIGNS)),
        "last_said_age": age_text(said_last.map(|row| row.at_ms), now_ms),
        "note": STATUS_NOTE,
    })
}

/// The newest messages of the talk that say something, the oldest first: `limit` of them
/// at most, each within its bound and all of them within theirs, read from the newest back.
/// `rows`: the agent's newest messages, twice `limit` of them, the oldest first. Says also
/// whether there was more than is read. `mark_cuts`: a message that is not whole says so.
fn recent(
    rows: &[Message],
    (limit, message_signs, mut room): (usize, usize, usize),
    mark_cuts: bool,
) -> (Vec<Value>, bool) {
    let of_the_talk =
        |row: &&Message| (row.role == "user" || row.role == "assistant") && !row.text.is_empty();
    let talk: Vec<&Message> = rows.iter().filter(of_the_talk).collect();
    let selected = &talk[talk.len().saturating_sub(limit)..];
    let mut cut = talk.len() > selected.len();
    let mut read = Vec::new();
    for row in selected.iter().rev() {
        if room == 0 {
            cut = true;
            break;
        }
        let text = python::first(row.text, message_signs.min(room));
        room -= text.chars().count();
        let mut message = Map::new();
        message.insert("id".to_string(), json!(row.id));
        message.insert("role".to_string(), json!(row.role));
        message.insert("timestamp".to_string(), json!(row.timestamp));
        message.insert("text".to_string(), json!(text));
        if mark_cuts && text.len() < row.text.len() {
            message.insert("truncated".to_string(), json!(true));
        }
        read.push(Value::Object(message));
    }
    read.reverse();
    (read, cut)
}

/// How old the newest of the messages that were read is.
fn newest_age(read: &[Value], rows: &[Message], now_ms: i64) -> String {
    let stamps = read
        .iter()
        .filter_map(|message| message["timestamp"].as_str());
    let newest = stamps.max().unwrap_or("");
    let at = rows.iter().find(|row| row.timestamp == newest);
    age_text(at.map(|row| row.at_ms), now_ms)
}

/// What an agent said recently, for a sentence about it. `rows`: its newest messages, twice
/// [`MESSAGES_READ`] of them, the oldest first.
pub fn messages(agent: &Agent, rows: &[Message], now_ms: i64) -> Value {
    let bounds = (MESSAGES_READ, MESSAGE_SIGNS, MESSAGES_SIGNS);
    let (read, _) = recent(rows, bounds, false);
    let age = newest_age(&read, rows, now_ms);
    json!({
        "agent": agent.persona,
        "messages": read,
        "newest_message_age": age,
        "note": MESSAGES_NOTE,
    })
}

/// What an agent said recently, for its own words: every cut is marked. `rows`: its newest
/// messages, twice `limit` of them ([`Plan::Transcript`]), the oldest first.
pub fn transcript(agent: &Agent, rows: &[Message], limit: usize, now_ms: i64) -> Value {
    let bounds = (limit, TRANSCRIPT_MESSAGE_SIGNS, TRANSCRIPT_SIGNS);
    let (read, cut) = recent(rows, bounds, true);
    let age = newest_age(&read, rows, now_ms);
    json!({
        "agent": agent.persona,
        "session": agent.session,
        "messages": read,
        "newest_message_age": age,
        "truncated": cut,
        "note": TRANSCRIPT_NOTE,
    })
}

/// The messages of the talk as [`recent`] reads them, for a test of the bounds themselves.
#[doc(hidden)]
pub fn read(
    rows: &[Message],
    bounds: (usize, usize, usize),
    mark_cuts: bool,
) -> (Vec<Value>, bool) {
    recent(rows, bounds, mark_cuts)
}
