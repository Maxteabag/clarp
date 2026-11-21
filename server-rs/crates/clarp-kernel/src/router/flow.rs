//! A message's way through the router.
//!
//! The router is a janitor's job (`message-delegator`): a message is routed in a run of
//! that janitor, under an id made from the message's own, so that a message sent twice is
//! decided once. The way has four steps, and between them the Host does what only a Host
//! can: [`Book::begin`] says whether the router takes the message at all and what a model
//! is shown; [`Book::answered`] reads what the model answered and says whether it is asked
//! once more with every agent in view; [`Book::apply`] makes of the decision what is done
//! (a message given to an agent, a word aloud, a message held while its user is asked, an
//! agent opened, stopped or started); [`Book::finish`] keeps what came of it.
//!
//! The rules are the Python Host's (`server/lib/orchestrator.py`, `handle_send`,
//! `_apply_decision`, `_command_check` and `_replayed_run`, as they are since its PRs 436 and 443
//! of 18 and 19 Nov 2026), held message by message by `tests/fixtures/router-flow-vectors.json`.
//! A command is never held; a held message belongs to the session it was made in; a name given
//! anywhere is the target, resolved as a voice name. What nothing was delivered for (a question,
//! a command that did nothing) answers the session spoken to, and its row and event name the
//! session its target resolved to, or none; an app is told nothing aloud. A run's receipt keeps
//! what its first answer said, and a message sent again is answered that.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;
use unicode_normalization::char::canonical_combining_class;

use super::packet::{self, Came, Member, Pending, Said, Shown};
use super::{
    AGENT_CONTROL, AGENT_MESSAGE, AMBIGUOUS, CLARIFY, CONTROL, CORRECTION, Decision, ERROR,
    IGNORED, JOB, STATUS, Settings, asks_again, should_run,
};
use crate::janitors::book::{Book as Janitors, World};
use crate::janitors::demand;
use crate::janitors::option_values;
use crate::python;

/// What became of a message, beside what a decision's kind already names (`clarify`,
/// `control`, `ignored`, `error`): it was given to an agent, its question was answered, or
/// the router had nothing to say and it goes where it was said to.
pub const ROUTE: &str = "route";
pub const TOLD_STATUS: &str = "status";
pub const FALLBACK: &str = "fallback";
/// A run of the router that is not over, and one that was cut off before it decided.
pub const PENDING: &str = "pending";
pub const CANCELLED: &str = "cancelled";
/// How long a message is held while its user is asked whom it was for.
pub const HELD_FOR_MS: i64 = 2 * 60 * 1000;
/// How long a held message is kept after that: a message that was given can be given
/// again for as long, when its agent did not take it.
const HELD_KEPT_MS: i64 = 24 * 60 * 60 * 1000;
/// How many decisions are kept: as many as anybody can ask for.
pub const DECISIONS_MOST: usize = 200;
/// How many of what the router gave an agent are kept: what a packet shows.
const GIVEN_MOST: usize = 5;
/// In a receipt: the agent did not take the message, and may when it is sent again.
const RETRYABLE: &str = "dispatch_retryable";

const CHANGED: &str = "Message delegator configuration changed";
const FAILED: &str = "Message routing failed";
const NOT_SEEN: &str = "I do not see that agent running.";
/// One of the router's own sentences, with the key its clip is kept under (the Python Host's
/// `orchestrator_phrases`): a decision refused in one of them keeps its key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Fixed {
    key: &'static str,
    text: &'static str,
}

const fn fixed(key: &'static str, text: &'static str) -> Fixed {
    Fixed { key, text }
}

/// What a command or a question about an agent is answered where it is not carried out.
const NO_SUCH_AGENT: Fixed = fixed("no_such_agent", "I do not have an agent by that name.");
const WHICH_AGENT: Fixed = fixed("which_agent", "Which agent did you mean?");
const CANNOT_CHECK: Fixed = fixed("cannot_check", "I cannot check on that agent from here.");
const CANNOT_COMMAND: Fixed = fixed(
    "cannot_command",
    "I cannot do that for that agent from here.",
);
const UNSURE_COMMAND: Fixed = fixed("unsure_command", "I am not sure what you wanted me to do.");
const NO_VOICE_COMMANDS: Fixed = fixed(
    "no_voice_commands",
    "That agent does not accept voice commands.",
);
const FORK_IN_APP: Fixed = fixed("fork_in_app", "Please fork agents from the app.");
/// A new agent never takes a name an agent has already, as it is spoken.
const NAME_TAKEN: Fixed = fixed("name_taken", "I already have an agent by that name.");
/// The Host refused to start an agent: said, never a silent error.
const COMMAND_REFUSED: Fixed = fixed("command_refused", "I could not do that.");
/// A confirmation of a message that is no longer held here: nothing is given in its place.
/// A message for an agent whose name is none the model was shown, or one that cannot be spoken to.
const NO_VALID_TARGET: Fixed = fixed("no_valid_target", "I am not sure which agent that was for.");
const HELD_MESSAGE_GONE: Fixed = fixed(
    "held_message_gone",
    "That message is no longer waiting. Please say it again.",
);

const CLARIFY_TARGET: Fixed = fixed("clarify_target", "Was that for me?");
const AMBIGUOUS_TARGET: Fixed = fixed("ambiguous_target", "I am not sure who that was for.");
const REPEAT_MISSING: Fixed = fixed("repeat_missing", "I do not have anything recent to replay.");
const CONTROL_UNKNOWN: Fixed = fixed(
    "control_unknown",
    "I am not sure what control action you meant.",
);
const AGENT_CONTROL_UNKNOWN: Fixed =
    fixed("agent_control_unknown", "I am not sure how to do that yet.");

/// Only the router's own words can address its phrase store.
pub fn phrase_key(text: &str) -> Option<&'static str> {
    [
        CLARIFY_TARGET,
        AMBIGUOUS_TARGET,
        REPEAT_MISSING,
        CONTROL_UNKNOWN,
        AGENT_CONTROL_UNKNOWN,
        NO_SUCH_AGENT,
        WHICH_AGENT,
        CANNOT_CHECK,
        CANNOT_COMMAND,
        UNSURE_COMMAND,
        NO_VOICE_COMMANDS,
        FORK_IN_APP,
        NAME_TAKEN,
        COMMAND_REFUSED,
        NO_VALID_TARGET,
        HELD_MESSAGE_GONE,
    ]
    .iter()
    .find(|phrase| phrase.text == text)
    .map(|phrase| phrase.key)
}

/// A decision as it is kept.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Kept {
    pub decision_id: i64,
    pub trace_id: String,
    pub utterance: String,
    pub requested_session: String,
    pub hands_free: bool,
    pub enabled: bool,
    pub provider: String,
    pub model: String,
    pub effort: String,
    pub latency_ms: i64,
    pub context_hash: Option<String>,
    pub context_agent_count: i64,
    pub context_message_count: i64,
    pub decision_kind: String,
    pub target_session: String,
    pub confidence: f64,
    pub addressing: bool,
    pub mentioned_sessions: Vec<String>,
    pub name_corrections: Vec<Value>,
    pub candidate_scores: Vec<Value>,
    pub reason: String,
    pub raw_response: Map<String, Value>,
    pub final_action: String,
    pub fallback_used: bool,
    pub phrase_key: String,
    pub error: String,
    pub created_at: i64,
}

impl Kept {
    /// The decision as the Python Host keeps it: what is a list or an object as the text
    /// of its JSON, and yes and no as one and nothing.
    pub fn row(&self) -> Value {
        let text = |items: Value| python::compact_ascii(&items);
        json!({
            "decision_id": self.decision_id, "trace_id": self.trace_id,
            "utterance": self.utterance, "requested_session": self.requested_session,
            "hands_free": i64::from(self.hands_free), "enabled": i64::from(self.enabled),
            "provider": self.provider, "model": self.model, "effort": self.effort,
            "latency_ms": self.latency_ms, "context_hash": self.context_hash,
            "context_agent_count": self.context_agent_count,
            "context_message_count": self.context_message_count,
            "decision_kind": self.decision_kind, "target_session": self.target_session,
            "confidence": self.confidence, "addressing": i64::from(self.addressing),
            "mentioned_sessions_json": text(json!(self.mentioned_sessions)),
            "name_corrections_json": text(json!(self.name_corrections)),
            "candidate_scores_json": text(json!(self.candidate_scores)),
            "reason": self.reason,
            "raw_response_json": text(Value::Object(self.raw_response.clone())),
            "final_action": self.final_action, "fallback_used": i64::from(self.fallback_used),
            "phrase_key": self.phrase_key, "error": self.error, "created_at": self.created_at,
        })
    }

