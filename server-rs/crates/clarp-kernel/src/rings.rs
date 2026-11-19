//! Rings: an agent calls its owner.
//!
//! An agent that needs its user now, by voice, asks the Host to ring the owner's phone with
//! a reason and the line it will open with. The phone shows an incoming call. Answering
//! starts an ordinary call with that agent on the floor ([`crate::calls`]), and the opening
//! line, already voiced in the agent's own voice, is heard first.
//!
//! A ring is a request; answering is the user's. The Host records the ring, says who may
//! ring and how often, and ends every ring one way: answered, declined, cancelled by the
//! agent, or missed when nobody took it in time. Every request, refused or not, and every
//! outcome is written to a trail the owner can read.
//!
//! This is the Python Host's contract (`docs/agent-rings.md`, `server/lib/agent_rings.py`)
//! as rules without a store, a clock, a voice or a phone: a request is given the moment and
//! the Host's agents, and says what it answers, what the apps are told, what phones are
//! pushed and what is noted in the agent's conversation. A ring asks for one of two kinds of
//! conversation, and is placed and answered as the kind it is ([`Rings::admit`],
//! [`Rings::answer`]): the ordinary one here, and the one held as a live voice call in
//! [`live`]. A ring that listens first is another contract this Host does not have: asked
//! for, it is refused in words that tell the caller how to ring instead.
//!
//! Held to the Python Host's own module by `tests/fixtures/rings-vectors.json` and
//! `tests/fixtures/live-ring-vectors.json`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::calls::{self, Asker, Calls, Contact, Refused, refused, refused_with};
use crate::python;

mod live;
pub mod opening;

pub use live::{
    Admission, Android, Claim, Given, JOIN_MS, Joined, Live, Minted, Profiles, Sent, World,
    alert_follows, claimed,
};

/// How long a ring rings before it is missed.
pub const RING_MS: i64 = 45 * 1000;
/// How long an agent waits between its rings.
pub const BETWEEN_MS: i64 = 60 * 1000;
const HOUR_MS: i64 = 60 * 60 * 1000;
/// How many rings a Host places in an hour.
pub const HOURLY_MOST: usize = 10;
/// How many rings are kept, and how many lines of the trail.
pub const KEPT: usize = 50;
pub const AUDIT_KEPT: usize = 200;
/// How many lines of the trail the owner is shown, the newest first.
pub const AUDIT_SHOWN: usize = 50;
/// How long a reason and an opening line may be.
pub const REASON_MOST: usize = 200;
pub const OPENING_MOST: usize = 600;

const RINGING: &str = "ringing";
const ANSWERED: &str = "answered";
const DECLINED: &str = "declined";
const MISSED: &str = "missed";
const CANCELLED: &str = "cancelled";

/// The ways a conversation of a ring can be held.
const CLASSIC: &str = "classic";
const LIVE: &str = "agent_live_v1";
/// The ways a ring can open. This Host has the first.
const SAY: &str = "say";
const LISTEN_FIRST: &str = "listen_first";

/// One ring, as it is kept and as an app reads it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Ring {
    pub ring_id: String,
    pub session: String,
    pub persona: String,
    pub agent_id: String,
    /// Why the agent calls: what the incoming call shows.
    pub reason: String,
    /// What the agent says first when the user answers.
    pub opening_line: String,
    /// Where that line is, voiced; empty when it could not be voiced: the phone shows it.
    #[serde(default)]
    pub opening_audio_url: String,
    /// `ringing`, `answered`, `declined`, `missed` or `cancelled`.
    pub state: String,
    pub created_at: i64,
    pub expires_at: i64,
    #[serde(default)]
    pub ended_at: Option<i64>,
    /// The phone that answered.
    #[serde(default)]
    pub answered_by: String,
    /// The call the answer started.
    #[serde(default)]
    pub call_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub opening_mode: String,
    #[serde(default, rename = "_host_gen", skip_serializing_if = "Value::is_null")]
    pub generation: Value,
    #[serde(default, rename = "_opening", skip_serializing_if = "Value::is_null")]
    pub opening: Value,
    /// How its conversation is held when not as a call: `agent_live_v1`, live with a voice.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub conversation_mode: String,
    /// What a ring held as a live voice call keeps of it. Kept, and never shown.
    #[serde(default, rename = "_live", skip_serializing_if = "Option::is_none")]
    pub live: Option<Box<Live>>,
}

impl Ring {
    fn rings(&self) -> bool {
        self.state == RINGING
    }

    /// A ring nobody took can be held as no live call any more: what its voice would have
    /// been told is let go.
    fn unheld(&mut self) {
        if let Some(live) = self.live.as_mut() {
            live.shed();
        }
    }

    /// A call the user started with the agent: this phone's own, and no ring of the agent's.
    pub fn of_the_user(&self) -> Option<&str> {
        let live = self.live.as_ref()?;
        (!live.user_call.is_empty()).then_some(live.user_call.as_str())
    }

    /// As an app and an agent's tool read it.
    pub fn told(&self, now_ms: i64) -> Value {
        let mut told = serde_json::to_value(self).unwrap_or_else(|_| json!({}));
        if let Some(fields) = told.as_object_mut() {
            fields.shift_remove("_live");
            fields.shift_remove("_host_gen");
            fields.shift_remove("_opening");
            if !self.opening.is_null() {
                fields.insert("opening".into(), opening::public(&self.opening, false));
            }
            if self.of_the_user().is_some() {
                fields.insert("origin".to_string(), json!("user_call"));
            }
        }
        told["server_now"] = json!(now_ms);
        told
    }

