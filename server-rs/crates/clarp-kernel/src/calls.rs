//! Calls: the Host's record of a hands-free conversation with several agents.
//!
//! The phone captures and plays. The Host keeps who is in the call, who is on hold and who
//! has the floor, so that agents can manage the call for their user ("can we add Mike?",
//! "put Solu on hold"). Nothing here reads what was said: the record changes only by an
//! explicit request (the app's buttons, an agent's own tool) and by routing, which moves
//! the floor to whoever was spoken to. One live call for each phone that owns one; the
//! newest twenty that ended are kept beside it.
//!
//! This is the Python Host's contract (`docs/group-calls.md`, `server/lib/group_calls.py`)
//! as rules without a store, a clock or a stream: a request is given the moment, the
//! Host's roster and a new id, and says what it answers, what the apps are told and what
//! is noted in the agents' conversations.
//!
//! Held to the Python Host's own module by `tests/fixtures/calls-vectors.json`.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{difflib, python};

/// Who the Host's own tools act as: for whichever phone's call.
pub const ADMINISTRATOR: &str = "administrator";
/// How many agents a call holds.
pub const MOST: usize = 8;
/// How many ended calls are kept for each phone.
pub const KEPT_ENDED: usize = 20;
/// A call nothing happened to for this long is over.
pub const STALE_MS: i64 = 12 * 60 * 60 * 1000;
/// How long the beginning of a name must be to stand for it, and how alike a spoken name
/// must be.
const PREFIX_LEAST: usize = 3;
const ALIKE_LEAST: f64 = 0.75;

const LIVE: &str = "live";
const ACTIVE: &str = "active";
const INVITED: &str = "invited";
const ON_HOLD: &str = "on_hold";
const LEFT: &str = "left";

/// A request that is refused: the status and what the asker is told.
#[derive(Debug, Clone, PartialEq)]
pub struct Refused {
    pub status: u16,
    pub body: Value,
}

fn refused(status: u16, error: &str) -> Refused {
    Refused {
        status,
        body: json!({ "ok": false, "error": error }),
    }
}

fn refused_with(status: u16, error: &str, more: Value) -> Refused {
    let mut body = json!({ "ok": false, "error": error });
    if let (Some(body), Value::Object(more)) = (body.as_object_mut(), more) {
        body.extend(more);
    }
    Refused { status, body }
}

/// An agent that may be asked into a call: one the app shows as a contact (not archived).
/// A helper is another agent's sub-agent: found by its session, never by its name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Contact {
    pub session: String,
    pub agent_id: String,
    pub persona: String,
    pub helper: bool,
}

/// The letters and digits of a name, small.
fn letters(value: &str) -> String {
    let small = value.to_lowercase();
    small
        .chars()
        .filter(|sign| sign.is_alphanumeric())
        .collect()
}

/// `mike` for `mike-86db`: the part of a session name that is said.
fn stem(session: &str) -> String {
    match session.rsplit_once('-') {
        Some((head, tail))
            if !head.is_empty()
                && !tail.is_empty()
                && tail.chars().all(char::is_alphanumeric)
                && tail.chars().count() <= 8 =>
        {
            letters(head)
        }
        _ => letters(session),
    }
}

/// How well a spoken name fits an agent: 3 the same, 2 one begins the other, 1 alike.
fn score(spoken: &str, contact: &Contact) -> u8 {
    let mut best = 0;
    for name in [letters(&contact.persona), stem(&contact.session)] {
        if name.is_empty() {
            continue;
        }
        if spoken == name {
            return 3;
        }
        let begins = name.starts_with(spoken) || spoken.starts_with(&name);
        if spoken.chars().count() >= PREFIX_LEAST && begins {
            best = best.max(2);
        } else {
            let signs = |text: &str| -> Vec<String> { text.chars().map(String::from).collect() };
            let (said, known) = (signs(spoken), signs(&name));
            let said: Vec<&str> = said.iter().map(String::as_str).collect();
            let known: Vec<&str> = known.iter().map(String::as_str).collect();
            if difflib::ratio(&said, &known) >= ALIKE_LEAST {
                best = best.max(1);
            }
        }
    }
    best
}

