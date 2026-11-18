//! A call of the Oracle put through to an agent, and back: the Host's record of it.
//!
//! The user asks the Oracle for an agent, and the phone leaves the call of the Oracle for a
//! talk with that agent, hands free; later it comes back. The phone owns the sound and the
//! Host owns the record: a handoff is offered to the phone, which prepares it, activates it
//! and says when it is with the agent, each step acknowledged, and each with a time after
//! which the Host acts by itself. What the Host does to a call that is open is little, and
//! all of it is said here as something to tell that call: hold it, feed it again, a cue,
//! one neutral word of what became of the handoff.
//!
//! Nothing here reads what anybody said. A handoff begins only when somebody asks for one
//! (`POST /oracle/connect`, `clarp-admin oracle connect`).
//!
//! The Python Host's `server/lib/oracle_handoffs.py`, whose contract with the phone is
//! `docs/oracle-handoff.md`, held to it by `tests/fixtures/oracle-handoff-vectors.json`
//! (`server-rs/tools/oracle-handoff-vectors.py`). There the records are kept in the Host's
//! settings and read anew for every request, and timers of the process run the deadlines;
//! here they are one book a phone, and the Host is told which deadline is next.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use super::said;
use crate::python;

/// The states a handoff does not leave by itself, and no longer changes in (but for what
/// [`LATE`] names).
const OVER: [&str; 4] = ["failed", "broken", "cancelled", "ended"];
/// What a phone may say of how far it is.
const PHASES: [&str; 7] = [
    "preparing",
    "activating",
    "ready",
    "rolled_back",
    "rollback_failed",
    "failed",
    "abandon",
];
/// Why a phone may give a handoff up.
const GIVEN_UP_FOR: [&str; 3] = ["local_stop", "client_restarted", "local_intent_changed"];
/// What is over and may still be acknowledged late: a phone that was slower than the Host.
const LATE: [(&str, &str); 2] = [
    ("failed", "prepare_timeout"),
    ("broken", "activation_timeout"),
];
/// How many records that are over, and that nothing depends on, are kept for a phone.
pub const KEPT: usize = 20;
/// Who acts for whichever phone has the one call.
pub const ADMINISTRATOR: &str = "administrator";
/// How long whoever asked for a handoff is kept waiting for the phone: every step's time,
/// and a little more.
pub const CONNECT_WAITS_MS: i64 = 55_000;

/// How long a handoff may stay in a state before the Host acts by itself; nothing for a
/// state that has no such time.
pub fn waits_ms(state: &str) -> Option<i64> {
    match state {
        "offered" | "preparing" => Some(10_000),
        "activating" => Some(30_000),
        _ => None,
    }
}

fn over(state: &str) -> bool {
    OVER.contains(&state)
}

/// The agent a call is put through to.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Agent {
    pub session: String,
    pub agent_id: String,
    pub persona: String,
}

/// The call of the Oracle that is left, and the thread to come back to.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Left {
    pub voice_session_id: String,
    pub provider_session: String,
    pub thread_id: String,
}

/// One handoff, as it is kept. What begins with a line below is shown to nobody.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Record {
    pub handoff_id: String,
    /// Of a way back: the handoff it ends. Otherwise nothing.
    pub parent_handoff_id: String,
    pub host_id: String,
    pub principal: String,
    /// The number of this handoff among the phone's: each has a higher one than the last.
    pub generation: i64,
    /// Goes up by one with every change of this handoff.
    pub revision: i64,
    /// `oracle_to_agent` or `agent_to_oracle`.
    pub direction: String,
    pub mode: String,
    pub state: String,
    pub reason: String,
    /// What the call of the Oracle was when the handoff was over. While it is not over
    /// this is asked each time.
    pub oracle_call: String,
    pub agent: Agent,
    pub oracle: Left,
    pub issued_at: i64,
    /// When the Host acts by itself, in a state that has a time.
    #[serde(rename = "_deadline", default, skip_serializing_if = "Option::is_none")]
    pub deadline: Option<i64>,
    /// What the phone has said of it, each with the number it said it for.
    #[serde(rename = "_acks", default)]
    pub acks: Vec<(i64, String)>,
    /// What the phone does with the sound of the call of the Oracle, as far as it said.
    #[serde(
        rename = "_oracle_audio",
        default,
        skip_serializing_if = "Option::is_none"
    )]
    pub oracle_audio: Option<String>,
}