    /// What the apps are told when it changes.
    fn event(&self, now_ms: i64) -> Value {
        let mut event = json!({
            "type": "agent-ring", "ring_id": self.ring_id, "session": self.session,
            "agent_id": self.agent_id, "persona": self.persona, "reason": self.reason,
            "opening_line": self.opening_line, "opening_audio_url": self.opening_audio_url,
            "state": self.state, "created_at": self.created_at, "expires_at": self.expires_at,
            "ended_at": self.ended_at, "answered_by": self.answered_by, "call_id": self.call_id,
            "server_now": now_ms,
        });
        if !self.opening_mode.is_empty() {
            event["opening_mode"] = json!(self.opening_mode);
        }
        if !self.opening.is_null() {
            event["opening"] = opening::public(&self.opening, false);
        }
        if !self.conversation_mode.is_empty() {
            event["conversation_mode"] = json!(self.conversation_mode);
        }
        event
    }
}

/// An agent as the rules of ringing see it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Caller {
    pub session: String,
    pub persona: String,
    pub agent_id: String,
    /// A muted agent does not ring.
    pub muted: bool,
    /// An agent that is a contact: not archived, and not one that only tidies up.
    pub can_call: bool,
}

/// The agents of the Host: those a name may mean, and any of them by its id.
#[derive(Clone, Copy)]
pub struct People<'a> {
    pub roster: &'a [Contact],
    pub known: &'a dyn Fn(&str) -> Option<Caller>,
}

/// A request to ring.
#[derive(Debug, Clone, Copy)]
pub struct Asked<'a> {
    /// The agent whose turn asks, proven by the turn: it rings as itself only. Empty: the
    /// owner's own tools, which name the agent.
    pub turn_agent_id: &'a str,
    /// The agent the request names.
    pub agent: &'a str,
    pub reason: &'a str,
    pub opening_line: &'a str,
    /// How the conversation is to be held; null: as a call.
    pub conversation_mode: &'a Value,
    /// What a live voice call would be told. A call takes none.
    pub event_context: &'a Value,
    /// How the ring opens, when the request says.
    pub opening_mode: Option<&'a Value>,
}

/// A ring that may be placed: who rings, and what the incoming call says.
#[derive(Debug, Clone, PartialEq)]
pub struct Admitted {
    generation: Value,
    caller: Caller,
    reason: String,
    opening: String,
    at: i64,
    /// What a ring held as a live voice call keeps of it: its voice is told its opening,
    /// and no line is voiced before.
    live: Option<Box<Live>>,
}

impl Admitted {
    /// The line the agent opens with: what is voiced before the ring is placed.
    pub fn opening(&self) -> &str {
        &self.opening
    }

    pub fn caller(&self) -> &Caller {
        &self.caller
    }

    /// Whether the ring is held as a live voice call: its opening is the voice's own, and
    /// nothing is voiced for it before.
    pub fn is_live(&self) -> bool {
        self.live.is_some()
    }
}

/// An answer to a ring.
#[derive(Debug, Clone, Copy)]
pub struct Answer<'a> {
    /// The phone that answers, or the administrator.
    pub principal: &'a str,
    pub ring_id: &'a str,
    pub conversation_mode: &'a Value,
    pub opening_mode: Option<&'a Value>,
    /// What a phone names its answer by, where it asks to hold the ring as a live voice
    /// call.
    pub request_id: &'a Value,
    pub client_nonce: &'a Value,
}

/// A notice for the conversation of the agent that rang: quiet, never spoken.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    pub agent_id: String,
    /// One for each ring and outcome: the same notice is written once.
    pub id: String,
    pub text: String,
}

/// What the owner's phones are pushed: a ring as it is now, and what happened to it
/// (`ring`, `answered`, `declined`, `missed`, `cancelled`).
#[derive(Debug, Clone, PartialEq)]
pub struct Pushed {
    pub ring: Ring,
    pub kind: &'static str,
}

/// Which of what is kept changed, and is to be kept again.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Changed {
    pub rings: bool,
    pub audit: bool,
    pub settings: bool,
    pub voip: bool,
    /// The phones that switched rings off for themselves.
    pub prefs: bool,
}

/// What a request did beside its answer.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Effects {
    /// What the apps are told, in order: one `agent-ring` event for each change.
    pub events: Vec<Value>,
    pub notices: Vec<Notice>,
    pub pushes: Vec<Pushed>,
    /// The lines this request wrote to the trail.
    pub audited: Vec<Value>,
    pub changed: Changed,
}

/// The rings of a Host, the owner's switches for them, the trail, and the tokens phones
/// left for calls.
#[derive(Debug, Clone, PartialEq)]
pub struct Rings {
    // Only the archived openings requested by this transaction. Never
    // included in public ring history, rate limits or the next transaction.
    archived: Vec<Ring>,
    rings: Vec<Ring>,
    audit: Vec<Value>,
    enabled: bool,
    show_reason: bool,
    voip: Vec<Value>,
    /// The phones that switched rings off for themselves, each with when.
    prefs: Vec<Value>,
}