fn brief(contact: &Contact) -> Value {
    json!({ "session": contact.session, "persona": contact.persona })
}

/// The one agent a typed or spoken name means. A session name or an agent's name as it is
/// written first; then a spoken approximation of either. Of two that fit equally, the one
/// that is in the call (`in_call`) is meant, when one of them is.
pub fn named<'a>(
    name: &str,
    roster: &'a [Contact],
    in_call: &[&str],
) -> Result<&'a Contact, Refused> {
    let wanted = python::strip(name);
    let small = wanted.to_lowercase();
    if let Some(found) = roster
        .iter()
        .find(|contact| contact.session.to_lowercase() == small)
    {
        return Ok(found);
    }
    let by_name: Vec<&Contact> = roster
        .iter()
        .filter(|contact| !contact.helper && contact.persona.to_lowercase() == small)
        .collect();
    match by_name.as_slice() {
        [one] if !wanted.is_empty() => return Ok(one),
        [_, _, ..] if !wanted.is_empty() => {
            let inside: Vec<&&Contact> = by_name
                .iter()
                .filter(|contact| in_call.contains(&contact.session.as_str()))
                .collect();
            if let [one] = inside.as_slice() {
                return Ok(one);
            }
            let candidates: Vec<Value> = by_name.iter().map(|contact| brief(contact)).collect();
            return Err(refused_with(
                409,
                "ambiguous agent",
                json!({ "candidates": candidates }),
            ));
        }
        _ => {}
    }
    let spoken = letters(wanted);
    if spoken.is_empty() {
        return Err(refused(404, "unknown agent"));
    }
    let scored: Vec<(u8, &Contact)> = roster
        .iter()
        .filter(|contact| !contact.helper)
        .map(|contact| (score(&spoken, contact), contact))
        .collect();
    let top = scored.iter().map(|(score, _)| *score).max().unwrap_or(0);
    let mut found: Vec<&Contact> = scored
        .iter()
        .filter(|(score, _)| *score == top && *score > 0)
        .map(|(_, contact)| *contact)
        .collect();
    if found.len() > 1 {
        let inside: Vec<&Contact> = found
            .iter()
            .filter(|contact| in_call.contains(&contact.session.as_str()))
            .copied()
            .collect();
        if inside.len() == 1 {
            found = inside;
        }
    }
    match found.as_slice() {
        [] => Err(refused(404, "unknown agent")),
        [one] => Ok(one),
        many => {
            let candidates: Vec<Value> = many.iter().map(|contact| brief(contact)).collect();
            Err(refused_with(
                409,
                "ambiguous agent",
                json!({ "candidates": candidates }),
            ))
        }
    }
}

/// One agent in a call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Participant {
    pub session: String,
    pub agent_id: String,
    pub persona: String,
    /// `invited` (added, not yet spoken to), `active`, `on_hold`, `left`.
    pub state: String,
    pub joined_at: i64,
    pub changed_at: i64,
}

impl Participant {
    /// Whether it takes speech and may have the floor.
    fn reachable(&self) -> bool {
        self.state == ACTIVE || self.state == INVITED
    }

    fn set(&mut self, state: &str, now_ms: i64) -> bool {
        if self.state == state {
            return false;
        }
        self.state = state.to_string();
        self.changed_at = now_ms;
        true
    }
}

/// A call, as it is kept.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Call {
    pub call_id: String,
    pub host_id: String,
    /// The phone that owns it.
    pub principal: String,
    /// `live` or `ended`.
    pub state: String,
    /// Why it ended: `ended`, `empty`, `superseded`, `stale`.
    pub reason: String,
    /// One more with every change. An app drops what is not newer than what it has.
    pub revision: u64,
    pub started_at: i64,
    pub ended_at: Option<i64>,
    /// The session that gets what names nobody; empty when nobody can take speech.
    pub floor: String,
    pub participants: Vec<Participant>,
    /// Who had the floor, the latest last: where it goes back to.
    #[serde(default)]
    floor_order: Vec<String>,
    /// The request that started it: the same request again is the same call.
    #[serde(default)]
    request_id: String,
    #[serde(default)]
    changed_at: i64,
    /// Whom the call's latest utterance went to, and when.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    routed: Option<(String, i64)>,
    /// What the agent that placed the call said it is about (an answered ring).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    ring: Option<Value>,
}