impl Record {
    fn outward(&self) -> bool {
        self.direction == "oracle_to_agent"
    }

    fn is(&self, state: &str, reason: &str) -> bool {
        self.state == state && self.reason == reason
    }

    /// A record the phone's talk still depends on, or that may still be acknowledged late.
    fn pinned(&self) -> bool {
        !over(&self.state) || LATE.contains(&(self.state.as_str(), self.reason.as_str()))
    }
}

/// What a Host keeps for one phone.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Book {
    /// How many handoffs were made for the phone, ever.
    pub generation: i64,
    pub records: Vec<Record>,
}

impl Book {
    fn find(&self, handoff_id: &str) -> Option<&Record> {
        let named = |record: &&Record| record.handoff_id == handoff_id;
        self.records.iter().find(named)
    }

    fn at(&self, handoff_id: &str) -> Option<usize> {
        let named = |record: &Record| record.handoff_id == handoff_id;
        self.records.iter().position(named)
    }

    /// The handoff the phone is with an agent by: the newest that is active. Only a handoff
    /// to an agent is ever active: a way back that is made is over.
    fn active(&self) -> Option<&Record> {
        let with_an_agent = |record: &&Record| record.state == "active";
        self.records.iter().rev().find(with_an_agent)
    }

    /// Lets go of what is over and nothing depends on, but for the newest [`KEPT`] of it.
    /// What a talk depends on is kept, and so is the handoff such a record is the way back
    /// of. The number of handoffs made stands beside the records and is never lowered.
    fn retain(&mut self) {
        let pinned = |record: &&Record| record.pinned();
        let mut keep: Vec<String> = self.records.iter().filter(pinned).map(named).collect();
        let parents: Vec<String> = self
            .records
            .iter()
            .filter(|record| keep.contains(&record.handoff_id))
            .filter(|record| !record.parent_handoff_id.is_empty())
            .map(|record| record.parent_handoff_id.clone())
            .collect();
        keep.extend(parents);
        let history: Vec<String> = self
            .records
            .iter()
            .filter(|record| !keep.contains(&record.handoff_id))
            .map(named)
            .collect();
        let newest = history.len().saturating_sub(KEPT);
        keep.extend_from_slice(&history[newest..]);
        self.records
            .retain(|record| keep.contains(&record.handoff_id));
    }
}

fn named(record: &Record) -> String {
    record.handoff_id.clone()
}

/// A call of the Oracle that is open, as far as a handoff looks at it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Call {
    pub principal: String,
    /// The Host's own name for this socket's call: what an offer names the call by.
    pub voice_session_id: String,
    pub provider_session: String,
    pub thread_id: String,
    /// Its phone asked for handoffs when it opened the call.
    pub capable: bool,
    /// The handoff this call is the way back of, when it is one.
    pub handoff_id: String,
    /// Held for a handoff: the Host does not feed it. Kept here as the call is told.
    pub held: bool,
}

impl Call {
    /// What a phone is told of its call the moment the call can be offered a handoff: before
    /// anything else, so that no offer names a call its phone does not know.
    fn identity(&self, host_id: &str, now: i64) -> Value {
        let back_of = match self.handoff_id.as_str() {
            "" => Value::Null,
            handoff => json!(handoff),
        };
        json!({
            "type": "oracle_v2.session", "host_id": host_id, "principal": self.principal,
            "voice_session_id": self.voice_session_id,
            "provider_session": self.provider_session, "thread_id": self.thread_id,
            "handoff_id": back_of, "server_now": now,
        })
    }
}

/// Something to tell a call that is open.
#[derive(Debug, Clone, PartialEq)]
pub enum ToCall {
    /// Its phone is told which call this is.
    Session(Value),
    /// A handoff to this agent is offered: the cue of it.
    Offer(String),
    /// What became of a handoff, for its phone to see on the call's own socket.
    Mirror(Value),
    Hold,
    Resume,
    /// A handoff to this agent failed: the cue, and with `note` one neutral word to the
    /// voice of what happened.
    Failed {
        persona: String,
        reason: String,
        note: bool,
    },
    /// The user is back from this agent.
    Returned(String),
}

/// What a Host does about a change of a handoff, in this order.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// To the call of this name (its `voice_session_id`).
    Call(String, ToCall),
    /// To the apps, on the stream of the Host's events.
    Apps(Value),
    /// The Host's focus follows the user to this agent.
    Focus(Agent),
}