impl Default for Rings {
    fn default() -> Self {
        Self {
            archived: Vec::new(),
            rings: Vec::new(),
            audit: Vec::new(),
            // Rings are on until the owner turns them off; why an agent calls is not
            // shown on a locked phone until the owner says so.
            enabled: true,
            show_reason: false,
            voip: Vec::new(),
            prefs: Vec::new(),
        }
    }
}

/// A text on one line, its blanks single, and no longer than `most` signs.
fn one_line(text: &str, most: usize) -> String {
    let words: Vec<&str> = text
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .collect();
    words.join(" ").chars().take(most).collect()
}

fn over(ring: &Ring, now_ms: i64) -> Refused {
    refused_with(
        409,
        "the call is over",
        json!({ "ring": ring.told(now_ms) }),
    )
}

/// What the agent is told of the outcome of its ring.
fn notice_of(kind: &str, reason: &str) -> Option<String> {
    let said = match kind {
        ANSWERED => "The user answered your call about",
        DECLINED => "The user declined your call about",
        MISSED => "The user did not answer your call about",
        CANCELLED => "You cancelled your call about",
        _ => return None,
    };
    Some(format!("Call: {said}: {reason}"))
}

/// Whether a request asks for a live voice call. What is no way to hold a conversation is
/// refused.
fn live_asked(conversation_mode: &Value) -> Result<bool, Refused> {
    match conversation_mode {
        Value::Null => Ok(false),
        Value::String(mode) if mode == CLASSIC => Ok(false),
        Value::String(mode) if mode == LIVE => Ok(true),
        _ => Err(refused(400, "live_mode_invalid")),
    }
}

/// Whether a request names the live voice call as the way to hold its conversation.
fn names_live(conversation_mode: &Value) -> bool {
    conversation_mode.as_str() == Some(LIVE)
}

/// The ways a phone says it can hold a ring's conversation (`supported_conversation_modes`,
/// with an iPhone's call token and with an Android phone's push token): nothing said, or a
/// list of the ways there are, each counted once. Anything else is refused.
pub fn declared_modes(said: &Value) -> Result<Vec<&'static str>, Refused> {
    let invalid = || refused(400, "live_mode_invalid");
    let listed = match said {
        Value::Null => return Ok(Vec::new()),
        Value::Array(listed) if listed.len() <= 2 => listed,
        _ => return Err(invalid()),
    };
    let mut known = Vec::new();
    for mode in listed {
        let mode = match mode.as_str() {
            Some(CLASSIC) => CLASSIC,
            Some(LIVE) => LIVE,
            _ => return Err(invalid()),
        };
        if !known.contains(&mode) {
            known.push(mode);
        }
    }
    Ok(known)
}

impl Rings {
    /// From what is kept. `settings` is what [`Rings::settings`] gave.
    pub fn kept(rings: Vec<Ring>, audit: Vec<Value>, settings: &Value, voip: Vec<Value>) -> Self {
        Self {
            archived: Vec::new(),
            rings,
            audit,
            enabled: settings["enabled"].as_bool().unwrap_or(true),
            show_reason: settings["show_reason"].as_bool().unwrap_or(false),
            voip,
            prefs: Vec::new(),
        }
    }

    /// The rings, to be kept.
    pub fn rings(&self) -> &[Ring] {
        &self.rings
    }

    /// The trail, the oldest line first, to be kept.
    pub fn audit(&self) -> &[Value] {
        &self.audit
    }

    /// The tokens phones left for calls, to be kept.
    pub fn voip(&self) -> &[Value] {
        &self.voip
    }

    /// The owner's switches: whether agents may ring at all, and whether a locked phone
    /// shows why.
    pub fn settings(&self) -> Value {
        json!({ "enabled": self.enabled, "show_reason": self.show_reason })
    }

    /// Whether a locked phone shows why an agent calls.
    pub fn shows_reason(&self) -> bool {
        self.show_reason
    }

    /// The ring that rings now, when one does and it is not overdue: what a timer is set by.
    pub fn ringing(&self) -> Option<&Ring> {
        self.rings.iter().rev().find(|ring| ring.rings())
    }

    fn write(&mut self, entry: Value, now_ms: i64, out: &mut Effects) {
        let mut line = Map::new();
        line.insert("at".to_string(), json!(now_ms));
        if let Value::Object(entry) = entry {
            line.extend(entry);
        }
        let line = Value::Object(line);
        self.audit.push(line.clone());
        if self.audit.len() > AUDIT_KEPT {
            let dropped = self.audit.len() - AUDIT_KEPT;
            self.audit.drain(..dropped);
        }
        out.audited.push(line);
        out.changed.audit = true;
    }

    /// The event, the push and the agent's notice for one change of a ring.
    fn announce(ring: &Ring, kind: &'static str, now_ms: i64, out: &mut Effects) {
        out.events.push(ring.event(now_ms));
        out.pushes.push(Pushed {
            ring: ring.clone(),
            kind,
        });
        if let Some(text) = notice_of(kind, &ring.reason) {
            out.notices.push(Notice {
                agent_id: ring.agent_id.clone(),
                id: format!("{}:{}", ring.ring_id, ring.state),
                text,
            });
        }
    }