    /// The decision as an app reads it: the row, and what the row keeps as text also read.
    pub fn shown(&self) -> Value {
        let mut shown = self.row();
        shown["error"] = json!(crate::janitors::routing_input::public_error(&self.error));
        shown["mentioned_sessions"] = json!(self.mentioned_sessions);
        shown["name_corrections"] = json!(self.name_corrections);
        shown["candidate_scores"] = json!(self.candidate_scores);
        shown["raw_response"] = Value::Object(self.raw_response.clone());
        shown
    }
}

/// What the router keeps.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Book {
    /// What was decided, the oldest first: the newest two hundred.
    pub decisions: Vec<Kept>,
    /// The number of the last decision. No number is ever another decision's.
    pub decided: i64,
    /// The messages that are held, and those that were given since, the oldest first.
    pub held: Vec<Pending>,
    /// What the router gave each agent, by the agent's id: the newest few, the oldest first.
    pub given: BTreeMap<String, Vec<Said>>,
}

/// A message that came in.
#[derive(Debug, Clone, Copy)]
pub struct Message<'a> {
    pub text: &'a str,
    /// The agent it was said to.
    pub requested_session: &'a str,
    pub trace_id: &'a str,
    pub hands_free: bool,
    /// An app asks for it, because it could place the message by no name.
    pub fallback_request: bool,
    /// The agents of a call: nobody else can be meant.
    pub only_sessions: Option<&'a [String]>,
    /// How it came in. None: it is known by its trace alone.
    pub came: Option<&'a Came>,
}

/// What the router reads of the Host around it, and the janitors' book its runs are in.
pub struct Around<'a> {
    /// Every agent of the Host, in the order they are listed: also those put away and the
    /// janitors, each with what it was last told and said (the router's own
    /// [`Book::given`] among it).
    pub agents: &'a [Member],
    /// The agent that is open, by its id.
    pub focus: Option<&'a str>,
    pub janitors: &'a mut Janitors,
    pub world: &'a dyn World,
    pub now_ms: i64,
}

/// A message to give to an agent.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Give {
    pub text: String,
    /// The agent it is for.
    pub session: String,
    /// The agent it was said to.
    pub requested_session: String,
    pub trace_id: String,
    /// How it came in: its own way in where it was held, else the way of the message that
    /// was just routed. None: no way in is known, and it goes as a user's.
    pub came: Option<Came>,
}

/// Something to say aloud.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Say {
    pub text: String,
    pub voice_id: String,
    /// The agent it is said as. None: the router says it, in its own voice.
    pub session: Option<String>,
    /// The name of a phrase that may be kept ready-spoken.
    pub phrase_key: String,
}

/// An agent to start: again as it is known, or a new one.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Start {
    /// The session of the agent that is started again: everything about it is taken from
    /// what the Host knows of it. None: a new agent is made.
    pub again: Option<String>,
    /// The name of a new agent, where the message gave one. Nothing else a model says of
    /// an agent is taken: it has read what other agents wrote.
    pub name: Option<String>,
}

/// What the Host does for a decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    /// Give a message to an agent. What comes of it is told to [`Book::finish`].
    Give(Give),
    Say(Say),
    /// Play again what an agent last said aloud. Where there is nothing: say `or`.
    Repeat {
        session: String,
        or: Option<Say>,
    },
    /// Open an agent: a message that names nobody is for it from now on.
    Focus {
        agent_id: String,
        session: String,
    },
    /// Stop what an agent is doing.
    Stop {
        agent_id: String,
    },
    /// Start an agent. What comes of it is told to [`Book::finish`].
    Start(Start),
}

/// What came of what the Host did for a plan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Done {
    /// All of it was done, and none of it was a message given or an agent started.
    Fine,
    /// The message was given: to which agent, run by which tool.
    Given { session: String, backend: String },
    /// The agent did not take the message: the status and the words of its refusal. A
    /// status of five hundred or more: it may take it when the message is sent again.
    NotGiven { status: u16, error: String },
    /// An agent was started or made: its session.
    Started { session: String },
    /// The Host refused to start or make the agent: its word for why. Nothing was done.
    Refused { code: String },
    /// Something else failed.
    Broke(String),
}

/// What a message's sender is answered, and what the apps are told.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Outcome {
    pub ok: bool,
    /// The agent the message went to, or was said to.
    pub session: String,
    /// The tool of the agent a message was given to.
    pub dispatch: String,
    pub trace_id: String,
    /// `route`, `clarify`, `status`, `control`, `ignored`, `fallback`, `error`, or of a
    /// message sent again `pending` or `cancelled`.
    pub action: String,
    pub decision_id: Option<i64>,
    pub decision: Option<Value>,
    pub error: String,
    pub status: u16,
    /// The event the apps are told.
    pub told: Option<Value>,
    /// What the Host is still to say, after it did what the decision asked: that it refused
    /// to start an agent.
    pub say: Option<Say>,
    /// The session the decision resolved to, `""` for none, which the run's receipt names; `None`
    /// where the answer's session already is it (a message given, an answer without a decision).
    /// Never in what is answered.
    pub target: Option<String>,
}

/// One routing under way: what the Host holds from [`Book::begin`] to [`Book::finish`].
#[derive(Debug, Clone)]
pub struct Routing {
    run_id: String,
    /// Whom to ask, how sure it has to be, and in which voice the router speaks.
    pub settings: Settings,
    /// What the decision is held against: the packet the model answered to.
    packet: Value,
    /// The packet with every agent in view, while the model is asked once more.
    wider: Option<Value>,
    /// The model is being asked that second time.
    widening: bool,
    decided: Option<(Decision, Map<String, Value>)>,
    text: String,
    requested_session: String,
    trace_id: String,
    hands_free: bool,
    fallback_request: bool,
    only_sessions: Option<Vec<String>>,
    came: Option<Came>,
}

impl Routing {
    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    /// What the model is shown now.
    pub fn packet(&self) -> &Value {
        self.wider.as_ref().unwrap_or(&self.packet)
    }

    fn message(&self) -> Message<'_> {
        Message {
            text: &self.text,
            requested_session: &self.requested_session,
            trace_id: &self.trace_id,
            hands_free: self.hands_free,
            fallback_request: self.fallback_request,
            only_sessions: self.only_sessions.as_deref(),
            came: self.came.as_ref(),
        }
    }
}

/// A message that was routed before and whose agent did not take it then.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Again {
    pub give: Give,
    decision_id: i64,
    /// The trace it was routed under.
    routed_trace: String,
}

/// How a message's way begins.
#[derive(Debug, Clone)]
pub enum Begun {
    /// The router takes no such message: it goes to the agent it was said to.
    NotMine,
    /// It is answered without a model.
    Answered(Outcome),
    /// It was decided before, and its agent did not take it then: it is given again, and
    /// what comes of that is told to [`Book::given_again`].
    Again(Again),
    /// A model is asked: [`Routing::packet`] is what it is shown.
    Ask(Box<Routing>),
}

/// What follows a model's answer.
#[derive(Debug, Clone, PartialEq)]
pub enum Next {
    /// The model is asked once more, with every agent in view ([`Routing::packet`]).
    AskAgain,
    /// It is decided: [`Book::apply`] says what is done.
    Decided,
    /// The router was set anew while the model was asked: nothing is done.
    Answered(Box<Outcome>),
}

/// What is done for a decision, and what is kept of it afterwards.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    /// What the Host does, in this order.
    pub effects: Vec<Effect>,
    decision: Decision,
    raw: Map<String, Value>,
    action: String,
    target: String,
    /// The decision was a command (before a refusal made it a question).
    command: bool,
    fallback_used: bool,
    error: String,
}

fn sha256(text: &str) -> String {
    let digest = Sha256::digest(text.as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The id of the run a message is routed in: made of the id its sender gave it, so that a
/// message that is sent again finds the run it has. A message that came with no id is
/// known by its trace.
pub fn request_id(came: Option<&Came>, trace_id: &str) -> Value {
    let client = match came {
        Some(came) => {
            let id = python::strip(&came.client_id);
            let marked = id.starts_with("u-");
            if marked {
                id.to_string()
            } else {
                format!("u-{id}")
            }
        }
        None => trace_id.to_string(),
    };
    json!(format!("routing-{}", sha256(&client)))
}

/// The agent a session name or an id names.
fn lookup<'a>(agents: &'a [Member], key: &str) -> Option<&'a Member> {
    let key = python::strip(key);
    if key.is_empty() {
        return None;
    }
    let by_session = agents.iter().find(|agent| agent.session == key);
    by_session.or_else(|| agents.iter().find(|agent| agent.agent_id == key))
}