/// A request the one who asked must hear about.
#[derive(Debug, Clone, PartialEq)]
pub struct Refused {
    pub status: u16,
    pub error: String,
    pub handoff: Option<Value>,
}

impl Refused {
    fn plainly(status: u16, error: &str) -> Self {
        Self {
            status,
            error: error.to_string(),
            handoff: None,
        }
    }

    fn with(status: u16, error: &str, handoff: Value) -> Self {
        Self {
            status,
            error: error.to_string(),
            handoff: Some(handoff),
        }
    }

    pub fn body(&self) -> Value {
        let mut body = Map::new();
        body.insert("ok".to_string(), json!(false));
        body.insert("error".to_string(), json!(self.error));
        if let Some(handoff) = &self.handoff {
            body.insert("handoff".to_string(), handoff.clone());
        }
        Value::Object(body)
    }
}

/// The handoffs of a Host: a book for each phone, and the calls of the Oracle that are open.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Desk {
    pub host_id: String,
    pub books: BTreeMap<String, Book>,
    /// The open call of each phone. Not kept: a Host that starts again has none.
    pub calls: BTreeMap<String, Call>,
}

/// One phone's book while something is done to it, with the calls and what is to be told.
struct Work<'a> {
    host_id: &'a str,
    principal: &'a str,
    book: &'a mut Book,
    calls: &'a mut BTreeMap<String, Call>,
    now: i64,
    told: Vec<Effect>,
    /// The record that was changed last, as the change left it: also where the book let
    /// go of it at once, as something that is over and old.
    last: Option<Record>,
}

/// The call of the Oracle a handoff is about, as it stands: `running`, `closed`, or
/// `unknown` while its phone has its sound paused or the Host holds it.
fn oracle_call(calls: &BTreeMap<String, Call>, record: &Record) -> &'static str {
    let call = calls.get(&record.principal);
    if record.outward() {
        let Some(call) =
            call.filter(|call| call.voice_session_id == record.oracle.voice_session_id)
        else {
            return "closed";
        };
        let live = record.oracle_audio.as_deref().unwrap_or("live") == "live";
        return if live && !call.held {
            "running"
        } else {
            "unknown"
        };
    }
    match call.filter(|call| call.handoff_id == record.handoff_id) {
        Some(_) => "running",
        None => "closed",
    }
}

/// A record as it is shown: without what nobody is shown, with what the call of the Oracle
/// is now, the Host's clock, and how long the Host still waits.
fn shown(calls: &BTreeMap<String, Call>, record: &Record, now: i64) -> Value {
    let oracle_call = match over(&record.state) {
        true => record.oracle_call.as_str(),
        false => oracle_call(calls, record),
    };
    let deadline = record
        .deadline
        .filter(|_| waits_ms(&record.state).is_some());
    json!({
        "handoff_id": record.handoff_id, "parent_handoff_id": record.parent_handoff_id,
        "host_id": record.host_id, "principal": record.principal,
        "generation": record.generation, "revision": record.revision,
        "direction": record.direction, "mode": record.mode, "state": record.state,
        "reason": record.reason, "oracle_call": oracle_call, "agent": record.agent,
        "oracle": record.oracle, "issued_at": record.issued_at, "server_now": now,
        "ttl_ms": deadline.map(|deadline| (deadline - now).max(0)), "expires_at": deadline,
    })
}