impl Call {
    fn is_live(&self) -> bool {
        self.state == LIVE
    }

    fn place(&self, session: &str) -> Option<usize> {
        self.participants
            .iter()
            .position(|part| part.session == session)
    }

    /// Whom the call's latest utterance went to, and when.
    pub fn routed(&self) -> Option<(&str, i64)> {
        self.routed
            .as_ref()
            .map(|(session, at)| (session.as_str(), *at))
    }

    /// What the agent that placed the call said it is about.
    pub fn ring(&self) -> Option<&Value> {
        self.ring.as_ref()
    }

    /// As an app and an agent's tool read it.
    pub fn told(&self, now_ms: i64) -> Value {
        json!({
            "call_id": self.call_id, "host_id": self.host_id, "principal": self.principal,
            "state": self.state, "reason": self.reason, "revision": self.revision,
            "started_at": self.started_at, "ended_at": self.ended_at, "floor": self.floor,
            "participants": self.participants, "server_now": now_ms,
        })
    }

    fn give_floor(&mut self, session: &str) -> bool {
        if self.floor == session {
            return false;
        }
        self.floor = session.to_string();
        self.floor_order.retain(|had| had != session);
        self.floor_order.push(session.to_string());
        true
    }

    /// The one who has the floor can take speech no more: it goes to the one who had it
    /// last among those who can, else to the first who can, else to nobody.
    fn pass_floor(&mut self) {
        let reachable = |call: &Self, session: &str| {
            let part = call.place(session).map(|place| &call.participants[place]);
            part.is_some_and(Participant::reachable)
        };
        if !self.floor.is_empty() && reachable(self, &self.floor) {
            return;
        }
        let mut had = self.floor_order.iter().rev();
        if let Some(session) = had.find(|&session| reachable(self, session)).cloned() {
            self.floor = session;
            return;
        }
        match self.participants.iter().find(|part| part.reachable()) {
            Some(first) => {
                self.floor = first.session.clone();
                self.floor_order.push(first.session.clone());
            }
            None => self.floor = String::new(),
        }
    }

    fn finish(&mut self, reason: &str, now_ms: i64) {
        for part in &mut self.participants {
            part.set(LEFT, now_ms);
        }
        self.state = "ended".to_string();
        self.reason = reason.to_string();
        self.floor = String::new();
        self.ended_at = Some(now_ms);
    }

    /// Those still in the call, without the ones named.
    fn others(&self, without: &str) -> Vec<&Participant> {
        let still = self.participants.iter();
        still
            .filter(|part| part.session != without && part.state != LEFT)
            .collect()
    }
}

/// "Theo, Mike and Nadia".
fn names(parts: &[&Participant]) -> String {
    let names: Vec<&str> = parts.iter().map(|part| part.persona.as_str()).collect();
    match names.as_slice() {
        [] => String::new(),
        [one] => (*one).to_string(),
        [first @ .., last] => format!("{} and {last}", first.join(", ")),
    }
}

/// A notice for the conversation of an agent a change concerns: quiet, never spoken.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notice {
    pub session: String,
    pub agent_id: String,
    /// One for each change and agent: the same notice is written once.
    pub id: String,
    pub text: String,
}

/// What a request did beside its answer.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Effects {
    /// What the apps are told, in order: one `group-call` event for each change.
    pub events: Vec<Value>,
    /// What is noted in agents' conversations.
    pub notices: Vec<Notice>,
    /// The phones whose calls changed: their records are to be kept again.
    pub changed: BTreeSet<String>,
}

/// The moment of a request, and the Host it is made on.
#[derive(Debug, Clone, Copy)]
pub struct At<'a> {
    pub now_ms: i64,
    pub host_id: &'a str,
}

/// Who asks, and for which call.
#[derive(Debug, Clone, Copy, Default)]
pub struct Asker<'a> {
    /// The phone that asks, or [`ADMINISTRATOR`].
    pub caller: &'a str,
    /// The call it means; empty: the one that is up.
    pub call_id: &'a str,
    /// For the administrator: whose call.
    pub principal: &'a str,
    /// The agent that asks through its own tool; empty: the phone or the Host.
    pub by: &'a str,
}