/// A spoken name as names are compared: its case folded as Python folds it, and its accents
/// taken off (the signs that combine, after the compatibility decomposition).
pub fn fold(name: &str) -> String {
    let folded = python::casefold(name);
    let bare: String = folded
        .nfkd()
        .filter(|sign| canonical_combining_class(*sign) == 0)
        .collect();
    python::strip(&bare).to_string()
}

/// The agents a spoken name stands for: the agent of that session or id where there is
/// one, else every agent whose session or name it is, case and accents aside. Agents that
/// were put away and janitors count, so that one out of reach is told apart from one that
/// does not exist.
fn agents_named<'a>(agents: &'a [Member], name: &str) -> Vec<&'a Member> {
    if let Some(exact) = lookup(agents, name) {
        return vec![exact];
    }
    let key = fold(name);
    if key.is_empty() {
        return Vec::new();
    }
    let named = |agent: &&Member| fold(&agent.session) == key || fold(&agent.persona) == key;
    agents.iter().filter(named).collect()
}

/// A status kept in a receipt, where a reply to a client can carry it: a whole number from 200 to
/// 599 but 204 and 304 (yes and no are none). A client cannot read the others as this answer: 0 or
/// 99 make the status line unreadable, one of the hundreds is passed over as interim, and the
/// hundreds, 204 and 304 carry no body, so the answer would be lost.
fn answerable(status: &Value) -> Option<u16> {
    // A fraction is no whole number, though it be 200.0.
    let status = status.as_i64()?;
    let status = u16::try_from(status).ok()?;
    ((200..=599).contains(&status) && ![204, 304].contains(&status)).then_some(status)
}

/// One receipt with the fields of another added.
fn merged(receipt: &Value, more: &Value) -> Value {
    let mut merged = receipt.clone();
    if let (Some(into), Some(more)) = (merged.as_object_mut(), more.as_object()) {
        into.extend(more.iter().map(|(key, value)| (key.clone(), value.clone())));
    }
    merged
}

/// What a run is closed with where its receipt was refused: what its first answer said (its
/// status and tool, and its session where that fits) beside its action, recipient and status of
/// failure; without the session where that does not fit; else its action alone.
fn least_receipt(receipt: &Value, action: &str) -> Value {
    const KEPT: [&str; 9] = [
        "status",
        "target_agent_id",
        "http_status",
        "answer_status",
        "answer_dispatch",
        "answer_session",
        "decision_id",
        "decision_target",
        "decision_action",
    ];
    let kept = KEPT.iter().filter_map(|key| {
        let value = receipt.get(*key)?;
        Some(((*key).to_string(), value.clone()))
    });
    let mut least = Value::Object(kept.collect());
    if !demand::fits(&least)
        && let Some(least) = least.as_object_mut()
    {
        least.remove("answer_session");
    }
    if !demand::fits(&least) {
        least = json!({ "status": action });
    }
    least
}

/// The session a name stands for: its one agent, by id, session or voice name (one that was put
/// away and a janitor too); `""` for none, or for more than one.
fn resolved(agents: &[Member], name: &str) -> String {
    let name = python::strip(name);
    match agents_named(agents, name).as_slice() {
        [one] if !name.is_empty() => one.session.clone(),
        _ => String::new(),
    }
}

/// The names a command gives: the decision's own and, of a command for an agent, the payload's.
fn names_given(decision: &Decision) -> Vec<&str> {
    let mut names = vec![python::strip(&decision.target_session)];
    if decision.kind == AGENT_CONTROL {
        let payload = decision.control_payload.as_ref();
        let given = payload.and_then(|payload| payload.get("name"));
        names.push(given.and_then(Value::as_str).map_or("", python::strip));
    }
    names.retain(|name| !name.is_empty());
    names
}

/// The session every name a command gives resolves to, one agent; `""` for none, for more than
/// one, for names that disagree, or where it gives no name.
fn named_session(decision: &Decision, agents: &[Member]) -> String {
    let mut sessions: Vec<String> = Vec::new();
    for name in names_given(decision) {
        let session = resolved(agents, name);
        if !sessions.contains(&session) {
            sessions.push(session);
        }
    }
    match sessions.as_slice() {
        [one] => one.clone(),
        _ => String::new(),
    }
}

/// Whether a command is carried out: the sentence it is refused with, where it is, and the
/// session its name resolved to (`""` where it resolved to none, and for a command that makes an
/// agent). A refused command still names the session its name resolved to: one out of reach. A
/// name given anywhere, the decision's own or, of a command for an agent, the payload's, is the
/// target; the agent the message was said to is the target only where none is given. It acts only
/// on an agent of the packet: in a call, one of the call. `asked`: the command, as it is compared.
fn command_check(
    decision: &Decision,
    asked: &str,
    sure: bool,
    shown: &[&str],
    requested: &str,
    agents: &[Member],
) -> (Option<Fixed>, String) {
    if !sure {
        return (Some(UNSURE_COMMAND), named_session(decision, agents));
    }
    let of_agents = decision.kind == AGENT_CONTROL;
    if of_agents && asked == "fork" {
        // No source of a fork is taken from the router; a real agent it names is kept.
        return (Some(FORK_IN_APP), named_session(decision, agents));
    }
    if of_agents && asked == "create" {
        // A name the voice already gives an agent (one that was put away too) is taken: the
        // Host compares names as they are written only. (A blank name names nobody.)
        let payload = decision.control_payload.as_ref();
        let given = payload.and_then(|payload| payload.get("name"));
        let given = given.and_then(Value::as_str).unwrap_or_default();
        if !agents_named(agents, given).is_empty() {
            return (Some(NAME_TAKEN), String::new());
        }
        return (None, String::new());
    }
    let names = names_given(decision);
    let agent = if names.is_empty() {
        lookup(agents, requested)
    } else {
        let mut found: Vec<&Member> = Vec::new();
        for name in names {
            match agents_named(agents, name).as_slice() {
                [] => return (Some(NO_SUCH_AGENT), String::new()),
                [one] => found.push(one),
                _ => return (Some(WHICH_AGENT), String::new()),
            }
        }
        // The two names given disagree.
        if found
            .iter()
            .any(|agent| agent.agent_id != found[0].agent_id)
        {
            return (Some(WHICH_AGENT), String::new());
        }
        found.first().copied()
    };
    let Some(agent) = agent else {
        return (Some(NO_SUCH_AGENT), String::new());
    };
    if !agent.voice_target {
        return (Some(NO_VOICE_COMMANDS), agent.session.clone());
    }
    // No packet shows an agent that was put away: it is out of reach as one of another call is.
    if !shown.contains(&agent.session.as_str()) {
        return (Some(CANNOT_COMMAND), agent.session.clone());
    }
    (None, agent.session.clone())
}

/// The name of the agent of a session, or the session's own name where it has none.
fn name_of(agents: &[Member], session: &str) -> String {
    let persona = lookup(agents, session).map(|agent| agent.persona.as_str());
    persona
        .filter(|persona| !persona.is_empty())
        .unwrap_or(session)
        .to_string()
}

/// The session a decision names, where it is one of those the model was shown and its
/// agent can be spoken to.
fn valid<'a>(agents: &[Member], shown: &[&str], session: &'a str) -> Option<&'a str> {
    let agent = lookup(agents, session).filter(|_| shown.contains(&session));
    agent
        .filter(|agent| !agent.archived && agent.voice_target)
        .map(|_| session)
}

/// How the router is set, read from what a janitor keeps of its job.
fn settings_of(
    options: Option<&Value>,
    enabled: bool,
    provider: &str,
    model: &str,
    effort: &str,
) -> Settings {
    let options = option_values(JOB, options).unwrap_or_default();
    let unset = Settings::default();
    let yes = |key: &str, or: bool| options.get(key).and_then(Value::as_bool).unwrap_or(or);
    let voice = options.get("voice_id").and_then(Value::as_str);
    let bar = options.get("confidence_threshold").and_then(Value::as_f64);
    let time = options.get("timeout_ms").and_then(Value::as_i64);
    Settings {
        enabled,
        hands_free_only: yes("hands_free_only", unset.hands_free_only),
        fallback_only: yes("fallback_only", unset.fallback_only),
        confidence_threshold: bar.unwrap_or(unset.confidence_threshold),
        provider: provider.to_string(),
        model: model.to_string(),
        effort: effort.to_string(),
        timeout_ms: time.unwrap_or(unset.timeout_ms),
        voice_id: voice.map_or(unset.voice_id, str::to_string),
    }
}