impl Work<'_> {
    fn shown(&self, record: &Record) -> Value {
        shown(self.calls, record, self.now)
    }

    /// The open call a handoff acts on: the one it left, or the one that is its way back.
    fn call_of(&self, record: &Record) -> Option<&Call> {
        let call = self.calls.get(&record.principal)?;
        let its = if record.outward() {
            call.voice_session_id == record.oracle.voice_session_id
        } else {
            call.handoff_id == record.handoff_id
        };
        its.then_some(call)
    }

    fn tell(&mut self, call: &str, what: ToCall) {
        self.told.push(Effect::Call(call.to_string(), what));
    }

    /// Tells the apps what a handoff is now, and the call it left, when that is open.
    fn emit(&mut self, record: &Record) {
        let wire = self.shown(record);
        let mut event = Map::new();
        event.insert("type".to_string(), json!("oracle-handoff"));
        if let Value::Object(fields) = &wire {
            event.extend(fields.clone());
        }
        self.told.push(Effect::Apps(Value::Object(event)));
        if record.outward()
            && let Some(call) = self
                .call_of(record)
                .map(|call| call.voice_session_id.clone())
        {
            self.tell(&call, ToCall::Mirror(wire));
        }
    }

    /// One change of state: the revision, the time the Host waits, what is kept, and what
    /// is told. Says the record as it now is.
    fn set(&mut self, handoff_id: &str, state: &str, reason: &str) -> Option<Record> {
        let at = self.book.at(handoff_id)?;
        let now = self.now;
        let record = &mut self.book.records[at];
        record.state = state.to_string();
        record.reason = reason.to_string();
        record.revision += 1;
        record.deadline = waits_ms(state).map(|waits| now + waits);
        if over(state) {
            record.oracle_call = oracle_call(self.calls, record).to_string();
        }
        let record = record.clone();
        self.book.retain();
        self.emit(&record);
        self.last = Some(record.clone());
        Some(record)
    }

    /// The call a handoff left is fed again, where it is open.
    fn resume(&mut self, record: &Record) {
        if !record.outward() {
            return;
        }
        let Some(call) = self
            .call_of(record)
            .map(|call| call.voice_session_id.clone())
        else {
            return;
        };
        if let Some(call) = self.calls.get_mut(&record.principal) {
            call.held = false;
        }
        self.tell(&call, ToCall::Resume);
    }

    /// The call a handoff left hears that it failed, where it is open.
    fn failed(&mut self, record: &Record, note: bool) {
        if !record.outward() {
            return;
        }
        let Some(call) = self
            .call_of(record)
            .map(|call| call.voice_session_id.clone())
        else {
            return;
        };
        let persona = record.agent.persona.clone();
        let reason = record.reason.clone();
        let failed = ToCall::Failed {
            persona,
            reason,
            note,
        };
        self.tell(&call, failed);
    }

    /// What the phone does with the sound of the call it left, as it said.
    fn sound(&mut self, handoff_id: &str, what: &str) {
        if let Some(at) = self.book.at(handoff_id) {
            self.book.records[at].oracle_audio = Some(what.to_string());
        }
    }

    /// A new handoff, offered.
    fn new_offer(
        &mut self,
        id: &str,
        direction: &str,
        (agent, oracle): (Agent, Left),
        parent: &str,
    ) -> Record {
        self.book.generation += 1;
        let record = Record {
            handoff_id: id.to_string(),
            parent_handoff_id: parent.to_string(),
            host_id: self.host_id.to_string(),
            principal: self.principal.to_string(),
            generation: self.book.generation,
            revision: 1,
            direction: direction.to_string(),
            mode: "hands_free".to_string(),
            state: "offered".to_string(),
            reason: String::new(),
            oracle_call: String::new(),
            agent,
            oracle,
            issued_at: self.now,
            deadline: waits_ms("offered").map(|waits| self.now + waits),
            acks: Vec::new(),
            oracle_audio: None,
        };
        self.book.records.push(record.clone());
        self.book.retain();
        record
    }

    /// The handoff a way back ends is over, where it is still the one the phone is with an
    /// agent by. (A handoff to an agent names none, and so ends none.)
    fn end_parent(&mut self, way_back: &Record, reason: &str) {
        let parent = self.book.find(&way_back.parent_handoff_id);
        if parent.is_some_and(|parent| parent.state == "active") {
            self.set(&way_back.parent_handoff_id, "ended", reason);
        }
    }

    /// What a phone says of how far it is, taken where it is a step from where the handoff
    /// stands. Says whether it was one.
    fn apply(&mut self, handoff_id: &str, phase: &str, reason: &str) -> bool {
        let Some(record) = self.book.find(handoff_id).cloned() else {
            return false;
        };
        let (state, outward) = (record.state.as_str(), record.outward());
        let held_by = self
            .call_of(&record)
            .map(|call| call.voice_session_id.clone());
        match (phase, state) {
            ("preparing", "offered") => {
                if outward {
                    self.sound(handoff_id, "paused");
                    if let Some(call) = held_by {
                        if let Some(held) = self.calls.get_mut(&record.principal) {
                            held.held = true;
                        }
                        self.tell(&call, ToCall::Hold);
                    }
                }
                self.set(handoff_id, "preparing", "");
            }
            ("failed", "offered") => {
                let changed = !outward && reason == "local_intent_changed";
                let why = if changed {
                    "local_intent_changed"
                } else {
                    "client_failed"
                };
                let now = self.set(handoff_id, "failed", why);
                if changed {
                    self.end_parent(&record, "local_intent_changed");
                }
                if let Some(now) = now {
                    self.failed(&now, false);
                }
            }
            ("activating", "preparing") => {
                if outward {
                    self.sound(handoff_id, "released");
                }
                self.set(handoff_id, "activating", "");
            }
            ("rolled_back", "preparing" | "activating") => self.rolled_back(&record),
            ("rolled_back", "broken") if record.reason == "activation_timeout" => {
                self.rolled_back(&record);
            }
            ("ready", "activating") => self.ready(&record),
            ("ready", "broken") if record.reason == "activation_timeout" => self.ready(&record),
            ("rollback_failed", "activating") => {
                let now = self.set(handoff_id, "broken", "rollback_failed");
                self.end_parent(&record, "return_failed");
                if let Some(now) = now {
                    self.failed(&now, false);
                }
            }
            // The Host had given up on a phone that was preparing: the phone says it has
            // the call of the Oracle again. Nothing changes but what that call is.
            ("rolled_back", "failed") if record.reason == "prepare_timeout" => {
                if outward {
                    self.sound(handoff_id, "live");
                    self.resume(&record);
                }
                if let Some(at) = self.book.at(handoff_id) {
                    let call = oracle_call(self.calls, &self.book.records[at]);
                    let record = &mut self.book.records[at];
                    record.oracle_call = call.to_string();
                    record.revision += 1;
                    let record = record.clone();
                    self.book.retain();
                    self.emit(&record);
                    self.last = Some(record);
                }
            }
            ("abandon", _) if waits_ms(state).is_some() && GIVEN_UP_FOR.contains(&reason) => {
                self.set(handoff_id, "cancelled", reason);
                self.end_parent(&record, reason);
            }
            ("abandon", "active") if GIVEN_UP_FOR.contains(&reason) => {
                self.set(handoff_id, "ended", reason);
            }
            _ => return false,
        }
        true
    }

    /// The phone has the call of the Oracle again: the handoff failed, and the voice is
    /// told so in a word.
    fn rolled_back(&mut self, record: &Record) {
        if record.outward() {
            self.sound(&record.handoff_id, "live");
            self.resume(record);
        }
        if let Some(now) = self.set(&record.handoff_id, "failed", "rolled_back") {
            self.failed(&now, true);
        }
    }

    /// The phone is where the handoff put it: with the agent, or back with the Oracle.
    fn ready(&mut self, record: &Record) {
        if record.outward() {
            self.set(&record.handoff_id, "active", "");
            self.told.push(Effect::Focus(record.agent.clone()));
            return;
        }
        let back = self
            .call_of(record)
            .map(|call| call.voice_session_id.clone());
        self.set(&record.handoff_id, "ended", "returned");
        self.end_parent(record, "returned");
        if let Some(call) = back {
            self.tell(&call, ToCall::Returned(record.agent.persona.clone()));
        }
    }

    /// A deadline passed: the Host acts by itself, where the handoff still stands as it
    /// stood when the deadline was set.
    fn expire(&mut self, handoff_id: &str, revision: i64) {
        let Some(record) = self.book.find(handoff_id).cloned() else {
            return;
        };
        if record.revision != revision {
            return;
        }
        match record.state.as_str() {
            "offered" => {
                if let Some(now) = self.set(handoff_id, "failed", "no_ack") {
                    self.failed(&now, false);
                }
            }
            "preparing" => {
                // What the phone did with the sound is not known until it says: it said it
                // had paused it when it began to prepare, and that stands.
                self.resume(&record);
                if let Some(now) = self.set(handoff_id, "failed", "prepare_timeout") {
                    self.failed(&now, false);
                }
            }
            "activating" => {
                self.set(handoff_id, "broken", "activation_timeout");
            }
            _ => {}
        }
    }
}