    /// Every ring that is overdue is missed from now on. Answers which became so.
    fn settle(&mut self, now_ms: i64, out: &mut Effects) -> Vec<String> {
        self.settle_live(now_ms, out);
        let mut missed = Vec::new();
        for place in 0..self.rings.len() {
            if !self.rings[place].rings() || now_ms < self.rings[place].expires_at {
                continue;
            }
            self.rings[place].state = MISSED.to_string();
            self.rings[place].ended_at = Some(now_ms);
            self.rings[place].unheld();
            let ring = self.rings[place].clone();
            out.changed.rings = true;
            let entry = json!({
                "outcome": MISSED, "ring_id": ring.ring_id, "session": ring.session, "by": "timeout",
            });
            self.write(entry, now_ms, out);
            missed.push(ring);
        }
        for ring in &missed {
            Self::announce(ring, MISSED, now_ms, out);
        }
        missed.into_iter().map(|ring| ring.ring_id).collect()
    }

    /// The timer of a ring went: what is overdue is missed. Answers the rings that became so.
    pub fn timer(&mut self, now_ms: i64, out: &mut Effects) -> Vec<String> {
        self.settle(now_ms, out)
    }

    fn place_of(&self, ring_id: &str) -> Result<usize, Refused> {
        let found = self.rings.iter().position(|ring| ring.ring_id == ring_id);
        found.ok_or_else(|| refused(404, "unknown call"))
    }

    /// The agent that calls. One proven by its turn rings as itself only.
    fn caller(asked: &Asked, people: People) -> Result<Caller, Refused> {
        let named = python::strip(asked.agent);
        if !asked.turn_agent_id.is_empty() {
            let me = (people.known)(asked.turn_agent_id);
            let me = me.ok_or_else(|| refused(404, "unknown agent"))?;
            if !named.is_empty() && calls::named(named, people.roster, &[])?.agent_id != me.agent_id
            {
                return Err(refused(403, "an agent can only ring as itself"));
            }
            return Ok(me);
        }
        if named.is_empty() {
            return Err(refused(400, "agent required"));
        }
        let contact = calls::named(named, people.roster, &[])?;
        Ok((people.known)(&contact.agent_id).unwrap_or_else(|| Caller {
            session: contact.session.clone(),
            persona: contact.persona.clone(),
            agent_id: contact.agent_id.clone(),
            muted: false,
            can_call: true,
        }))
    }

    /// Whether this agent may ring now.
    fn may_ring(&self, caller: &Caller, now_ms: i64) -> Result<(), Refused> {
        if !self.enabled {
            return Err(refused(403, "agent calls are turned off"));
        }
        if !caller.can_call {
            return Err(refused(403, "this agent cannot call"));
        }
        if caller.muted {
            return Err(refused(403, "this agent is muted"));
        }
        if self.rings.iter().any(Ring::rings) {
            return Err(refused(409, "the user is already being called"));
        }
        let mut own = self.rings.iter().rev();
        let own_ring =
            |ring: &&Ring| ring.agent_id == caller.agent_id && ring.of_the_user().is_none();
        if let Some(last) = own.find(own_ring)
            && now_ms - last.created_at < BETWEEN_MS
        {
            let wait = (BETWEEN_MS - (now_ms - last.created_at) + 999) / 1000;
            return Err(refused_with(
                429,
                "wait before calling again",
                json!({ "retry_after_s": wait }),
            ));
        }
        let this_hour = self.rings.iter();
        if this_hour
            .filter(|ring| ring.of_the_user().is_none() && now_ms - ring.created_at < HOUR_MS)
            .count()
            >= HOURLY_MOST
        {
            return Err(refused(429, "too many calls this hour"));
        }
        Ok(())
    }