/// How the router of a Host is set: as the janitor of the Host's own keeps it whose job
/// routing is. `None`: the Host has no such janitor.
pub fn settings(janitors: &Janitors, world: &dyn World) -> Option<Settings> {
    let own = janitors
        .builtins
        .iter()
        .find(|(_, role)| role.as_str() == JOB);
    let (id, _) = own?;
    let config = janitors.configs.get(id)?;
    let agent = world.agent(id)?;
    let provider = config.execution.get("provider").and_then(Value::as_str);
    let options = Value::Object(config.options.clone());
    Some(settings_of(
        Some(&options),
        config.enabled,
        provider.unwrap_or_default(),
        &agent.model,
        &agent.effort,
    ))
}

/// A decision turned into a question to the user: in the model's own words where it gave
/// some, else in these.
fn as_question(mut decision: Decision, words: &str) -> Decision {
    decision.kind = CLARIFY.to_string();
    if decision.spoken_text.is_empty() {
        decision.spoken_text = words.to_string();
    }
    if decision.phrase_key.is_empty() {
        decision.phrase_key = "clarify_target".to_string();
    }
    decision
}

/// How an agent is doing, in one sentence: by what it last said, else by what it was last
/// doing.
fn status_of(agent: &Member) -> String {
    let persona = &agent.persona;
    let said = packet::last_said(agent, "assistant", 2);
    let last = said.last().and_then(|said| said["text"].as_str());
    match (last, &agent.state) {
        (Some(text), _) => format!(
            "{persona} was last working on: {}",
            python::first(text, 180)
        ),
        (None, Some(state)) => format!("{persona} is currently {}.", state.kind),
        (None, None) => format!("{persona} is active, but I do not have recent work details yet."),
    }
}

impl Book {
    /// What the router gave an agent, to be shown among what the agent was last told.
    pub fn given_to(&self, agent_id: &str) -> &[Said] {
        self.given
            .get(agent_id)
            .map(Vec::as_slice)
            .unwrap_or_default()
    }

    /// The decisions, the newest first: at most so many, and no more than are kept.
    pub fn recent(&self, limit: i64, only: Option<&str>) -> Vec<Value> {
        let most = usize::try_from(limit.clamp(1, 200)).unwrap_or(1);
        let newest = self.decisions.iter().rev();
        let wanted = newest.filter(|kept| only.is_none_or(|action| kept.final_action == action));
        wanted.take(most).map(Kept::shown).collect()
    }

    /// The packet for a message.
    fn packet_for(
        &self,
        message: &Message<'_>,
        settings: &Settings,
        scope_all: bool,
        around: &Around<'_>,
    ) -> Value {
        packet::packet(&Shown {
            utterance: message.text,
            requested_session: message.requested_session,
            trace_id: message.trace_id,
            hands_free: message.hands_free,
            settings,
            scope_all,
            fallback_request: message.fallback_request,
            only_sessions: message.only_sessions,
            focus: around.focus,
            agents: around.agents,
            pending: &self.held,
            now_ms: around.now_ms,
        })
    }

    /// Keeps a decision. Answers its number.
    #[allow(clippy::too_many_arguments)]
    fn keep(
        &mut self,
        decision: &Decision,
        raw: &Map<String, Value>,
        packet: &Value,
        settings: &Settings,
        message: &Message<'_>,
        latency_ms: i64,
        target: &str,
        action: &str,
        fallback_used: bool,
        error: &str,
        now_ms: i64,
    ) -> i64 {
        self.decided += 1;
        let context = &packet["context_summary"];
        self.decisions.push(Kept {
            decision_id: self.decided,
            trace_id: message.trace_id.to_string(),
            utterance: packet["utterance"].as_str().unwrap_or_default().to_string(),
            requested_session: message.requested_session.to_string(),
            hands_free: message.hands_free,
            enabled: settings.enabled,
            provider: settings.provider.clone(),
            model: settings.model.clone(),
            effort: settings.effort.clone(),
            latency_ms,
            context_hash: context["hash"].as_str().map(str::to_string),
            context_agent_count: context["agent_count"].as_i64().unwrap_or(0),
            context_message_count: context["message_count"].as_i64().unwrap_or(0),
            decision_kind: decision.kind.clone(),
            target_session: target.to_string(),
            confidence: decision.confidence,
            addressing: decision.addressing,
            mentioned_sessions: decision.mentioned_sessions.clone(),
            name_corrections: decision.name_corrections.clone(),
            candidate_scores: decision.candidate_scores.clone(),
            reason: decision.reason.clone(),
            raw_response: raw.clone(),
            final_action: action.to_string(),
            fallback_used,
            phrase_key: decision.phrase_key.clone(),
            error: error.to_string(),
            created_at: now_ms,
        });
        let over = self.decisions.len().saturating_sub(DECISIONS_MOST);
        self.decisions.drain(..over);
        self.decided
    }

    /// The router has nothing to say of a message: it goes where it was said to, and that
    /// is kept as a decision of its own.
    fn unavailable(
        &mut self,
        packet: &Value,
        settings: &Settings,
        message: &Message<'_>,
        reason: &str,
        now_ms: i64,
    ) -> Outcome {
        let decision = Decision {
            kind: ERROR.to_string(),
            reason: reason.to_string(),
            ..Decision::default()
        };
        let session = message.requested_session;
        let none = Map::new();
        let id = self.keep(
            &decision, &none, packet, settings, message, 0, session, FALLBACK, true, "", now_ms,
        );
        Outcome {
            ok: true,
            session: session.to_string(),
            trace_id: message.trace_id.to_string(),
            action: FALLBACK.to_string(),
            decision_id: Some(id),
            decision: Some(decision.told()),
            status: 200,
            ..Outcome::default()
        }
    }