/// Every phone's calls.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Calls {
    books: BTreeMap<String, Vec<Call>>,
}

type Texts = BTreeMap<String, String>;

impl Calls {
    /// From what is kept: each phone with its calls.
    pub fn kept(books: BTreeMap<String, Vec<Call>>) -> Self {
        Self { books }
    }

    /// One phone's calls, to be kept.
    pub fn of(&self, principal: &str) -> &[Call] {
        self.books.get(principal).map_or(&[], Vec::as_slice)
    }

    fn live_at(&self, principal: &str) -> Option<usize> {
        let calls = self.books.get(principal)?;
        calls.iter().rposition(Call::is_live)
    }

    /// One change of a call: its number, the record kept, the apps told, the agents' notices.
    fn commit(
        &mut self,
        (principal, place): (&str, usize),
        (action, session, by): (&str, &str, &str),
        notices: Option<Texts>,
        at: At,
        out: &mut Effects,
    ) {
        let Some(calls) = self.books.get_mut(principal) else {
            return;
        };
        let call = &mut calls[place];
        call.revision += 1;
        call.changed_at = at.now_ms;
        let mut event = json!({ "type": "group-call" });
        if let (Some(event), Value::Object(told)) = (event.as_object_mut(), call.told(at.now_ms)) {
            for (key, value) in told {
                if key != "server_now" {
                    event.insert(key, value);
                }
            }
            event.insert(
                "change".to_string(),
                json!({ "action": action, "session": session, "by": by }),
            );
            event.insert("server_now".to_string(), json!(at.now_ms));
        }
        out.events.push(event);
        for (session, text) in notices.unwrap_or_default() {
            if let Some(part) = call.place(&session).map(|place| &call.participants[place]) {
                out.notices.push(Notice {
                    id: format!("{}:{}:{}", call.call_id, call.revision, part.agent_id),
                    session,
                    agent_id: part.agent_id.clone(),
                    text: format!("Group call: {text}"),
                });
            }
        }
        // Of the calls that ended, the newest are kept.
        let ended: Vec<String> = calls
            .iter()
            .filter(|call| !call.is_live())
            .map(|call| call.call_id.clone())
            .collect();
        if ended.len() > KEPT_ENDED {
            let dropped: BTreeSet<&String> = ended[..ended.len() - KEPT_ENDED].iter().collect();
            calls.retain(|call| !dropped.contains(&call.call_id));
        }
        out.changed.insert(principal.to_string());
    }

    /// A phone's calls as they are looked at: a live one nothing happened to for half a day
    /// is over from then on.
    fn looked_at(&mut self, principal: &str, at: At, out: &mut Effects) {
        let Some(place) = self.live_at(principal) else {
            return;
        };
        let Some(calls) = self.books.get_mut(principal) else {
            return;
        };
        if at.now_ms - calls[place].changed_at > STALE_MS {
            calls[place].finish("stale", at.now_ms);
            self.commit((principal, place), ("end", "", ""), None, at, out);
        }
    }

    fn principals(&self) -> Vec<String> {
        self.books.keys().cloned().collect()
    }

    /// The live call a request applies to: whose it is, and its place among that phone's.
    fn locate(
        &mut self,
        asker: Asker,
        at: At,
        out: &mut Effects,
    ) -> Result<(String, usize), Refused> {
        let candidates = if asker.caller != ADMINISTRATOR {
            vec![asker.caller.to_string()]
        } else if !asker.principal.is_empty() {
            vec![asker.principal.to_string()]
        } else {
            self.principals()
        };
        let mut found = Vec::new();
        for who in candidates {
            self.looked_at(&who, at, out);
            if let Some(place) = self.live_at(&who) {
                let call = &self.books[&who][place];
                if asker.call_id.is_empty() || call.call_id == asker.call_id {
                    found.push((who, place));
                }
            }
        }
        match found.len() {
            0 => Err(refused(404, "no live call")),
            1 => Ok(found.remove(0)),
            _ => Err(refused(409, "several live calls")),
        }
    }