/// Whether a handoff that is not the phone's newest may still be ended by its own number:
/// a way back that failed leaves the phone with the agent, and it must be able to end that.
fn finalizes_parent(book: &Book, record: &Record, phase: &str) -> bool {
    let active = book.active().map(|active| active.handoff_id.as_str());
    phase == "abandon" && active == Some(&record.handoff_id)
}

/// Whether this is the name of a handoff: `hof_` and thirty-two signs of a number in
/// sixteens, in small letters.
fn is_name(name: &str) -> bool {
    let sixteens = |sign: u8| sign.is_ascii_digit() || (b'a'..=b'f').contains(&sign);
    name.strip_prefix("hof_")
        .is_some_and(|rest| rest.len() == 32 && rest.bytes().all(sixteens))
}

impl Desk {
    fn work<'a>(&'a mut self, principal: &'a str, now: i64) -> Work<'a> {
        Work {
            host_id: &self.host_id,
            principal,
            book: self.books.entry(principal.to_string()).or_default(),
            calls: &mut self.calls,
            now,
            told: Vec::new(),
            last: None,
        }
    }

    /// A phone's book is kept once it has a record: asking about a phone keeps nothing.
    fn tidy(&mut self, principal: &str) {
        let empty = |book: &Book| book.generation == 0 && book.records.is_empty();
        if self.books.get(principal).is_some_and(empty) {
            self.books.remove(principal);
        }
    }