    /// Reconstruct the first client-facing decision. The row's target can differ from
    /// the target in the reply. A retryable legacy receipt may have only the decision id.
    fn replayed_decision(&self, receipt: &Map<String, Value>) -> Option<(&Kept, Value)> {
        let id = receipt.get("decision_id")?.as_i64()?;
        let target = receipt.get("decision_target")?;
        let kept = self.decisions.iter().find(|kept| kept.decision_id == id)?;
        let word = |value: &Value| {
            if !python::truthy(value) {
                String::new()
            } else {
                value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| python::repr(value))
            }
        };
        let action = receipt.get("decision_action").unwrap_or(&Value::Null);
        let decision = Decision {
            kind: kept.decision_kind.clone(),
            target_session: word(target),
            confidence: kept.confidence,
            addressing: kept.addressing,
            reason: kept.reason.clone(),
            phrase_key: kept.phrase_key.clone(),
            control_action: word(action),
            ..Decision::default()
        };
        Some((kept, decision.told()))
    }

    /// What a message is answered that was routed before: by what its run came to.
    fn replayed(&self, run_id: &str, message: &Message<'_>, around: &mut Around<'_>) -> Outcome {
        let out = |status: &str| ["queued", "running"].contains(&status);
        let is_out = |janitors: &Janitors| {
            janitors
                .runs
                .get(run_id)
                .is_some_and(|run| out(&run.status))
        };
        if is_out(around.janitors) {
            // A message sent again never begins a run, which is where runs that were not
            // finished in their time are given up: so they are given up here too.
            around.janitors.recover_expired(around.now_ms);
        }
        let run = around.janitors.runs.get(run_id);
        let status = run.map(|run| run.status.as_str()).unwrap_or_default();
        let receipt = run.and_then(|run| run.demand_result.as_ref());
        let receipt = receipt
            .and_then(Value::as_object)
            .filter(|receipt| !receipt.is_empty());
        let decision = receipt
            .filter(|_| !out(status))
            .and_then(|receipt| self.replayed_decision(receipt));
        let answer = |ok: bool, session: &str, action: &str, status: u16, error: &str| Outcome {
            ok,
            session: session.to_string(),
            trace_id: decision
                .as_ref()
                .map(|(kept, _)| kept.trace_id.as_str())
                .filter(|trace| !trace.is_empty())
                .unwrap_or(message.trace_id)
                .to_string(),
            decision_id: decision.as_ref().map(|(kept, _)| kept.decision_id),
            decision: decision.as_ref().map(|(_, answer)| answer.clone()),
            action: action.to_string(),
            error: error.to_string(),
            status,
            ..Outcome::default()
        };
        let Some(receipt) = receipt else {
            if status == CANCELLED {
                let said = run
                    .map(|run| run.error.as_str())
                    .filter(|error| !error.is_empty());
                let error = said.unwrap_or("Message routing was cancelled before completion");
                return answer(false, message.requested_session, CANCELLED, 409, error);
            }
            let action = if out(status) { PENDING } else { CLARIFY };
            let status = if out(status) { 202 } else { 200 };
            return answer(true, message.requested_session, action, status, "");
        };
        let requested = message.requested_session;
        if out(status) {
            return answer(true, requested, PENDING, 202, "");
        }
        let action = receipt
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or(CLARIFY);
        let failed = action == ERROR;
        let error = if failed { FAILED } else { "" };
        // What the first answer said, where the receipt keeps it in a form a client can be
        // answered with: its status one a reply can carry, and its session a text that is not
        // empty, or none (the first answer named the session asked for, too long to keep).
        let said_status = receipt.get("answer_status").and_then(answerable);
        let said_session = receipt.get("answer_session").map(|session| {
            session
                .as_str()
                .filter(|session| !session.is_empty())
                .map(str::to_string)
        });
        let readable = said_session.as_ref().is_none_or(Option::is_some);
        if let Some(status) = said_status.filter(|_| readable) {
            let session = said_session
                .flatten()
                .unwrap_or_else(|| requested.to_string());
            let dispatched = receipt.get("answer_dispatch").and_then(Value::as_str);
            let mut outcome = answer(!failed, &session, action, status, error);
            outcome.dispatch = dispatched.unwrap_or_default().to_string();
            return outcome;
        }
        // A receipt written before the answer was kept, or one that keeps it in no form a
        // client can be answered with: answered by its recipient.
        let recipient = receipt.get("target_agent_id").and_then(Value::as_str);
        let recipient = recipient.and_then(|id| lookup(around.agents, id));
        let session = recipient.map_or(requested, |agent| agent.session.as_str());
        if failed {
            let said = receipt.get("http_status").and_then(answerable);
            return answer(false, session, ERROR, said.unwrap_or(500), FAILED);
        }
        let mut outcome = answer(true, session, action, 200, "");
        if action == ROUTE {
            outcome.dispatch = recipient
                .map(|agent| agent.backend.clone())
                .unwrap_or_default();
        }
        outcome
    }

    /// A message that was decided before and not taken by its agent: what is given again,
    /// by the decision that is kept. No model is asked a second time.
    fn again(&self, receipt: &Value, message: &Message<'_>, around: &Around<'_>) -> Begun {
        let recipient = receipt["target_agent_id"].as_str();
        let recipient = recipient.and_then(|id| lookup(around.agents, id));
        let number = receipt["decision_id"].as_i64();
        let kept = self
            .decisions
            .iter()
            .find(|kept| Some(kept.decision_id) == number);
        let (Some(recipient), Some(kept)) = (recipient, kept) else {
            return Begun::Answered(Outcome {
                session: message.requested_session.to_string(),
                trace_id: message.trace_id.to_string(),
                action: ERROR.to_string(),
                error: FAILED.to_string(),
                status: 500,
                ..Outcome::default()
            });
        };
        let said = Decision::read(&kept.raw_response);
        let held = Some(said.pending_id.as_str()).filter(|id| !id.is_empty());
        let held = held.and_then(|id| self.held.iter().find(|held| held.pending_id == id));
        let own = Some(said.text_to_send.as_str()).filter(|text| !text.is_empty());
        let text = held.map(|held| held.utterance.as_str());
        let text = text.filter(|text| !text.is_empty()).or(own);
        let routed_trace = match kept.trace_id.as_str() {
            "" => message.trace_id,
            trace => trace,
        };
        Begun::Again(Again {
            give: Give {
                text: text.unwrap_or(&kept.utterance).to_string(),
                session: recipient.session.clone(),
                requested_session: message.requested_session.to_string(),
                trace_id: message.trace_id.to_string(),
                came: match held {
                    Some(held) => held.came.clone(),
                    None => message.came.cloned(),
                },
            },
            decision_id: kept.decision_id,
            routed_trace: routed_trace.to_string(),
        })
    }

    /// What came of giving a message again.
    pub fn given_again(&mut self, again: &Again, done: Done, around: &Around<'_>) -> Outcome {
        let give = &again.give;
        let refused = |status: u16, error: String| Outcome {
            session: give.session.clone(),
            trace_id: give.trace_id.clone(),
            action: ERROR.to_string(),
            error,
            status,
            ..Outcome::default()
        };
        match done {
            Done::Given { session, backend } => {
                // What the agent was given is kept once, under the trace it was routed
                // by: sent a third time, the message is known already.
                let trace = &again.routed_trace;
                self.gave(&give.session, &give.text, trace, true, around);
                Outcome {
                    ok: true,
                    session,
                    dispatch: backend,
                    trace_id: give.trace_id.clone(),
                    action: ROUTE.to_string(),
                    decision_id: Some(again.decision_id),
                    status: 200,
                    ..Outcome::default()
                }
            }
            Done::NotGiven { status, error } => refused(status, error),
            Done::Broke(error) => refused(500, error),
            Done::Fine | Done::Started { .. } | Done::Refused { .. } => {
                refused(500, FAILED.to_string())
            }
        }
    }

    /// Keeps what the router gave an agent. `once`: only if nothing is kept for the agent
    /// under this trace yet.
    fn gave(&mut self, session: &str, text: &str, trace_id: &str, once: bool, around: &Around<'_>) {
        let text = python::strip(text);
        let Some(agent) = lookup(around.agents, session).filter(|_| !text.is_empty()) else {
            return;
        };
        let kept = self.given.entry(agent.agent_id.clone()).or_default();
        let known = |said: &Said| said.role == "user" && said.trace_id == trace_id;
        if once && kept.iter().any(known) {
            return;
        }
        kept.push(Said {
            role: "user".to_string(),
            text: text.to_string(),
            trace_id: trace_id.to_string(),
            source: "orchestrator".to_string(),
            created_at: around.now_ms,
        });
        let over = kept.len().saturating_sub(GIVEN_MOST);
        kept.drain(..over);
    }

    /// Forgets what nobody can ask for any more: what was given to agents that are gone,
    /// and messages that were held a day ago.
    fn forget(&mut self, around: &Around<'_>) {
        let there = |id: &String| around.agents.iter().any(|agent| agent.agent_id == *id);
        self.given.retain(|id, _| there(id));
        let kept_from = around.now_ms - HELD_KEPT_MS;
        self.held.retain(|held| held.expires_at > kept_from);
    }

    /// A message's way begins: whether the router takes it, and what a model is shown.
    pub fn begin(&mut self, message: &Message<'_>, around: &mut Around<'_>) -> Begun {
        self.forget(around);
        let answer = |status: u16, error: String| {
            Begun::Answered(Outcome {
                session: message.requested_session.to_string(),
                trace_id: message.trace_id.to_string(),
                action: ERROR.to_string(),
                error,
                status,
                ..Outcome::default()
            })
        };
        let request = request_id(message.came, message.trace_id);
        let target = lookup(around.agents, message.requested_session);
        let target = target.map(|agent| agent.agent_id.clone());
        let weighed = json!({
            "utterance": message.text, "requested_session": message.requested_session,
            "hands_free": message.hands_free, "fallback_request": message.fallback_request,
            "origin": message.came.map_or("user", |came| came.origin.as_str()),
            "sender_agent_id": message.came.map_or("", |came| came.sender_agent_id.as_str()),
        });
        let context = json!({ "input_hash": sha256(&python::sorted_ascii(&weighed)) });
        let conflict = "Request identity already refers to different work";
        if let Ok(Some(prior)) = around.janitors.request_run(JOB, &request) {
            let frozen = &prior["configuration"];
            if frozen["context"] != context || frozen["target_agent_id"] != json!(target) {
                return answer(409, conflict.to_string());
            }
            if prior["demand_result"]["reason"] == RETRYABLE {
                return self.again(&prior["demand_result"], message, around);
            }
            let run_id = prior["run_id"].as_str().unwrap_or_default();
            return Begun::Answered(self.replayed(run_id, message, around));
        }
        let owner = around
            .janitors
            .resolve(JOB, target.as_deref(), around.world);
        let Ok(Some(owner)) = owner else {
            return Begun::NotMine;
        };
        let (Some(config), Some(janitor)) = (
            around.janitors.configs.get(&owner),
            around.world.agent(&owner),
        ) else {
            return Begun::NotMine;
        };
        let provider = config.execution.get("provider").and_then(Value::as_str);
        let options = Value::Object(config.options.clone());
        let settings = settings_of(
            Some(&options),
            config.enabled,
            provider.unwrap_or_default(),
            &janitor.model,
            &janitor.effort,
        );
        if !should_run(&settings, message.hands_free, message.fallback_request) {
            return Begun::NotMine;
        }
        let mut packet = self.packet_for(message, &settings, false, around);
        let now = around.now_ms;
        let begun = around.janitors.begin_run(
            JOB,
            &request,
            Some(&context),
            target.as_deref(),
            around.world,
            now,
        );
        let run = match begun {
            // The id of a message that is other work now is no reason to send that work
            // where it was said to: it is refused.
            Err(refused) => return answer(refused.status, refused.message),
            Ok(None) => {
                let why = "Message delegator is busy or paused";
                return Begun::Answered(self.unavailable(&packet, &settings, message, why, now));
            }
            Ok(Some(run)) => run,
        };
        let run_id = run["run_id"].as_str().unwrap_or_default().to_string();
        if !around.janitors.claim_run(&run_id, around.world, now) {
            return Begun::Answered(self.replayed(&run_id, message, around));
        }
        // From here on the router is as it was when the run began, whatever is set since.
        let frozen = &run["configuration"];
        let word = |key: &str| frozen[key].as_str().unwrap_or_default();
        let settings = settings_of(
            frozen.get("options"),
            true,
            word("provider"),
            word("model"),
            word("effort"),
        );
        packet["settings"] = settings.shown();
        packet["routing_policy"]["automatic_confidence_threshold"] =
            json!(settings.confidence_threshold);
        if !should_run(&settings, message.hands_free, message.fallback_request) {
            let result = json!({
                "summary": "Routing policy changed before execution", "status": FALLBACK,
            });
            let janitors = &mut *around.janitors;
            let _ = janitors.complete_run(
                &run_id,
                CANCELLED,
                Some(&result),
                "",
                None,
                around.world,
                now,
            );
            let why = "Message delegator policy changed";
            return Begun::Answered(self.unavailable(&packet, &settings, message, why, now));
        }
        Begun::Ask(Box::new(Routing {
            run_id,
            settings,
            packet,
            wider: None,
            widening: false,
            decided: None,
            text: message.text.to_string(),
            requested_session: message.requested_session.to_string(),
            trace_id: message.trace_id.to_string(),
            hands_free: message.hands_free,
            fallback_request: message.fallback_request,
            only_sessions: message.only_sessions.map(<[String]>::to_vec),
            came: message.came.cloned(),
        }))
    }

    /// What a model answered: the object in its answer, or why there is none. The first
    /// answer may send the router to ask once more with every agent in view; when that
    /// second asking fails, the first answer stands.
    pub fn answered(
        &mut self,
        routing: &mut Routing,
        answer: Result<Map<String, Value>, String>,
        around: &mut Around<'_>,
    ) -> Next {
        if routing.widening {
            routing.widening = false;
            let wider = routing.wider.take();
            if let (Ok(raw), Some(wider)) = (answer, wider) {
                routing.decided = Some((Decision::read(&raw), raw));
                routing.packet = wider;
            }
            return Next::Decided;
        }
        let raw = match answer {
            Ok(raw) => raw,
            Err(error) => {
                let failed = Decision {
                    kind: ERROR.to_string(),
                    reason: "orchestrator model failed".to_string(),
                    error,
                    ..Decision::default()
                };
                routing.decided = Some((failed, Map::new()));
                return Next::Decided;
            }
        };
        let now = around.now_ms;
        if !around
            .janitors
            .is_current(&routing.run_id, around.world, now)
        {
            let janitors = &mut *around.janitors;
            let _ = janitors.complete_run(
                &routing.run_id,
                CANCELLED,
                None,
                "",
                None,
                around.world,
                now,
            );
            let message = routing.message();
            let outcome =
                self.unavailable(&routing.packet, &routing.settings, &message, CHANGED, now);
            return Next::Answered(Box::new(outcome));
        }
        let decision = Decision::read(&raw);
        let scope = routing.packet["context_scope"].as_str();
        let again = asks_again(&decision, scope, &routing.requested_session);
        routing.decided = Some((decision, raw));
        if !again {
            return Next::Decided;
        }
        let wider = self.packet_for(&routing.message(), &routing.settings, true, around);
        routing.wider = Some(wider);
        routing.widening = true;
        Next::AskAgain
    }

    /// Makes the packet with every agent in view anew, from agents that are given whole.
    /// For a Host that gives [`Book::answered`] only what a first asking shows (the last
    /// words of the agent a message was said to and of the one that is open), and reads
    /// the others' only when a model is in fact asked a second time.
    pub fn widen(&self, routing: &mut Routing, around: &Around<'_>) {
        if routing.widening {
            let wider = self.packet_for(&routing.message(), &routing.settings, true, around);
            routing.wider = Some(wider);
        }
    }

    /// Holds a message while its user is asked whom it was for, and asks. A message that
    /// is held already in this session, and whose time is not up, keeps its own words, id,
    /// trace and way in when it is asked about again: only the question is new (whom it may
    /// be for, why it is asked, and until when). Any other message is held anew, under
    /// `hold_as`: a hold made while speaking to another agent is not that agent's to renew.
    fn hold(
        &mut self,
        decision: &Decision,
        routing: &Routing,
        hold_as: &str,
        around: &Around<'_>,
        effects: &mut Vec<Effect>,
    ) {
        let now = around.now_ms;
        let candidate = decision.target_session.as_str();
        let requested = routing.requested_session.as_str();
        let waiting = |held: &&mut Pending| {
            held.pending_id == decision.pending_id
                && held.status == "pending"
                && held.expires_at > now
                && held.requested_session == requested
        };
        if let Some(held) = self.held.iter_mut().find(waiting) {
            held.candidate_session = candidate.to_string();
            held.reason.clone_from(&decision.reason);
            held.expires_at = now + HELD_FOR_MS;
        } else {
            let said = routing.packet["utterance"].as_str().unwrap_or_default();
            self.held.push(Pending {
                pending_id: hold_as.to_string(),
                trace_id: routing.trace_id.clone(),
                utterance: said.to_string(),
                requested_session: routing.requested_session.clone(),
                candidate_session: candidate.to_string(),
                speak_as_session: routing.requested_session.clone(),
                reason: decision.reason.clone(),
                created_at: now,
                expires_at: now + HELD_FOR_MS,
                status: "pending".to_string(),
                came: routing.came.clone(),
            });
        }
        let own = python::strip(&decision.spoken_text);
        let text = match (own, candidate) {
            ("", "") => AMBIGUOUS_TARGET.text.to_string(),
            ("", candidate) => format!("Was that for {}?", name_of(around.agents, candidate)),
            (own, _) => own.to_string(),
        };
        let asked = self.as_agent(
            &routing.requested_session,
            text,
            &decision.phrase_key,
            around,
        );
        effects.extend(asked.map(Effect::Say));
    }

    /// Something said as an agent, in its voice: where there is such an agent.
    fn as_agent(
        &self,
        session: &str,
        text: impl Into<String>,
        phrase_key: &str,
        around: &Around<'_>,
    ) -> Option<Say> {
        let agent = lookup(around.agents, session)?;
        Some(Say {
            text: text.into(),
            voice_id: agent.voice_id.clone(),
            session: Some(session.to_string()),
            phrase_key: phrase_key.to_string(),
        })
    }

    /// The held message a decision names, taken to be given: once, while it is held and
    /// its time is not up, and only in the session it was held in (one held while speaking
    /// to another agent is not released from here; its time runs out).
    fn claim(&mut self, pending_id: &str, requested: &str, now_ms: i64) -> Option<Pending> {
        let waiting = |held: &&mut Pending| {
            held.pending_id == pending_id
                && held.status == "pending"
                && held.expires_at > now_ms
                && held.requested_session == requested
        };
        let held = self.held.iter_mut().find(waiting)?;
        held.status = "sent".to_string();
        Some(held.clone())
    }

    /// Makes of a decision what is done. `hold_as`: an id nobody has, under which the
    /// message is held should it be held and the model named none.
    pub fn apply(&mut self, routing: &Routing, hold_as: &str, around: &Around<'_>) -> Plan {
        let requested = routing.requested_session.as_str();
        let (decision, raw) = routing.decided.clone().unwrap_or_default();
        let now = around.now_ms;
        if !around
            .janitors
            .is_current(&routing.run_id, around.world, now)
        {
            // Set anew while it was decided: nothing of the decision is done.
            let changed = Decision {
                kind: ERROR.to_string(),
                reason: CHANGED.to_string(),
                ..Decision::default()
            };
            return Plan {
                effects: Vec::new(),
                decision: changed,
                raw: Map::new(),
                action: FALLBACK.to_string(),
                target: requested.to_string(),
                command: false,
                fallback_used: true,
                error: decision.error,
            };
        }
        let agents = around.agents;
        let settings = &routing.settings;
        let listed = routing.packet["agents"].as_array();
        let shown: Vec<&str> = listed
            .map(|agents| {
                agents
                    .iter()
                    .filter_map(|agent| agent["session"].as_str())
                    .collect()
            })
            .unwrap_or_default();
        let mut decision = decision;
        let mut effects = Vec::new();
        let mut target = match decision.target_session.as_str() {
            "" => requested.to_string(),
            named => named.to_string(),
        };
        let error = decision.error.clone();
        // What became of the message. Nothing yet: each rule below takes it only while
        // none before it has.
        let mut action: Option<&str> = None;
        let sure = |decision: &Decision| decision.confidence >= settings.confidence_threshold;
        let asked = python::strip(&decision.control_action).to_lowercase();
        // Voice is answered aloud; an app's request never is, whichever sentence it would be.
        let aloud = !routing.fallback_request;
        let command = [CONTROL, AGENT_CONTROL].contains(&decision.kind.as_str());
        if command {
            let (refusal, resolved) = command_check(
                &decision,
                &asked,
                sure(&decision),
                &shown,
                requested,
                agents,
            );
            // The row, the event and the decision carry the session the name resolved to, or
            // nothing; the name as it was said stays in the model's own answer.
            target.clone_from(&resolved);
            decision.target_session = resolved;
            // Refused in its sentence, and never held: a later "yes" must not give its words to
            // an agent as a message.
            if let Some(refusal) = refusal {
                decision.kind = CLARIFY.to_string();
                decision.phrase_key = refusal.key.to_string();
                action = Some(CLARIFY);
                if aloud {
                    let said = self.as_agent(requested, refusal.text, refusal.key, around);
                    effects.extend(said.map(Effect::Say));
                }
            }
        }
        let for_an_agent = [AGENT_MESSAGE, CORRECTION].contains(&decision.kind.as_str());
        if routing.fallback_request && !for_an_agent {
            // An app that could place a message by no name asks one thing: whom it is
            // for. Nothing else is made of its message.
            action = Some(CLARIFY);
        }
        if for_an_agent {
            match valid(agents, &shown, &decision.target_session).map(str::to_string) {
                // A question: whom its row names is said when it is kept (`finish`).
                None => {
                    action = Some(CLARIFY);
                    decision = as_question(decision, NO_VALID_TARGET.text);
                    if aloud {
                        self.hold(&decision, routing, hold_as, around, &mut effects);
                    }
                }
                Some(agent) if !sure(&decision) => {
                    action = Some(CLARIFY);
                    let words = format!("Was that for {}?", name_of(agents, &agent));
                    decision = as_question(decision, &words);
                    if aloud {
                        self.hold(&decision, routing, hold_as, around, &mut effects);
                    }
                }
                Some(agent) => {
                    let held = self.claim(&decision.pending_id, requested, now);
                    if held.is_none() && !decision.pending_id.is_empty() {
                        // What the model confirms is no longer held here (its time is up,
                        // it was given, or it was held while speaking to another agent):
                        // the confirmation is not given in its place.
                        action = Some(CLARIFY);
                        decision.kind = CLARIFY.to_string();
                        decision.phrase_key = HELD_MESSAGE_GONE.key.to_string();
                        let gone = HELD_MESSAGE_GONE;
                        if aloud {
                            let said = self.as_agent(requested, gone.text, gone.key, around);
                            effects.extend(said.map(Effect::Say));
                        }
                    } else {
                        let own = python::strip(&decision.text_to_send);
                        let text = held.as_ref().map(|held| held.utterance.as_str());
                        let text = text.filter(|text| !text.is_empty());
                        let text = text.or(Some(own).filter(|own| !own.is_empty()));
                        let said = routing.packet["utterance"].as_str().unwrap_or_default();
                        effects.push(Effect::Give(Give {
                            text: text.unwrap_or(said).to_string(),
                            session: agent.clone(),
                            requested_session: requested.to_string(),
                            trace_id: routing.trace_id.clone(),
                            came: match held {
                                Some(held) => held.came,
                                None => routing.came.clone(),
                            },
                        }));
                        action = Some(ROUTE);
                    }
                    target = agent;
                }
            }
        }
        let kind = decision.kind.clone();
        if action.is_none() && [CLARIFY, AMBIGUOUS].contains(&kind.as_str()) {
            action = Some(CLARIFY);
            if aloud {
                self.hold(&decision, routing, hold_as, around, &mut effects);
            }
        }
        let router_says = |text: String| {
            Effect::Say(Say {
                text,
                voice_id: settings.voice_id.clone(),
                session: None,
                phrase_key: String::new(),
            })
        };
        if action.is_none() && kind == STATUS {
            action = Some(TOLD_STATUS);
            let named = python::strip(&decision.target_session);
            let found = if named.is_empty() {
                Vec::new()
            } else {
                agents_named(agents, named)
            };
            let agent = match found.as_slice() {
                [one] => valid(agents, &shown, &one.session),
                _ => None,
            };
            let text = if named.is_empty() || agent.is_some() {
                target = agent.unwrap_or(requested).to_string();
                // The model's words, as they were read (without the blanks at their ends).
                match decision.status_text.as_str() {
                    "" => lookup(agents, &target).map_or(NOT_SEEN.to_string(), status_of),
                    own => own.to_string(),
                }
            } else {
                // Not another agent's status, nor one the model made up: no such agent,
                // two of that name, or one this conversation cannot answer about (put
                // away, outside the call, a janitor).
                action = Some(CLARIFY);
                let refusal = match found.len() {
                    0 => NO_SUCH_AGENT,
                    1 => CANNOT_CHECK,
                    _ => WHICH_AGENT,
                };
                refusal.text.to_string()
            };
            effects.push(router_says(text));
        }
        // The agent a command acts on: a command carried out names it by now, by the name it was
        // given or as the agent the message was said to (`command_check`), and is kept for it.
        let on = decision.target_session.clone();
        if action.is_none() && kind == CONTROL {
            action = Some(CONTROL);
            match asked.as_str() {
                "repeat" | "repeat_last" | "say_again" => {
                    let nothing = REPEAT_MISSING.text;
                    effects.push(Effect::Repeat {
                        session: on.clone(),
                        or: self.as_agent(requested, nothing, "repeat_missing", around),
                    });
                }
                "who_am_i_talking_to" | "current_agent" => {
                    let name = name_of(agents, requested);
                    effects.push(router_says(format!("You are talking to {name}.")));
                }
                _ => {
                    let words = CONTROL_UNKNOWN.text;
                    effects.push(router_says(words.to_string()));
                }
            }
        }
        if action.is_none() && kind == AGENT_CONTROL {
            action = Some(CONTROL);
            let agent = lookup(agents, &on);
            match asked.as_str() {
                "switch" | "switch_focus" => {
                    effects.extend(agent.map(|agent| Effect::Focus {
                        agent_id: agent.agent_id.clone(),
                        session: on.clone(),
                    }));
                }
                "stop" | "stop_agent" | "interrupt" => {
                    effects.extend(agent.map(|agent| Effect::Stop {
                        agent_id: agent.agent_id.clone(),
                    }));
                }
                "create" => {
                    let payload = decision.control_payload.as_ref();
                    let name = payload.and_then(|payload| payload.get("name"));
                    let name = name.and_then(Value::as_str).map(python::strip);
                    effects.push(Effect::Start(Start {
                        again: None,
                        name: name.filter(|name| !name.is_empty()).map(str::to_string),
                    }));
                }
                // Started again as the Host knows it. Its command named an agent there is, or
                // was said to one: no agent is made of a name alone.
                "relaunch" | "restart" | "resume" => {
                    effects.extend(agent.map(|agent| {
                        Effect::Start(Start {
                            again: Some(agent.session.clone()),
                            name: None,
                        })
                    }));
                }
                _ => {
                    let words = AGENT_CONTROL_UNKNOWN.text;
                    effects.extend(self.as_agent(requested, words, "", around).map(Effect::Say));
                }
            }
        }
        if action.is_none() && kind == IGNORED {
            action = Some(IGNORED);
            target = requested.to_string();
        }
        Plan {
            effects,
            fallback_used: action.is_none(),
            action: action.unwrap_or(FALLBACK).to_string(),
            decision,
            raw,
            target,
            command,
            error,
        }
    }

    /// Keeps what came of a decision, ends the run it was made in, and says what the
    /// message's sender is answered and the apps are told. `latency_ms`: how long the
    /// model took.
    pub fn finish(
        &mut self,
        routing: &Routing,
        plan: Plan,
        done: Done,
        latency_ms: i64,
        around: &mut Around<'_>,
    ) -> Outcome {
        let Plan {
            effects,
            mut decision,
            raw,
            mut action,
            mut target,
            command,
            fallback_used,
            mut error,
        } = plan;
        let now = around.now_ms;
        let (mut given, mut refused_with, mut say) = (None, 0, None);
        match done {
            Done::Fine => {}
            Done::Given { session, backend } => {
                let gave = effects.iter().find_map(|effect| match effect {
                    Effect::Give(give) => Some(give),
                    _ => None,
                });
                if let Some(give) = gave {
                    self.gave(&give.session, &give.text, &routing.trace_id, false, around);
                }
                given = Some((session, backend));
            }
            Done::NotGiven { status, error: why } => {
                (error, action, refused_with) = (why, ERROR.to_string(), status);
            }
            // What it acted on: a new agent's session, for one that was made.
            Done::Started { session } => {
                decision.target_session.clone_from(&session);
                target = session;
            }
            // Refused by the Host (a name or a session in use, ...): said, and kept as a
            // question, never as a silent error. Nothing was done.
            Done::Refused { code } => {
                (error, action) = (code, CLARIFY.to_string());
                let refused = COMMAND_REFUSED;
                decision.kind = CLARIFY.to_string();
                decision.spoken_text = refused.text.to_string();
                decision.phrase_key = refused.key.to_string();
                say = self.as_agent(
                    &routing.requested_session,
                    refused.text,
                    refused.key,
                    around,
                );
            }
            Done::Broke(why) => (error, action) = (why, ERROR.to_string()),
        }
        if action == CLARIFY {
            // A question delivered nothing. Its row and its event name the session the decision's
            // target resolves to, or none, and the decision keeps that as its suggestion.
            target = resolved(around.agents, &decision.target_session);
            decision.target_session.clone_from(&target);
        }
        let message = routing.message();
        let id = self.keep(
            &decision,
            &raw,
            &routing.packet,
            &routing.settings,
            &message,
            latency_ms,
            &target,
            &action,
            fallback_used,
            &error,
            now,
        );
        let told = json!({
            "type": "orchestrator-decision", "decision_id": id, "trace_id": routing.trace_id,
            "action": action, "kind": decision.kind, "target_session": target,
            "confidence": decision.confidence, "reason": decision.reason,
        });
        let failed = action == ERROR;
        let status = match (failed, refused_with) {
            (false, _) => 200,
            (true, 0) => 500,
            (true, status) => status,
        };
        // Nothing was delivered, or a command did nothing (refused, a question, or an error after
        // it was taken): the answer names the agent spoken to, so that a client that acts on this
        // session (a stop of a send it cancelled) never acts on another agent.
        let nothing_done = action == CLARIFY || (command && action != CONTROL);
        let session = if nothing_done {
            routing.requested_session.clone()
        } else {
            target.clone()
        };
        let mut outcome = Outcome {
            ok: !failed,
            session,
            dispatch: String::new(),
            trace_id: routing.trace_id.clone(),
            action,
            decision_id: Some(id),
            decision: Some(decision.told()),
            error,
            status,
            told: Some(told),
            say,
            target: Some(target),
        };
        if let Some((session, backend)) = given {
            outcome = Outcome {
                ok: true,
                session,
                dispatch: backend,
                error: String::new(),
                status: 200,
                target: None,
                ..outcome
            };
        }
        // The receipt of the run: what a message that is sent again is answered by.
        let recipient = outcome.target.as_deref().unwrap_or(&outcome.session);
        let recipient = lookup(around.agents, recipient);
        let recipient = recipient
            .map(|agent| agent.agent_id.as_str())
            .unwrap_or_default();
        let mut receipt = json!({
            "summary": format!("Message routing finished: {}", outcome.action),
            "status": outcome.action, "target_agent_id": recipient,
        });
        if failed && outcome.status != 500 {
            receipt["http_status"] = json!(outcome.status);
        }
        if failed && refused_with >= 500 {
            // The decision stands: sent again, the message is given again.
            receipt["reason"] = json!(RETRYABLE);
            receipt["decision_id"] = json!(id);
        }
        // What the first answer said, so that the message sent again is answered exactly this.
        // Its status and its tool always fit the history's bound; its session is kept only where
        // it fits too, never cut (a name cut short is another name). Only a session that was
        // asked for can be too long, and a status kept without a session means just that.
        let said = json!({ "answer_dispatch": outcome.dispatch, "answer_status": outcome.status });
        if demand::fits(&merged(&receipt, &said)) {
            receipt = merged(&receipt, &said);
            let session = json!({ "answer_session": outcome.session });
            if demand::fits(&merged(&receipt, &session)) {
                receipt = merged(&receipt, &session);
            }
        }
        if let (Some(id), Some(decision)) = (outcome.decision_id, &outcome.decision) {
            let replied = json!({
                "decision_id": id,
                "decision_target": decision["target_session"],
                "decision_action": decision["control_action"],
            });
            if demand::fits(&merged(&receipt, &replied)) {
                receipt = merged(&receipt, &replied);
            }
        }
        let answered = routing.decided.as_ref();
        let failed = failed || answered.is_some_and(|(decision, _)| decision.kind == ERROR);
        let (ended, why) = if failed {
            ("failed", FAILED)
        } else {
            ("completed", "")
        };
        let janitors = &mut *around.janitors;
        let run = &routing.run_id;
        let world = around.world;
        if janitors
            .complete_run(run, ended, Some(&receipt), why, None, world, now)
            .is_err()
        {
            // The receipt never costs the answer, and the run is never left claimed: it is closed
            // with its least receipt, which keeps what the first answer said where that fits.
            let least = least_receipt(&receipt, &outcome.action);
            let _ = janitors.complete_run(run, ended, Some(&least), why, None, world, now);
        }
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The least receipt, as the Python Host makes it where its first completion fails (its own
    /// tests: a refused completion keeps what the first answer said; with the session refused, only
    /// the session is left out; with everything refused, the action alone).
    #[test]
    fn the_least_receipt_keeps_what_the_first_answer_said_where_it_fits() {
        let receipt = json!({
            "summary": "Message routing finished: route", "status": "route",
            "target_agent_id": "id-antoni", "answer_dispatch": "claude", "answer_status": 200,
            "answer_session": "antoni",
        });
        let least = least_receipt(&receipt, "route");
        let kept = json!({
            "status": "route", "target_agent_id": "id-antoni", "answer_status": 200,
            "answer_dispatch": "claude", "answer_session": "antoni",
        });
        assert_eq!(least.to_string(), kept.to_string());
        // A status of failure and the decision id are kept beside it, but the retry flag is not.
        let failed = json!({
            "status": "error", "target_agent_id": "id-antoni", "http_status": 413,
            "reason": RETRYABLE, "decision_id": 7, "answer_status": 413,
            "decision_target": "antoni", "decision_action": "switch",
        });
        let least = least_receipt(&failed, "error");
        let kept = json!({
            "status": "error", "target_agent_id": "id-antoni", "http_status": 413,
            "answer_status": 413, "decision_id": 7,
            "decision_target": "antoni", "decision_action": "switch",
        });
        assert_eq!(least.to_string(), kept.to_string());
        // A session the history does not take is left out; the rest stays.
        let long =
            json!({ "status": "clarify", "answer_status": 200, "answer_session": "s".repeat(501) });
        let least = least_receipt(&long, "clarify");
        assert_eq!(least, json!({ "status": "clarify", "answer_status": 200 }));
        // Where even that does not fit, the action alone.
        let id =
            json!({ "status": "route", "target_agent_id": "i".repeat(501), "answer_status": 200 });
        assert_eq!(least_receipt(&id, "route"), json!({ "status": "route" }));
    }
}