    fn result(
        &self,
        (principal, place): (&str, usize),
        changed: bool,
        summary: &str,
        at: At,
    ) -> Value {
        let call = &self.books[principal][place];
        json!({ "ok": true, "call": call.told(at.now_ms), "changed": changed, "summary": summary })
    }

    /// The same for a call by its id: after a change, when keeping only the newest that
    /// ended may have moved it.
    fn result_of(
        &self,
        (principal, call_id): (&str, &str),
        changed: bool,
        summary: &str,
        at: At,
    ) -> Value {
        let calls = self.books.get(principal).map_or(&[][..], Vec::as_slice);
        let call = calls.iter().find(|call| call.call_id == call_id);
        let told = call.map_or(Value::Null, |call| call.told(at.now_ms));
        json!({ "ok": true, "call": told, "changed": changed, "summary": summary })
    }

    /// `GET /calls`: the call that is up for the asker, or none.
    pub fn snapshot(&mut self, asker: Asker, at: At, out: &mut Effects) -> Result<Value, Refused> {
        let asker = Asker {
            call_id: "",
            ..asker
        };
        match self.locate(asker, at, out) {
            Ok((who, place)) => {
                let call = self.books[&who][place].told(at.now_ms);
                Ok(json!({ "call": call, "server_now": at.now_ms }))
            }
            Err(none) if none.body["error"] == "no live call" => {
                Ok(json!({ "call": null, "server_now": at.now_ms }))
            }
            Err(other) => Err(other),
        }
    }

    /// Whose call the administrator starts when it names nobody: the phone that made the
    /// newest call.
    fn starter(&mut self, asker: Asker, at: At, out: &mut Effects) -> Result<String, Refused> {
        if asker.caller != ADMINISTRATOR {
            return Ok(asker.caller.to_string());
        }
        if !asker.principal.is_empty() {
            return Ok(asker.principal.to_string());
        }
        let mut newest: Option<(i64, String)> = None;
        for who in self.principals() {
            self.looked_at(&who, at, out);
            for call in &self.books[&who] {
                let this = (call.started_at, who.clone());
                if newest.as_ref().is_none_or(|newest| this > *newest) {
                    newest = Some(this);
                }
            }
        }
        newest
            .map(|(_, who)| who)
            .ok_or_else(|| refused(409, "principal required: no device has made a call yet"))
    }