    /// A call of the Oracle was opened. Its phone is told which call this is, where it
    /// asked for handoffs; and what was offered to another call of the phone is over, as
    /// is a talk with an agent the phone came back from by itself.
    pub fn register(&mut self, call: Call, now: i64) -> Vec<Effect> {
        let principal = call.principal.clone();
        let host_id = self.host_id.clone();
        let mut told = Vec::new();
        if call.capable {
            let identity = call.identity(&host_id, now);
            let session = ToCall::Session(identity);
            told.push(Effect::Call(call.voice_session_id.clone(), session));
        }
        self.calls.insert(principal.clone(), call.clone());
        let mut work = self.work(&principal, now);
        let open: Vec<Record> = work.book.records.clone();
        for record in open.iter().filter(|record| !over(&record.state)) {
            // As it stands now: what was done for an earlier record may have ended it.
            let Some(record) = work.book.find(&record.handoff_id).cloned() else {
                continue;
            };
            let offered = matches!(record.state.as_str(), "offered" | "preparing");
            if over(&record.state) {
                continue;
            }
            if record.outward() {
                if offered && record.oracle.voice_session_id != call.voice_session_id {
                    work.set(&record.handoff_id, "cancelled", "superseded");
                } else if record.state == "active" && call.handoff_id.is_empty() {
                    work.set(&record.handoff_id, "ended", "client_returned");
                }
            } else if call.handoff_id != record.handoff_id && offered {
                // The talk with the agent it was the way back of is not ended here: where
                // the phone came back by itself that talk was ended a moment ago, in its
                // own turn, which comes before its way back's.
                work.set(&record.handoff_id, "cancelled", "superseded");
            }
        }
        told.append(&mut work.told);
        self.tidy(&principal);
        told
    }

    /// A call of the Oracle was closed: where it is still the phone's open call.
    pub fn unregister(&mut self, principal: &str, voice_session_id: &str) {
        let this = |call: &Call| call.voice_session_id == voice_session_id;
        if self.calls.get(principal).is_some_and(this) {
            self.calls.remove(principal);
        }
    }

    /// Whose call somebody acts on: a phone on its own; the administrator on the one it
    /// names, or on the one there is.
    fn whose(&self, caller: &str, named: Option<&str>, returning: bool) -> Result<String, Refused> {
        if caller != ADMINISTRATOR {
            return Ok(caller.to_string());
        }
        if let Some(named) = named.filter(|named| !named.is_empty()) {
            return Ok(named.to_string());
        }
        let found: Vec<&String> = if returning {
            let with_an_agent = |(_, book): &(&String, &Book)| book.active().is_some();
            let books = self.books.iter().filter(with_an_agent);
            books.map(|(principal, _)| principal).collect()
        } else {
            self.calls.keys().collect()
        };
        match found.as_slice() {
            [one] => Ok((*one).clone()),
            [] if returning => Err(Refused::plainly(404, "no active handoff")),
            [] => Err(Refused::plainly(404, "no live Oracle call")),
            _ => Err(Refused::plainly(409, "several live calls")),
        }
    }