    /// `POST /calls/ring`, its first half: whether this ring may be placed, as the kind of
    /// ring it asks to be. What is refused is written to the trail with the reason it gave.
    /// An admitted ring is placed ([`Rings::place`]), an ordinary one once its line is
    /// voiced: no other ring is admitted in between. One that asks to be held as a live
    /// voice call is admitted as that or not at all, never as an ordinary ring in its place.
    pub fn admit(
        &mut self,
        asked: &Asked,
        people: People,
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Admission, Refused> {
        if names_live(asked.conversation_mode) {
            return self.admit_live(asked, people, world, now_ms, out);
        }
        let admitted = self.admit_ordinary(asked, people, world.opening, now_ms, out);
        admitted.map(Admission::Place)
    }

    /// The ring whose opening line is said, and whose answer starts an ordinary call.
    fn admit_ordinary(
        &mut self,
        asked: &Asked,
        people: People,
        proof: Option<&dyn opening::Proof>,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Admitted, Refused> {
        let reason = one_line(asked.reason, REASON_MOST);
        let opening = one_line(asked.opening_line, OPENING_MOST);
        self.settle(now_ms, out);
        let mut generation = Value::Null;
        let mut who = (
            String::new(),
            String::new(),
            asked.turn_agent_id.to_string(),
        );
        let mut admitted = || -> Result<Caller, Refused> {
            // What is no way to hold a conversation is refused before anything else.
            live_asked(asked.conversation_mode)?;
            let caller = Self::caller(asked, people)?;
            who = (
                caller.session.clone(),
                caller.persona.clone(),
                caller.agent_id.clone(),
            );
            if reason.is_empty() {
                return Err(refused(400, "reason required"));
            }
            let listens = match asked.opening_mode {
                None => false,
                Some(Value::String(mode)) if mode == SAY => false,
                Some(Value::String(mode)) if mode == LISTEN_FIRST => true,
                Some(_) => return Err(refused(400, "unknown opening_mode")),
            };
            if opening.is_empty() {
                return Err(refused(400, "opening_line required"));
            }
            if !asked.event_context.is_null() {
                return Err(refused(400, "live_context_invalid"));
            }
            if listens {
                generation = proof.map_or(Value::Null, opening::Proof::generation);
                if generation.is_null() {
                    return Err(refused(
                        409,
                        "listen-first opening is unavailable: host identity unprovable; ring with opening_mode say",
                    ));
                }
            }
            self.may_ring(&caller, now_ms)?;
            Ok(caller)
        };
        match admitted() {
            Ok(caller) => Ok(Admitted {
                generation,
                caller,
                reason,
                opening,
                at: now_ms,
                live: None,
            }),
            Err(why) => {
                let (session, persona, agent_id) = who;
                let entry = json!({
                    "outcome": "refused", "error": why.body["error"], "session": session,
                    "persona": persona, "agent_id": agent_id, "reason": reason,
                });
                self.write(entry, now_ms, out);
                Err(why)
            }
        }
    }

    /// `POST /calls/ring`, its second half: the admitted ring rings. `opening_audio_url` is
    /// where its opening line is, voiced, or empty; `new_id` is a new id for it.
    pub fn place(
        &mut self,
        admitted: Admitted,
        opening_audio_url: &str,
        new_id: &str,
        now_ms: i64,
        out: &mut Effects,
    ) -> Ring {
        let Admitted {
            generation,
            caller,
            reason,
            opening,
            at,
            live,
        } = admitted;
        let ring = Ring {
            opening_mode: if generation.is_null() {
                String::new()
            } else {
                LISTEN_FIRST.into()
            },
            generation,
            opening: Value::Null,
            ring_id: format!("rng_{new_id}"),
            session: caller.session,
            persona: caller.persona,
            agent_id: caller.agent_id,
            reason,
            opening_line: opening,
            opening_audio_url: opening_audio_url.to_string(),
            state: RINGING.to_string(),
            created_at: at,
            expires_at: at + RING_MS,
            ended_at: None,
            answered_by: String::new(),
            call_id: String::new(),
            conversation_mode: live.as_ref().map_or_else(String::new, |_| LIVE.to_string()),
            live,
        };
        self.rings.push(ring.clone());
        if self.rings.len() > KEPT {
            let dropped = self.rings.len() - KEPT;
            self.rings.drain(..dropped);
        }
        out.changed.rings = true;
        let entry = json!({
            "outcome": RINGING, "ring_id": ring.ring_id, "session": ring.session,
            "persona": ring.persona, "agent_id": ring.agent_id, "reason": ring.reason,
        });
        self.write(entry, now_ms, out);
        Self::announce(&ring, "ring", now_ms, out);
        ring
    }

    /// What the one who rang is answered, once the phones were pushed: `delivery` is what
    /// became of that.
    pub fn rung(ring: &Ring, delivery: Value, now_ms: i64) -> Value {
        json!({
            "ok": true, "ring": ring.told(now_ms), "delivery": delivery,
            "summary": format!("Calling the user. The call rings for {} seconds.", RING_MS / 1000),
        })
    }

    /// A ring that rings ends as `state`. `own`: the agent that may end it, when only one may.
    fn end(
        &mut self,
        ring_id: &str,
        (state, by): (&'static str, &str),
        own: Option<&str>,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        self.settle(now_ms, out);
        let place = self.place_of(ring_id)?;
        let ring = &self.rings[place];
        if ring.of_the_user().is_some_and(|phone| phone != by) {
            return Err(refused(404, "unknown call"));
        }
        if own.is_some_and(|agent| !agent.is_empty() && ring.agent_id != agent) {
            return Err(refused(403, "an agent can only cancel its own call"));
        }
        if ring.state == state {
            return Ok(json!({ "ok": true, "ring": ring.told(now_ms), "changed": false }));
        }
        if !ring.rings() {
            return Err(over(ring, now_ms));
        }
        self.rings[place].state = state.to_string();
        self.rings[place].ended_at = Some(now_ms);
        self.rings[place].unheld();
        out.changed.rings = true;
        let ring = self.rings[place].clone();
        let entry = json!({
            "outcome": state, "ring_id": ring.ring_id, "session": ring.session, "by": by,
        });
        self.write(entry, now_ms, out);
        Self::announce(&ring, state, now_ms, out);
        Ok(json!({ "ok": true, "ring": ring.told(now_ms), "changed": true }))
    }

    /// `POST /calls/ring/decline`: the user does not take the call.
    pub fn decline(
        &mut self,
        (principal, ring_id): (&str, &str),
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        self.end(ring_id, (DECLINED, principal), None, now_ms, out)
    }

    /// `POST /calls/ring/cancel`: the agent hangs up before the user answers. An agent
    /// proven by its turn cancels its own ring only; the owner's tools any.
    pub fn cancel(
        &mut self,
        (turn_agent_id, ring_id): (&str, &str),
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        let ring = &self.rings[self.place_of(ring_id)?];
        if ring.of_the_user().is_some() {
            return Err(refused(404, "unknown call"));
        }
        if !ring.rings() {
            return Ok(json!({ "ok": true, "ring": ring.told(now_ms), "changed": false }));
        }
        let by = if turn_agent_id.is_empty() {
            "owner"
        } else {
            turn_agent_id
        };
        self.end(ring_id, (CANCELLED, by), Some(turn_agent_id), now_ms, out)
    }

    /// `POST /calls/ring/answer`: the user takes the call, as the ring was placed. For an
    /// ordinary ring a call starts with the agent on the floor, and the answer hands back the
    /// opening line to play first; the same phone asking again is given the same call, and
    /// another is told the call was answered elsewhere. For a ring held as a live voice call
    /// the phone is given its grant instead, and no call of the ordinary kind starts.
    pub fn answer(
        &mut self,
        calls: &mut Calls,
        asked: &Answer,
        given: Given,
        world: &World,
        at: calls::At,
        (out, of_calls): (&mut Effects, &mut calls::Effects),
    ) -> Result<Value, Refused> {
        let now_ms = at.now_ms;
        if self.is_live(asked.ring_id) {
            return self.answer_live(asked, (calls, given.minted), world, now_ms, out);
        }
        let live = live_asked(asked.conversation_mode)?;
        let known = self.rings.iter().any(|ring| ring.ring_id == asked.ring_id);
        if known && live {
            // A ring is answered as it was placed, and this one is no live voice call.
            return Err(refused(409, "live_mode_conflict"));
        }
        self.settle(now_ms, out);
        let place = self.place_of(asked.ring_id)?;
        let ring = &self.rings[place];
        if ring.state == ANSWERED {
            if ring.answered_by != asked.principal {
                return Err(refused(409, "the call was answered elsewhere"));
            }
            if asked.opening_mode.is_some_and(|m| m == LISTEN_FIRST)
                && !ring.opening.is_null()
                && ring.opening["state"] != "fallback"
            {
                let generation = world
                    .opening
                    .map_or(Value::Null, opening::Proof::generation);
                if opening::breach(&ring.generation, &generation).is_some()
                    || opening::breach(&ring.opening["fence"]["host_gen"], &generation).is_some()
                    || !ring.opening["retired"].is_null()
                    || !ring.opening["identity_breach"].is_null()
                {
                    return Err(over(ring, now_ms));
                }
            }
            let call = calls.by_id(&ring.call_id, now_ms).unwrap_or(Value::Null);
            return Ok(Self::answered(
                ring,
                call,
                false,
                asked.opening_mode,
                now_ms,
            ));
        }
        if !ring.rings() {
            return Err(over(ring, now_ms));
        }
        let mut placed = json!({
            "ring_id": ring.ring_id, "reason": ring.reason, "opening_line": ring.opening_line,
        });
        let capable = asked.opening_mode.is_some_and(|mode| mode == LISTEN_FIRST);
        let listen = ring.opening_mode == LISTEN_FIRST;
        let generation = if listen {
            let first = world
                .opening
                .map_or(Value::Null, opening::Proof::generation);
            let second = world
                .opening
                .map_or(Value::Null, opening::Proof::generation);
            let proven = if first == second { first } else { Value::Null };
            if let Some(cause) = opening::breach(&ring.generation, &proven) {
                let ring = &mut self.rings[place];
                ring.state = MISSED.into();
                ring.ended_at = Some(now_ms);
                let ring = ring.clone();
                out.changed.rings = true;
                self.write(json!({"outcome":"refused","error":"the call is over","ring_id":ring.ring_id,"cause":cause,"by":asked.principal}),now_ms,out);
                out.events.push(ring.event(now_ms));
                out.pushes.push(Pushed {
                    ring: ring.clone(),
                    kind: "missed",
                });
                return Err(over(&ring, now_ms));
            }
            if capable {
                placed["opening_mode"] = json!(LISTEN_FIRST);
            }
            proven
        } else {
            Value::Null
        };
        let asker = Asker {
            caller: asked.principal,
            by: &ring.session,
            ..Asker::default()
        };
        let agents = [ring.session.clone()];
        let started = calls.start(
            asker,
            (&agents, &ring.ring_id, Some(placed)),
            (given.roster, given.new_id),
            at,
            of_calls,
        )?;
        let call = started["call"].clone();
        let ring = &mut self.rings[place];
        ring.state = ANSWERED.to_string();
        ring.ended_at = Some(now_ms);
        ring.answered_by = asked.principal.to_string();
        ring.call_id = call["call_id"].as_str().unwrap_or("").to_string();
        if let Some(proof) = world.opening.filter(|_| listen) {
            let token = if capable {
                format!("opn_{}", python::first(given.minted.ticket, 32))
            } else {
                String::new()
            };
            let nonce = if capable {
                format!("own_{}", python::first(given.minted.nonce, 32))
            } else {
                String::new()
            };
            let before = proof.generation();
            let after = proof.generation();
            let proven = if before == after { before } else { Value::Null };
            let late = opening::breach(&generation, &proven);
            ring.opening = opening::record(
                opening::Record {
                    ring,
                    principal: asked.principal,
                    host: world.host_id,
                    token: &token,
                    nonce: &nonce,
                    now: now_ms,
                    generation: if late.is_some() {
                        Value::Null
                    } else {
                        generation
                    },
                },
                proof,
            );
            if let Some(cause) = late {
                ring.opening["retired"] = json!({"reason":cause,"at":now_ms});
                ring.opening["identity_breach"] = json!({"cause":cause,"at":now_ms});
                if ring.opening["state"] != "fallback" {
                    ring.opening["state"] = json!("unknown");
                }
                out.changed.rings = true;
                let ring = ring.clone();
                self.write(json!({"outcome":"refused","error":"the call is over","ring_id":ring.ring_id,"cause":cause,"by":asked.principal,"call_id":ring.call_id}),now_ms,out);
                let _ = calls.end(
                    Asker {
                        caller: asked.principal,
                        call_id: &ring.call_id,
                        by: &ring.session,
                        ..Default::default()
                    },
                    at,
                    of_calls,
                );
                Self::announce(&ring, ANSWERED, now_ms, out);
                return Err(over(&ring, now_ms));
            }
        }
        out.changed.rings = true;
        let ring = ring.clone();
        let entry = json!({
            "outcome": ANSWERED, "ring_id": ring.ring_id, "session": ring.session,
            "by": asked.principal, "call_id": ring.call_id,
        });
        self.write(entry, now_ms, out);
        Self::announce(&ring, ANSWERED, now_ms, out);
        Ok(Self::answered(
            &ring,
            call,
            true,
            asked.opening_mode,
            now_ms,
        ))
    }

    fn answered(
        ring: &Ring,
        call: Value,
        changed: bool,
        opening_mode: Option<&Value>,
        now_ms: i64,
    ) -> Value {
        if opening_mode.is_some_and(|m| m == LISTEN_FIRST)
            && !ring.opening.is_null()
            && ring.opening["state"] != "fallback"
        {
            return json!({"ok":true,"ring":ring.told(now_ms),"call":call,"changed":changed,"opening":opening::public(&ring.opening,true)});
        }
        let mut opening = json!({
            "text": ring.opening_line, "audio_url": ring.opening_audio_url,
            "session": ring.session, "persona": ring.persona,
        });
        // A phone that says how it would open is told how this ring opens: its line is said.
        if opening_mode.is_some() {
            opening["mode"] = json!(SAY);
        }
        json!({
            "ok": true, "ring": ring.told(now_ms), "call": call, "changed": changed,
            "opening": opening,
        })
    }

    /// `GET /calls/ring/settings`: the owner's switches and the newest of the trail.
    pub fn shown(&self) -> Value {
        let newest: Vec<&Value> = self.audit.iter().rev().take(AUDIT_SHOWN).collect();
        json!({ "ok": true, "settings": self.settings(), "audit": newest })
    }

    /// `POST /calls/ring/settings`: the owner turns rings on or off, or says whether a
    /// locked phone shows why an agent calls.
    pub fn change(
        &mut self,
        said: &Value,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        if let Some(enabled) = said.get("enabled") {
            let Some(enabled) = enabled.as_bool() else {
                return Err(refused(400, "enabled must be true or false"));
            };
            self.enabled = enabled;
            out.changed.settings = true;
            self.write(
                json!({ "outcome": "settings", "enabled": enabled }),
                now_ms,
                out,
            );
        }
        if let Some(show) = said.get("show_reason") {
            let Some(show) = show.as_bool() else {
                return Err(refused(400, "show_reason must be true or false"));
            };
            self.show_reason = show;
            out.changed.settings = true;
            self.write(
                json!({ "outcome": "settings", "show_reason": show }),
                now_ms,
                out,
            );
        }
        Ok(self.shown())
    }

    /// `POST /calls/ring/voip-token`: a phone leaves the token an incoming call would be
    /// pushed to, one for each phone; an empty token forgets it. `modes`: the ways it can
    /// hold a ring's conversation, when it says.
    pub fn voip_token(
        &mut self,
        principal: &str,
        (token, environment, modes): (&str, &str, &Value),
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        let token = python::strip(token).to_lowercase();
        let environment = if environment.to_lowercase() == "sandbox" {
            "sandbox"
        } else {
            "production"
        };
        let signs = token.chars().count();
        let hex = token
            .chars()
            .all(|sign| sign.is_ascii_digit() || ('a'..='f').contains(&sign));
        if !token.is_empty() && !((32..=200).contains(&signs) && hex) {
            return Err(refused(400, "token must be hex"));
        }
        let modes = declared_modes(modes)?;
        let place = self
            .voip
            .iter()
            .position(|row| row["principal"] == principal);
        if token.is_empty() {
            if let Some(place) = place {
                self.voip.remove(place);
            }
        } else {
            let mut row = json!({
                "principal": principal, "token": token, "environment": environment,
                "updated_at": now_ms,
            });
            if !modes.is_empty() {
                row["supported_conversation_modes"] = json!(modes);
            }
            match place {
                Some(place) => self.voip[place] = row,
                None => self.voip.push(row),
            }
        }
        out.changed.voip = true;
        Ok(json!({ "ok": true, "registered": !token.is_empty() }))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn theo() -> Caller {
        Caller {
            session: "theo-97e5".to_string(),
            persona: "Theo".to_string(),
            agent_id: "a-theo".to_string(),
            muted: false,
            can_call: true,
        }
    }

    fn asked<'a>(conversation_mode: &'a Value, opening_mode: Option<&'a Value>) -> Asked<'a> {
        Asked {
            turn_agent_id: "a-theo",
            agent: "",
            reason: "A leak",
            opening_line: "Shall I look?",
            conversation_mode,
            event_context: &Value::Null,
            opening_mode,
        }
    }

    /// A Host with no profile and no phone that holds a live call.
    struct Bare;

    impl Profiles for Bare {
        fn profile(&self, _: &str) -> Option<String> {
            None
        }

        fn file(&self, _: &str, _: &str) -> Option<Vec<u8>> {
            None
        }

        fn takes_calls(&self, _: &str) -> Result<Option<String>, Refused> {
            Ok(None)
        }
    }

    fn world<'a>(device_ok: &'a dyn Fn(&str) -> bool) -> World<'a> {
        World {
            profiles: &Bare,
            android: &[],
            opening: None,
            can_push_android: false,
            device_ok,
            host_id: "host-1",
            has_key: false,
            runs_tools: false,
        }
    }

    fn placed(admission: Admission) -> Admitted {
        match admission {
            Admission::Place(admitted) => admitted,
            Admission::Again(answer) => panic!("{answer}"),
        }
    }

    #[test]
    fn a_ring_that_listens_first_is_refused_with_the_way_to_ring_instead() {
        let known = |agent_id: &str| (agent_id == "a-theo").then(theo);
        let people = People {
            roster: &[],
            known: &known,
        };
        let anyone = |_: &str| true;
        let world = world(&anyone);
        let mut rings = Rings::default();
        let mut out = Effects::default();
        let listens = json!("listen_first");
        let refused = rings
            .admit(
                &asked(&Value::Null, Some(&listens)),
                people,
                &world,
                1_000,
                &mut out,
            )
            .unwrap_err();
        assert_eq!(refused.status, 409);
        let said = refused.body["error"].as_str().unwrap();
        assert!(said.ends_with("ring with opening_mode say"), "{said}");
        // It is in the trail, and nothing rings.
        assert_eq!(out.audited.len(), 1);
        assert!(rings.ringing().is_none() && out.events.is_empty());
        // The same ring with its line said is placed.
        let say = json!("say");
        let admitted = rings
            .admit(
                &asked(&json!("classic"), Some(&say)),
                people,
                &world,
                1_000,
                &mut out,
            )
            .unwrap();
        let ring = rings.place(placed(admitted), "", "1", 1_000, &mut out);
        assert_eq!(rings.ringing(), Some(&ring));
        assert_eq!(ring.expires_at, 1_000 + RING_MS);
    }

    #[test]
    fn a_ring_is_pushed_as_a_call_only_to_a_phone_that_is_still_paired() {
        let known = |agent_id: &str| (agent_id == "a-theo").then(theo);
        let people = People {
            roster: &[],
            known: &known,
        };
        let paired = |phone: &str| phone == "device_a";
        let world = world(&paired);
        let mut rings = Rings::default();
        let mut out = Effects::default();
        for phone in ["device_a", "device_gone", "administrator"] {
            let token = (&*"ab".repeat(32), "", &Value::Null);
            rings.voip_token(phone, token, 500, &mut out).unwrap();
        }
        let admitted = rings.admit(&asked(&Value::Null, None), people, &world, 1_000, &mut out);
        let ring = rings.place(placed(admitted.unwrap()), "", "1", 1_000, &mut out);
        // The ring is for every phone that left a token for calls; it is pushed as a call
        // only to the phone that is still paired with all rights, and a token once.
        let theirs = rings.sent(&ring, "ring", &world).voip.unwrap();
        assert_eq!(theirs, ["device_a", "device_gone", "administrator"]);
        let its_token = (
            "device_a".to_string(),
            "ab".repeat(32),
            "production".to_string(),
        );
        assert_eq!(rings.rung_as_calls(&ring, &world), [its_token]);
        assert_eq!(
            rings.call_token("device_a"),
            Some((&*"ab".repeat(32), "production"))
        );
        assert_eq!(rings.call_token("device_none"), None);
        // An alert follows where no call push reached a phone, and never for a live ring.
        assert_eq!(rings.alerted(&ring, 0), Some(Vec::new()));
        assert_eq!(rings.alerted(&ring, 1), None);
        assert!(alert_follows("", 0) && !alert_follows("", 2));
        assert!(!alert_follows("agent_live_v1", 0));
    }

    #[test]
    fn rings_are_read_back_as_they_were_kept() {
        let known = |agent_id: &str| (agent_id == "a-theo").then(theo);
        let people = People {
            roster: &[],
            known: &known,
        };
        let anyone = |_: &str| true;
        let mut rings = Rings::default();
        let mut out = Effects::default();
        let admitted = rings
            .admit(
                &asked(&Value::Null, None),
                people,
                &world(&anyone),
                1_000,
                &mut out,
            )
            .unwrap();
        let admitted = placed(admitted);
        rings.place(admitted, "/audio/1000__theo-97e5.mp3", "1", 1_000, &mut out);
        rings
            .change(
                &json!({ "enabled": false, "show_reason": true }),
                2_000,
                &mut out,
            )
            .unwrap();
        let kept = serde_json::to_vec(rings.rings()).unwrap();
        let again = Rings::kept(
            serde_json::from_slice(&kept).unwrap(),
            rings.audit().to_vec(),
            &rings.settings(),
            rings.voip().to_vec(),
        );
        assert_eq!(again, rings);
        assert!(again.shows_reason());
        // What was kept before a field was there is read with the field empty.
        let old = json!([{
            "ring_id": "rng_1", "session": "s", "persona": "P", "agent_id": "a", "reason": "r",
            "opening_line": "o", "state": "missed", "created_at": 1, "expires_at": 2,
        }]);
        let read: Vec<Ring> = serde_json::from_value(old).unwrap();
        assert_eq!((read[0].ended_at, read[0].call_id.as_str()), (None, ""));
    }
}