    /// `POST /calls`: a call with one to eight agents; the first has the floor. A call
    /// that is up ends for it, unless it was started by the same request. `ring`: the call
    /// is an answered ring of the first agent's, and what it rang about.
    pub fn start(
        &mut self,
        asker: Asker,
        (agents, request_id, ring): (&[String], &str, Option<Value>),
        (roster, new_id): (&[Contact], &str),
        at: At,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        let wanted: Vec<&str> = agents
            .iter()
            .map(|name| python::strip(name))
            .filter(|name| !name.is_empty())
            .collect();
        if wanted.is_empty() {
            return Err(refused(400, "agents required"));
        }
        let who = self.starter(asker, at, out)?;
        self.looked_at(&who, at, out);
        let live = self.live_at(&who);
        let request_id: String = python::strip(request_id).chars().take(128).collect();
        if let Some(place) = live
            && !request_id.is_empty()
            && self.books[&who][place].request_id == request_id
        {
            return Ok(self.result((&who, place), false, "The call is already up.", at));
        }
        let mut resolved: Vec<&Contact> = Vec::new();
        for name in wanted {
            let agent = named(name, roster, &[])?;
            if resolved.iter().all(|known| known.session != agent.session) {
                resolved.push(agent);
            }
        }
        if resolved.len() > MOST {
            return Err(refused(409, &format!("a call holds at most {MOST} agents")));
        }
        if let Some(place) = live {
            let call = &mut self.books.entry(who.clone()).or_default()[place];
            let was_in: Texts = call
                .participants
                .iter()
                .map(|part| (part.session.clone(), "the call ended.".to_string()))
                .collect();
            call.finish("superseded", at.now_ms);
            self.commit((&who, place), ("end", "", asker.by), Some(was_in), at, out);
        }
        let now = at.now_ms;
        let first = resolved[0].session.clone();
        let participants: Vec<Participant> = resolved
            .iter()
            .enumerate()
            .map(|(place, agent)| Participant {
                session: agent.session.clone(),
                agent_id: agent.agent_id.clone(),
                persona: agent.persona.clone(),
                state: if place == 0 { ACTIVE } else { INVITED }.to_string(),
                joined_at: now,
                changed_at: now,
            })
            .collect();
        let ring = ring.map(|ring| {
            let mut placed = json!({ "session": first });
            if let (Some(placed), Value::Object(ring)) = (placed.as_object_mut(), ring) {
                placed.extend(ring);
            }
            placed
        });
        let call = Call {
            call_id: format!("gcl_{new_id}"),
            host_id: at.host_id.to_string(),
            principal: who.clone(),
            state: LIVE.to_string(),
            reason: String::new(),
            revision: 0,
            started_at: now,
            ended_at: None,
            floor: first.clone(),
            participants,
            floor_order: vec![first.clone()],
            request_id,
            changed_at: now,
            routed: None,
            ring,
        };
        let everyone = names(&call.participants.iter().collect::<Vec<_>>());
        let told: Texts = call
            .participants
            .iter()
            .map(|part| {
                (
                    part.session.clone(),
                    format!("a call started with {everyone}."),
                )
            })
            .collect();
        let call_id = call.call_id.clone();
        let calls = self.books.entry(who.clone()).or_default();
        calls.push(call);
        let place = calls.len() - 1;
        self.commit(
            (&who, place),
            ("start", &first, asker.by),
            Some(told),
            at,
            out,
        );
        let summary = format!("Started a call with {everyone}.");
        Ok(self.result_of((&who, &call_id), true, &summary, at))
    }

    /// `POST /calls/<action>`: add, remove, hold, resume, switch to, or transfer to one
    /// agent.
    pub fn change(
        &mut self,
        asker: Asker,
        (action, agent): (&str, &str),
        roster: &[Contact],
        at: At,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        if !matches!(
            action,
            "add" | "remove" | "hold" | "resume" | "switch" | "transfer"
        ) {
            let said = python::repr(&json!(action));
            return Err(refused(400, &format!("unknown action {said}")));
        }
        if python::strip(agent).is_empty() {
            return Err(refused(400, "agent required"));
        }
        let (who, place) = self.locate(asker, at, out)?;
        let call = &mut self.books.entry(who.clone()).or_default()[place];
        let in_call: Vec<String> = call
            .participants
            .iter()
            .map(|part| part.session.clone())
            .collect();
        let in_call: Vec<&str> = in_call.iter().map(String::as_str).collect();
        let target = named(agent, roster, &in_call)?;
        let present = call
            .place(&target.session)
            .is_some_and(|place| call.participants[place].state != LEFT);
        if matches!(action, "remove" | "hold" | "resume") && !present {
            return Err(refused_with(
                409,
                "not in the call",
                json!({ "session": target.session, "persona": target.persona }),
            ));
        }
        let (changed, mut summary, mut notices) = apply(call, action, target, at.now_ms)?;
        let call_id = call.call_id.clone();
        if changed {
            let anybody = call
                .participants
                .iter()
                .any(|part| part.reachable() || part.state == ON_HOLD);
            if action == "remove" && !anybody {
                call.finish("empty", at.now_ms);
                for part in &call.participants {
                    if part.session != target.session {
                        notices.insert(part.session.clone(), "the call ended.".to_string());
                    }
                }
                summary.push_str(" That was the last one, so the call ended.");
            }
            let session = target.session.clone();
            self.commit(
                (&who, place),
                (action, &session, asker.by),
                Some(notices),
                at,
                out,
            );
        }
        Ok(self.result_of((&who, &call_id), changed, &summary, at))
    }