    /// Offers a handoff: of the caller's call to the agent `target` names, or, where it
    /// names the Oracle, of the phone's talk with an agent back to the Oracle. `agent`
    /// says which agent a name means, or why none. `id` is the name the handoff gets.
    /// Says whose handoff it is and its name.
    pub fn offer(
        &mut self,
        (caller, named): (&str, Option<&str>),
        target: &Value,
        agent: impl FnOnce(&str) -> Result<Agent, String>,
        id: &str,
        now: i64,
    ) -> (Result<(String, String), Refused>, Vec<Effect>) {
        let wanted = python::strip(&said(target)).to_string();
        if wanted.is_empty() {
            return (Err(Refused::plainly(400, "agent required")), Vec::new());
        }
        let returning = python::casefold(&wanted) == "oracle";
        let who = match self.whose(caller, named, returning) {
            Ok(who) => who,
            Err(refused) => return (Err(refused), Vec::new()),
        };
        let mut work = self.work(&who, now);
        let offered = Self::offered(&mut work, &wanted, returning, agent, id);
        let told = std::mem::take(&mut work.told);
        self.tidy(&who);
        (offered.map(|()| (who, id.to_string())), told)
    }

    fn offered(
        work: &mut Work<'_>,
        wanted: &str,
        returning: bool,
        agent: impl FnOnce(&str) -> Result<Agent, String>,
        id: &str,
    ) -> Result<(), Refused> {
        let in_progress = || Refused::plainly(409, "handoff in progress");
        let activating = |record: &Record| record.state == "activating";
        if work.book.records.iter().any(activating) {
            return Err(in_progress());
        }
        if returning {
            let Some(parent) = work.book.active().cloned() else {
                return Err(Refused::plainly(404, "no active handoff"));
            };
            let on_its_way = |record: &Record| {
                waits_ms(&record.state).is_some() && record.parent_handoff_id == parent.handoff_id
            };
            if work.book.records.iter().any(on_its_way) {
                return Err(in_progress());
            }
            let whom = (parent.agent.clone(), parent.oracle.clone());
            let record = work.new_offer(id, "agent_to_oracle", whom, &parent.handoff_id);
            work.emit(&record);
            return Ok(());
        }
        let agent = agent(wanted).map_err(|why| {
            let status = if why == "unknown agent" { 404 } else { 409 };
            Refused::plainly(status, &why)
        })?;
        let Some(call) = work.calls.get(work.principal).cloned() else {
            return Err(Refused::plainly(404, "no live Oracle call"));
        };
        if !call.capable {
            return Err(Refused::plainly(409, "handoff_unsupported"));
        }
        // What was offered before and not yet activated is over.
        let older: Vec<String> = work.book.records.iter().map(named).collect();
        for older in older {
            let offered =
                |record: &Record| matches!(record.state.as_str(), "offered" | "preparing");
            if work.book.find(&older).is_some_and(offered) {
                work.set(&older, "cancelled", "superseded");
            }
        }
        let oracle = Left {
            voice_session_id: call.voice_session_id.clone(),
            provider_session: call.provider_session,
            thread_id: call.thread_id,
        };
        let record = work.new_offer(id, "oracle_to_agent", (agent, oracle), "");
        work.tell(
            &call.voice_session_id,
            ToCall::Offer(record.agent.persona.clone()),
        );
        work.emit(&record);
        Ok(())
    }

    /// Whether whoever asked for a handoff need wait no longer: it is over, or, where it
    /// was to an agent, the phone is with the agent.
    pub fn settled(&self, who: &str, handoff_id: &str) -> bool {
        let record = self.books.get(who).and_then(|book| book.find(handoff_id));
        record.is_none_or(|record| {
            over(&record.state) || (record.state == "active" && record.outward())
        })
    }

    /// What whoever asked for a handoff is answered: `waited` where they were kept waiting
    /// for the phone. `None`: there is no such handoff.
    pub fn outcome(
        &self,
        who: &str,
        handoff_id: &str,
        waited: bool,
        now: i64,
    ) -> Option<Result<Value, Refused>> {
        let record = self.books.get(who)?.find(handoff_id)?;
        let wire = shown(&self.calls, record, now);
        let (state, reason) = (record.state.as_str(), record.reason.as_str());
        let done = state == "active" || record.is("ended", "returned");
        Some(if done || (!waited && state == "offered") {
            Ok(json!({
                "ok": true, "handoff_id": record.handoff_id, "state": state, "handoff": wire,
            }))
        } else if state == "broken" {
            let why = "the handoff broke and the call could not be resumed";
            Err(Refused::with(409, why, wire))
        } else if over(state) {
            let why = format!("the handoff did not happen ({state}: {reason})");
            Err(Refused::with(409, &why, wire))
        } else {
            Err(Refused::with(
                504,
                &format!("the handoff is still {state}"),
                wire,
            ))
        })
    }