    /// `POST /calls/end`: the call is over. A call named that has ended already stays so.
    pub fn end(&mut self, asker: Asker, at: At, out: &mut Effects) -> Result<Value, Refused> {
        if !asker.call_id.is_empty() {
            let who = if asker.caller == ADMINISTRATOR {
                asker.principal
            } else {
                asker.caller
            };
            let candidates = if who.is_empty() {
                self.principals()
            } else {
                vec![who.to_string()]
            };
            for candidate in candidates {
                self.looked_at(&candidate, at, out);
                let calls = self.books.get(&candidate).map_or(&[][..], Vec::as_slice);
                let ended = calls
                    .iter()
                    .position(|call| call.call_id == asker.call_id && !call.is_live());
                if let Some(place) = ended {
                    let said = "The call has already ended.";
                    return Ok(self.result((&candidate, place), false, said, at));
                }
            }
        }
        let (who, place) = self.locate(asker, at, out)?;
        let call = &mut self.books.entry(who.clone()).or_default()[place];
        let was_in: Texts = call
            .participants
            .iter()
            .filter(|part| part.state != LEFT)
            .map(|part| (part.session.clone(), "the call ended.".to_string()))
            .collect();
        call.finish("ended", at.now_ms);
        let call_id = call.call_id.clone();
        self.commit((&who, place), ("end", "", asker.by), Some(was_in), at, out);
        Ok(self.result_of((&who, &call_id), true, "The call has ended.", at))
    }

    /// Where a message that carries a call goes: to the agent it names when that one can
    /// take speech, else to the floor. Whoever gets it takes the floor, and one who was
    /// invited is in. Answers the recipient and everybody who can take speech.
    pub fn route(
        &mut self,
        asker: Asker,
        session: &str,
        at: At,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        let ended = || refused_with(409, "call ended", json!({ "call_id": asker.call_id }));
        let (who, place) = self.locate(asker, at, out).map_err(|_| ended())?;
        let call = &self.books[&who][place];
        let reachable: Vec<String> = call
            .participants
            .iter()
            .filter(|part| part.reachable())
            .map(|part| part.session.clone())
            .collect();
        let recipient = if reachable.iter().any(|known| known == session) {
            session.to_string()
        } else {
            call.floor.clone()
        };
        if recipient.is_empty() {
            return Err(refused_with(
                409,
                "nobody to talk to",
                json!({ "call_id": asker.call_id }),
            ));
        }
        self.take_floor((&who, place), &recipient, at, out);
        Ok(json!({ "session": recipient, "reachable": reachable }))
    }

    /// A message of the call went to this agent by another way: it takes the floor.
    pub fn routed(&mut self, asker: Asker, session: &str, at: At, out: &mut Effects) {
        if let Ok((who, place)) = self.locate(asker, at, out) {
            self.take_floor((&who, place), session, at, out);
        }
    }

    fn take_floor(
        &mut self,
        (who, place): (&str, usize),
        session: &str,
        at: At,
        out: &mut Effects,
    ) {
        let Some(calls) = self.books.get_mut(who) else {
            return;
        };
        let call = &mut calls[place];
        let Some(part) = call.place(session) else {
            return;
        };
        if !call.participants[part].reachable() {
            return;
        }
        call.routed = Some((session.to_string(), at.now_ms));
        let mut changed = call.participants[part].set(ACTIVE, at.now_ms);
        changed |= call.give_floor(session);
        if changed {
            self.commit((who, place), ("route", session, ""), None, at, out);
        } else {
            out.changed.insert(who.to_string());
        }
    }
}

/// One action on a call: whether it changed anything, what an agent can say of it, and
/// what is noted for whom.
fn apply(
    call: &mut Call,
    action: &str,
    target: &Contact,
    now: i64,
) -> Result<(bool, String, Texts), Refused> {
    let (session, persona) = (target.session.as_str(), target.persona.as_str());
    let mut changed = false;
    let mut notices = Texts::new();
    // What is noted when something happens to one agent: `own` for it, `others` for
    // everybody else who is still in the call.
    let told = |call: &Call, own: String, others: String| -> Texts {
        let mut notices: Texts = call
            .others(session)
            .iter()
            .map(|part| (part.session.clone(), others.clone()))
            .collect();
        notices.insert(session.to_string(), own);
        notices
    };
    let mut place = call.place(session);
    if matches!(action, "add" | "switch" | "transfer") {
        match place {
            None => {
                let in_it = call.participants.iter().filter(|part| part.state != LEFT);
                if in_it.count() >= MOST {
                    return Err(refused(409, &format!("a call holds at most {MOST} agents")));
                }
                call.participants.push(Participant {
                    session: session.to_string(),
                    agent_id: target.agent_id.clone(),
                    persona: persona.to_string(),
                    state: INVITED.to_string(),
                    joined_at: now,
                    changed_at: now,
                });
                place = Some(call.participants.len() - 1);
                changed = true;
                let with = names(&call.others(session));
                let with = if with.is_empty() { "the user" } else { &with };
                notices = told(
                    call,
                    format!("you joined the call with {with}."),
                    format!("{persona} joined."),
                );
            }
            Some(at) if matches!(call.participants[at].state.as_str(), LEFT | ON_HOLD) => {
                changed |= call.participants[at].set(ACTIVE, now);
                notices = told(
                    call,
                    "you are back in the call.".to_string(),
                    format!("{persona} is back."),
                );
            }
            Some(_) => {}
        }
    }
    let Some(place) = place else {
        // Not in the call, and asked to leave it, wait or come back: refused before here.
        return Err(refused(409, "not in the call"));
    };
    match action {
        "add" => {
            if call.floor.is_empty() && changed {
                call.give_floor(session);
            }
            let summary = if changed {
                format!("{persona} joined the call.")
            } else {
                format!("{persona} is already in the call.")
            };
            Ok((changed, summary, notices))
        }
        "transfer" => {
            let held = Some(call.floor.clone())
                .filter(|floor| !floor.is_empty() && floor != session)
                .and_then(|floor| call.place(&floor));
            let mut put_on_hold = None;
            if let Some(held) = held {
                call.participants[held].set(ON_HOLD, now);
                let held = &call.participants[held];
                notices.insert(
                    held.session.clone(),
                    format!("you are on hold; the user is talking to {persona}."),
                );
                put_on_hold = Some(held.persona.clone());
                changed = true;
            }
            if call.participants[place].state == INVITED {
                changed |= call.participants[place].set(ACTIVE, now);
            }
            changed |= call.give_floor(session);
            notices
                .entry(session.to_string())
                .or_insert_with(|| "the user is talking to you now.".to_string());
            let summary = match put_on_hold {
                Some(held) => format!("Put {held} on hold and called {persona}."),
                None => format!("You're talking to {persona}."),
            };
            Ok((changed, summary, notices))
        }
        "switch" => {
            if call.participants[place].state == INVITED {
                changed |= call.participants[place].set(ACTIVE, now);
            }
            changed |= call.give_floor(session);
            notices
                .entry(session.to_string())
                .or_insert_with(|| "the user is talking to you now.".to_string());
            let notices = if changed { notices } else { Texts::new() };
            Ok((
                changed,
                format!("You're talking to {persona} now."),
                notices,
            ))
        }
        "resume" => {
            let changed = call.participants[place].set(ACTIVE, now);
            if changed && call.floor.is_empty() {
                call.give_floor(session);
            }
            let notices = told(
                call,
                "you are back in the call.".to_string(),
                format!("{persona} is back."),
            );
            let summary = if changed {
                format!("{persona} is back in the call.")
            } else {
                format!("{persona} is not on hold.")
            };
            Ok((
                changed,
                summary,
                if changed { notices } else { Texts::new() },
            ))
        }
        "hold" => {
            let changed = call.participants[place].set(ON_HOLD, now);
            call.pass_floor();
            let notices = told(
                call,
                "you are on hold.".to_string(),
                format!("{persona} is on hold."),
            );
            let summary = if changed {
                format!("{persona} is on hold.")
            } else {
                format!("{persona} is already on hold.")
            };
            Ok((
                changed,
                summary,
                if changed { notices } else { Texts::new() },
            ))
        }
        _ => {
            let changed = call.participants[place].set(LEFT, now);
            call.pass_floor();
            let notices = told(
                call,
                "you left the call.".to_string(),
                format!("{persona} left."),
            );
            Ok((changed, format!("{persona} left the call."), notices))
        }
    }
}