    /// A phone says how far it is with a handoff. The same said again is answered as it
    /// was, and changes nothing.
    pub fn ack(
        &mut self,
        caller: &str,
        body: &Value,
        now: i64,
    ) -> (Result<Value, Refused>, Vec<Effect>) {
        let handoff_id = said(&body["handoff_id"]);
        let (phase, reason) = (said(&body["phase"]), said(&body["reason"]));
        let generation = body["generation"]
            .as_i64()
            .filter(|_| !body["generation"].is_f64());
        let (true, true, Some(generation)) = (
            is_name(&handoff_id),
            PHASES.contains(&phase.as_str()),
            generation,
        ) else {
            let why = "handoff_id, generation and a known phase are required";
            return (Err(Refused::plainly(400, why)), Vec::new());
        };
        let has = |book: &Book| book.find(&handoff_id).is_some();
        let principal = if caller == ADMINISTRATOR {
            let found = self.books.iter().find(|(_, book)| has(book));
            found.map(|(principal, _)| principal.clone())
        } else {
            Some(caller.to_string()).filter(|caller| self.books.get(caller).is_some_and(has))
        };
        let Some(principal) = principal else {
            return (Err(Refused::plainly(404, "unknown_handoff")), Vec::new());
        };
        let mut work = self.work(&principal, now);
        let Some(record) = work.book.find(&handoff_id).cloned() else {
            return (Err(Refused::plainly(404, "unknown_handoff")), Vec::new());
        };
        let said_before = (generation, phase.clone());
        if record.acks.contains(&said_before) {
            return (Ok(json!({ "handoff": work.shown(&record) })), Vec::new());
        }
        let older = record.generation < work.book.generation;
        if generation != record.generation
            || (older && !finalizes_parent(work.book, &record, &phase))
        {
            let stale = Refused::with(409, "stale_generation", work.shown(&record));
            return (Err(stale), Vec::new());
        }
        if !work.apply(&handoff_id, &phase, &reason) {
            let illegal = Refused::with(409, "illegal_transition", work.shown(&record));
            return (Err(illegal), work.told);
        }
        if let Some(at) = work.book.at(&handoff_id) {
            work.book.records[at].acks.push(said_before);
        }
        work.book.retain();
        // As the step left it: also where the book let go of it at once.
        let left = work
            .last
            .take()
            .filter(|last| last.handoff_id == handoff_id);
        let after = work.book.find(&handoff_id).cloned().or(left);
        let answer = json!({ "handoff": after.map(|record| work.shown(&record)) });
        (Ok(answer), work.told)
    }

    /// What a phone is shown of its handoffs: the newest, and the one it is with an agent
    /// by.
    pub fn snapshot(&self, principal: &str, now: i64) -> Value {
        let book = self.books.get(principal);
        let newest = book.and_then(|book| book.records.last());
        let active = book.and_then(Book::active);
        let shown = |record: Option<&Record>| record.map(|record| shown(&self.calls, record, now));
        json!({
            "principal": principal, "host_id": self.host_id, "server_now": now,
            "handoff": shown(newest), "active": shown(active),
        })
    }

    /// The deadlines that stand: for each handoff in a state that has a time, its name,
    /// the moment, and the revision it stands for; the earliest first.
    pub fn deadlines(&self) -> Vec<(String, String, i64, i64)> {
        let mut stand = Vec::new();
        for (principal, book) in &self.books {
            for record in &book.records {
                if let Some(deadline) = record
                    .deadline
                    .filter(|_| waits_ms(&record.state).is_some())
                {
                    let name = record.handoff_id.clone();
                    stand.push((principal.clone(), name, deadline, record.revision));
                }
            }
        }
        stand.sort_by_key(|(_, _, deadline, _)| *deadline);
        stand
    }

    /// A deadline passed.
    pub fn expire(
        &mut self,
        principal: &str,
        handoff_id: &str,
        revision: i64,
        now: i64,
    ) -> Vec<Effect> {
        let mut work = self.work(principal, now);
        work.expire(handoff_id, revision);
        let told = std::mem::take(&mut work.told);
        self.tidy(principal);
        told
    }

    /// The Host started again: no call is open, and for a handoff that was in the middle
    /// of a step the time of that step runs anew.
    pub fn recovered(&mut self, now: i64) {
        self.calls.clear();
        for book in self.books.values_mut() {
            for record in &mut book.records {
                if let Some(waits) = waits_ms(&record.state) {
                    record.deadline = Some(now + waits);
                }
            }
            book.retain();
        }
    }
}
