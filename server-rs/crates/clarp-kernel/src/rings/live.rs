//! Rings held as live voice calls.
//!
//! A ring may ask that its conversation be held live: the phone that answers talks to a
//! voice model, and no call of the ordinary kind starts. Such a ring is placed with what its
//! producer says of an event, frozen with the ring ([`crate::live_call`]); it is shown only
//! to phones that said they can hold such a call; answering hands back a grant that one
//! phone joins once, within a minute; and the call ends by a close, or is switched once to
//! an ordinary call before its voice opened. The owner may start such a call with an agent
//! too: that one is the phone's own, already answered, and nobody else's to see.
//!
//! Where the authority of a call lies is on its ring: a phase from the answer to the end.
//! Whatever acts for a call (its session, a close, a fallback) asks the ring first, and a
//! call is retired there before anything of it is stopped.
//!
//! The Python Host's `agent_rings.py` and `agent_ring_live.py`, and the join of
//! `agent_ring_live_socket.py`, held to them by `tests/fixtures/live-ring-vectors.json`.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::{
    ANSWERED, Admitted, Answer, Asked, Effects, KEPT, LIVE, OPENING_MOST, People, REASON_MOST,
    RING_MS, RINGING, Ring, Rings, SAY, one_line,
};
use crate::calls::{self, Asker, Calls, Contact, Refused, refused};
use crate::live_call::{self, Agent, Folder, PHASES};

/// How long the grant of an answer may be joined.
pub const JOIN_MS: i64 = 60 * 1000;

const WAITING: &str = "answered_waiting_join";
const CONNECTING: &str = "connecting";
const READY: &str = "ready";
const ACTIVE: &str = "active";
const RETIRED: &str = "retired";
/// Why a call is over that was joined under a Host before this one.
const HOST_RESTART: &str = "host_restart";
const SWITCHED: &str = "classic";
const JOIN_EXPIRED: &str = "live_join_expired";

/// What a ring held as a live voice call keeps of it, beside what an app reads.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Live {
    /// Everything the voice is told, frozen when the ring was admitted. Kept until the call
    /// can no longer start or run; its digest stays.
    #[serde(skip_serializing_if = "Value::is_null")]
    pub snapshot: Value,
    /// The digest of the snapshot: what a grant and its join name the call by.
    pub context_digest: String,
    /// The event the ring is about (`source:id`), and the digest of what was said of it:
    /// the same event rings once.
    pub event_key: String,
    pub event_digest: String,
    /// The call, once the ring was answered.
    pub call_id: String,
    pub call_epoch: i64,
    pub attempt_id: String,
    pub owner_nonce: String,
    pub join_ticket: String,
    pub join_expires_at: i64,
    pub join_consumed: bool,
    /// Where the call is: empty before the answer, then one of the phases in which it has
    /// authority, then `retired`, or `classic` after the one switch.
    pub phase: String,
    pub retired_reason: String,
    /// The one request that closed the call, which alone may be answered again.
    pub close_receipt: Option<Value>,
    pub fallback_request_id: String,
    pub fallback_cancelled: bool,
    /// The voice was given its opening, and the user's sound went to it.
    pub opening_consumed: bool,
    pub audio_forwarded: bool,
    /// What the phone named its answer by.
    pub request_id: String,
    pub client_nonce: String,
    /// The phone that started the call, when the user did.
    pub user_call: String,
    /// The model the call runs on, chosen for the client that answered it (or asked for it)
    /// when its grant was made: what the grant, the session's frame and the voice's session
    /// all name. Empty in a log from before it was kept: the model every app accepts.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub model: String,
}

impl Live {
    /// The model the call runs on ([`Live::model`]).
    pub fn running(&self) -> &str {
        if self.model.is_empty() {
            live_call::MODEL
        } else {
            &self.model
        }
    }

    /// Whether the call has authority: from its answer to its end.
    fn holds(&self) -> bool {
        PHASES.contains(&self.phase.as_str())
    }

    /// The call loses its authority. Whether it had any.
    fn retire(&mut self, reason: &str) -> bool {
        if !self.holds() {
            return false;
        }
        self.phase = RETIRED.to_string();
        self.retired_reason = reason.to_string();
        self.shed();
        true
    }

    /// What the voice would have been told is needed no more: the call cannot start or run.
    pub(super) fn shed(&mut self) {
        self.snapshot = Value::Null;
    }
}

/// The profiles of a Host, as their folders have them.
pub trait Profiles {
    /// What `profile.json` of a profile holds, when it can be read.
    fn profile(&self, profile_id: &str) -> Option<String>;
    /// The bytes of a file inside the folder of a profile.
    fn file(&self, profile_id: &str, name: &str) -> Option<Vec<u8>>;
    /// The profile with which an agent takes the user's calls live, asked strictly
    /// ([`live_call::taking`]): `None` when it takes none so.
    fn takes_calls(&self, session: &str) -> Result<Option<String>, Refused>;
}

/// An Android phone's push row that says how it can hold a ring's conversation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Android {
    /// The paired device that registered it.
    pub owner: String,
    pub modes: Vec<String>,
}

/// What of the Host a live ring is admitted and held by.
#[derive(Clone, Copy)]
pub struct World<'a> {
    pub opening: Option<&'a dyn super::opening::Proof>,
    pub profiles: &'a dyn Profiles,
    pub android: &'a [Android],
    /// Whether this Host can push to Android phones at all.
    pub can_push_android: bool,
    /// Whether a paired device still has all rights. What is no device has.
    pub device_ok: &'a dyn Fn(&str) -> bool,
    pub host_id: &'a str,
    /// Whether the Host has a key for the voice.
    pub has_key: bool,
    /// Whether the Host runs the tools a profile may opt its calls into. One that does not
    /// holds no call whose voice would be told that it has them.
    pub runs_tools: bool,
    /// The owner's model of GPT-Live, and from which client contract, by the client's exact
    /// name, an agent's live call runs on it ([`live_call::agent_live_model`]).
    pub live_model: &'static str,
    pub live_model_min_client: &'a [(String, i64)],
}

impl World<'_> {
    /// The controls of a call are a paired phone's with all rights.
    fn a_phone(&self, principal: &str) -> bool {
        principal.starts_with("device_") && (self.device_ok)(principal)
    }
}

/// New names for an answered call: its id, its attempt, the owner's nonce and the ticket.
#[derive(Debug, Clone, Copy)]
pub struct Minted<'a> {
    pub call: &'a str,
    pub attempt: &'a str,
    pub nonce: &'a str,
    pub ticket: &'a str,
}

/// What an answer is given to make its call of: the contacts and a new id for the call an
/// ordinary ring starts, and new names for the call a live ring is held as.
#[derive(Debug, Clone, Copy)]
pub struct Given<'a> {
    pub roster: &'a [Contact],
    pub new_id: &'a str,
    pub minted: Minted<'a>,
}

/// What became of a request to ring.
#[derive(Debug, Clone, PartialEq)]
pub enum Admission {
    /// It may be placed ([`Rings::place`]).
    Place(Admitted),
    /// The event of a live ring already rang: the answer names that ring, and nothing is
    /// placed.
    Again(Value),
}

/// A call that was joined: what its session holds it by.
#[derive(Debug, Clone, PartialEq)]
pub struct Joined {
    pub ring_id: String,
    pub agent_id: String,
    pub call_id: String,
    pub call_epoch: i64,
    pub attempt_id: String,
    pub context_digest: String,
    pub snapshot: Value,
    /// The phone that joined.
    pub principal: String,
    /// The model the call runs on, as its grant named it.
    pub model: String,
}

/// The first half of a switch to an ordinary call.
#[derive(Debug, Clone, PartialEq)]
pub enum Claim {
    /// It was switched before by this very request: the answer of then.
    Before(Value),
    /// The call is retired for it: its session is to be stopped, and then the ordinary call
    /// started ([`Rings::switched`]).
    Stop { call_id: String },
}

/// Who is sent what for one change of a ring.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Sent {
    /// The phones whose call tokens are pushed: for a ring that begins to ring.
    pub voip: Option<Vec<String>>,
    /// The Android phones that are told of a live ring, in every state of it.
    pub android: Vec<String>,
}

/// Whether a change of a ring is alerted once its call pushes are over: never for a ring held
/// as a live voice call, and not where a call push reached a phone.
pub fn alert_follows(conversation_mode: &str, reached: u64) -> bool {
    conversation_mode != LIVE && reached == 0
}

/// Two texts compared without saying where they differ by how long it took.
fn same(ours: &str, theirs: &str) -> bool {
    let (ours, theirs) = (ours.as_bytes(), theirs.as_bytes());
    let mut differs = ours.len() ^ theirs.len();
    for (at, sign) in theirs.iter().enumerate() {
        differs |= usize::from(ours.get(at).copied().unwrap_or(0) ^ sign);
    }
    differs == 0
}

fn stale() -> Refused {
    refused(409, "live_call_stale")
}

fn not_the_owner() -> Refused {
    refused(409, "live_owner_conflict")
}

fn no_request() -> Refused {
    refused(400, "live_request_invalid")
}

fn not_allowed() -> Refused {
    refused(409, "live_fallback_not_allowed")
}

/// An answer names what it means exactly: a request id and a nonce.
fn intent<'a>(
    request_id: &'a Value,
    client_nonce: &'a Value,
) -> Result<(&'a str, &'a str), Refused> {
    match (
        live_call::request_id(request_id),
        live_call::request_id(client_nonce),
    ) {
        (Some(request_id), Some(client_nonce)) => Ok((request_id, client_nonce)),
        _ => Err(no_request()),
    }
}

/// A whole number of nought or more, as a join names its protocol and its epoch.
fn counted(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) if !number.is_f64() => number.as_i64().filter(|it| *it >= 0),
        _ => None,
    }
}

/// The identity a join claims, said back with a refusal: only what is well made, and
/// otherwise that of no call.
pub fn claimed(event: &Value) -> Value {
    if event["type"] == "agent_live.join" {
        let call = live_call::request_id(&event["call_id"]);
        let attempt = live_call::request_id(&event["attempt_id"]);
        if let (Some(call), Some(attempt), Some(epoch)) =
            (call, attempt, counted(&event["call_epoch"]))
        {
            return json!({ "call_id": call, "call_epoch": epoch, "attempt_id": attempt });
        }
    }
    json!({ "call_id": "", "call_epoch": 0, "attempt_id": "" })
}

impl Rings {
    /// The phones that switched rings off for themselves, to be kept.
    pub fn prefs(&self) -> &[Value] {
        &self.prefs
    }

    /// With the phones that switched rings off, as they were kept.
    #[must_use]
    pub fn with_prefs(mut self, prefs: Vec<Value>) -> Self {
        self.prefs = prefs;
        self
    }

    /// Whether a phone receives rings: it does until it switched them off for itself.
    pub fn receives(&self, principal: &str) -> bool {
        !self.prefs.iter().any(|row| row["principal"] == principal)
    }

    /// The call tokens of phones that said they can hold a live call.
    fn live_tokens(&self) -> impl Iterator<Item = &str> {
        let says_live = |row: &&Value| {
            let modes = row["supported_conversation_modes"].as_array();
            modes.is_some_and(|modes| modes.iter().any(|mode| mode == LIVE))
        };
        let tokens = self.voip.iter().filter(says_live);
        tokens.filter_map(|row| row["principal"].as_str())
    }

    /// The Android phones that said so, still paired with all rights.
    fn live_androids<'a>(world: &World<'a>) -> impl Iterator<Item = &'a str> {
        let device_ok = world.device_ok;
        let rows = world.android.iter().filter(move |row| {
            row.modes.iter().any(|mode| mode == LIVE)
                && row.owner.starts_with("device_")
                && device_ok(&row.owner)
        });
        rows.map(|row| row.owner.as_str())
    }

    /// Whether a phone can hold a live call: by what it last said of itself.
    pub fn live_capable(&self, principal: &str, world: &World) -> bool {
        !principal.is_empty()
            && (self.live_tokens().any(|phone| phone == principal)
                || Self::live_androids(world).any(|phone| phone == principal))
    }

    /// The Android phones a live ring reaches now: where this Host can push to them at all.
    fn reached_androids<'a>(&self, world: &World<'a>) -> Vec<&'a str> {
        if !world.can_push_android {
            return Vec::new();
        }
        let androids = Self::live_androids(world);
        androids.filter(|phone| self.receives(phone)).collect()
    }

    pub fn callback_recipient(&self, principal: &str, world: &World) -> bool {
        self.rung_live(world).iter().any(|phone| phone == principal)
    }

    /// The phones a live ring can ring: capable, and receiving rings.
    fn rung_live(&self, world: &World) -> Vec<String> {
        let tokens = self.live_tokens().filter(|phone| self.receives(phone));
        let mut phones: Vec<String> = tokens.map(str::to_string).collect();
        for phone in self.reached_androids(world) {
            if !phones.iter().any(|known| known == phone) {
                phones.push(phone.to_string());
            }
        }
        phones.retain(|phone| !phone.is_empty());
        phones
    }

    /// Whether a client may be told of a ring. A call the user started is its phone's own;
    /// the ring a phone answered stays its own; a phone that switched rings off is told of
    /// no other; and a live ring is shown only to a phone that can hold one. `principal` is
    /// empty for a client that is nobody in particular.
    pub fn sees(&self, ring: &Ring, principal: &str, world: &World) -> bool {
        if ring.of_the_user().is_some() {
            return !principal.is_empty() && ring.answered_by == principal;
        }
        if !principal.is_empty() && ring.answered_by == principal {
            return true;
        }
        if !principal.is_empty() && !self.receives(principal) {
            return false;
        }
        ring.conversation_mode != LIVE || self.live_capable(principal, world)
    }

    /// Who is sent what for one change of a ring.
    pub fn sent(&self, ring: &Ring, kind: &str, world: &World) -> Sent {
        let live = ring.conversation_mode == LIVE;
        let mut sent = Sent::default();
        if ring.of_the_user().is_some() {
            return sent;
        }
        if live {
            let phones = self.reached_androids(world);
            sent.android = phones.into_iter().map(str::to_string).collect();
        }
        if kind == "ring" {
            let tokens: Vec<String> = if live {
                let tokens = self.live_tokens().filter(|phone| self.receives(phone));
                tokens.map(str::to_string).collect()
            } else {
                let phones = self.voip.iter().filter_map(|row| row["principal"].as_str());
                let phones = phones.filter(|phone| self.receives(phone));
                phones.map(str::to_string).collect()
            };
            sent.voip = Some(tokens);
        }
        sent
    }

    /// The phones a ring that begins to ring is pushed to as an incoming call, each with
    /// the token it left for calls and its side of the push service: of the phones the ring
    /// is for ([`Rings::sent`]), those that are still paired with all rights and left a
    /// token, and no token twice. So the Python Host pushes it
    /// (`apns.send_agent_ring_voip`), since a phone taken out of the fleet is no longer
    /// rung there either (R56 in `docs/reference-defects.md` was that it was).
    pub fn rung_as_calls(&self, ring: &Ring, world: &World) -> Vec<(String, String, String)> {
        let phones = self.sent(ring, "ring", world).voip.unwrap_or_default();
        self.pushed_as_calls(&phones, world)
    }

    /// Of these phones, the ones a call push is handed over for, with the token it goes by
    /// (in the one form a token has) and its side of the push service.
    pub fn pushed_as_calls(
        &self,
        phones: &[String],
        world: &World,
    ) -> Vec<(String, String, String)> {
        let mut pushed: Vec<(String, String, String)> = Vec::new();
        for phone in phones {
            let Some((token, environment)) = self.call_token(phone) else {
                continue;
            };
            let token = crate::devices::canonical_token(token);
            let twice = pushed.iter().any(|(_, had, _)| *had == token);
            if token.is_empty() || twice || !world.a_phone(phone) {
                continue;
            }
            pushed.push((phone.clone(), token, environment.to_string()));
        }
        pushed
    }

    /// Whether a change of a ring is alerted, once its call pushes are over, and the phones
    /// that are left out of the alert every other phone gets. `reached`: how many phones a
    /// call push reached. A ring held as a live voice call has no alert, in any state of it,
    /// and a ring that reached a phone as an incoming call needs none.
    pub fn alerted(&self, ring: &Ring, reached: u64) -> Option<Vec<String>> {
        alert_follows(&ring.conversation_mode, reached).then(|| self.switched_off())
    }

    /// The token a phone left for calls, and the side of the push service it belongs to.
    pub fn call_token(&self, principal: &str) -> Option<(&str, &str)> {
        let mut rows = self.voip.iter();
        let row = rows.find(|row| row["principal"] == principal)?;
        Some((row["token"].as_str()?, row["environment"].as_str()?))
    }

    /// The phones that switched rings off for themselves: the alert of a ring leaves them
    /// out.
    pub fn switched_off(&self) -> Vec<String> {
        let phones = self.prefs.iter();
        let phones = phones.filter_map(|row| row["principal"].as_str());
        phones.map(str::to_string).collect()
    }

    /// A grant nobody joined in its minute is over: nobody can join it any more.
    pub(super) fn settle_live(&mut self, now_ms: i64, out: &mut Effects) {
        for ring in &mut self.rings {
            let Some(live) = ring.live.as_mut() else {
                continue;
            };
            if live.phase == WAITING && now_ms >= live.join_expires_at {
                live.retire(JOIN_EXPIRED);
                out.changed.rings = true;
            }
        }
    }

    /// Whether a ring is held as a live voice call: what says how it is answered.
    pub(super) fn is_live(&self, ring_id: &str) -> bool {
        let ring = self.rings.iter().find(|ring| ring.ring_id == ring_id);
        ring.is_some_and(|ring| ring.conversation_mode == LIVE)
    }

    /// `GET /calls/ring` for one client: the ring of this id, or the one that rings now,
    /// when this client may be told of it.
    pub fn look_as(
        &mut self,
        (ring_id, principal): (&str, &str),
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        self.settle(now_ms, out);
        let ring = if ring_id.is_empty() {
            // One ring rings at a time, and a call the user started never does.
            self.ringing()
        } else {
            self.rings.iter().find(|ring| ring.ring_id == ring_id)
        };
        let ring = ring.filter(|ring| self.sees(ring, principal, world));
        if !ring_id.is_empty() && ring.is_none() {
            return Err(refused(404, "unknown call"));
        }
        let ring = ring.map_or(Value::Null, |ring| ring.told(now_ms));
        Ok(json!({ "ring": ring, "settings": self.settings() }))
    }

    /// `POST /calls/ring` for a ring held as a live voice call, its first half: whether it
    /// may be placed. Its event is read and frozen with it; an event that rang before rings
    /// no second time; and it is placed only where a phone can hold such a call, never as an
    /// ordinary ring in its place.
    pub(super) fn admit_live(
        &mut self,
        asked: &Asked,
        people: People,
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Admission, Refused> {
        let reason = one_line(asked.reason, REASON_MOST);
        let opening = one_line(asked.opening_line, OPENING_MOST);
        self.settle(now_ms, out);
        let mut who = (
            String::new(),
            String::new(),
            asked.turn_agent_id.to_string(),
        );
        let mut admitted = || -> Result<Result<(super::Caller, Box<Live>), usize>, Refused> {
            let caller = Self::caller(asked, people)?;
            who = (
                caller.session.clone(),
                caller.persona.clone(),
                caller.agent_id.clone(),
            );
            if reason.is_empty() {
                return Err(refused(400, "reason required"));
            }
            if opening.is_empty() {
                return Err(refused(400, "opening_line required"));
            }
            // The opening of a live call is its voice's own: it is asked for with no other.
            if asked.opening_mode.is_some_and(|mode| mode != SAY) {
                return Err(refused(400, "live_mode_invalid"));
            }
            // And it is told that line whole, never one that was cut.
            if one_line(asked.opening_line, usize::MAX).chars().count() > OPENING_MOST {
                return Err(refused(400, "live_context_invalid"));
            }
            let context = live_call::context(asked.event_context, &opening)?;
            let event_key = format!(
                "{}:{}",
                context["source"].as_str().unwrap_or(""),
                context["event"]["id"].as_str().unwrap_or("")
            );
            let event_digest = live_call::digest(&context);
            let of_the_event = |ring: &Ring| {
                let live = ring.live.as_ref();
                ring.agent_id == caller.agent_id
                    && live.is_some_and(|live| live.event_key == event_key)
            };
            if let Some(place) = self.rings.iter().rposition(of_the_event) {
                let live = self.rings[place].live.as_ref();
                if live.is_some_and(|live| live.event_digest != event_digest) {
                    return Err(refused(409, "live_context_conflict"));
                }
                return Ok(Err(place));
            }
            self.may_ring(&caller, now_ms)?;
            if self.rung_live(world).is_empty() {
                return Err(refused(409, "live_client_required"));
            }
            let snapshot = Self::frozen(&caller, &context, world)?;
            let live = Live {
                context_digest: live_call::digest(&snapshot),
                snapshot,
                event_key,
                event_digest,
                ..Live::default()
            };
            Ok(Ok((caller, Box::new(live))))
        };
        match admitted() {
            Ok(Ok((caller, live))) => Ok(Admission::Place(Admitted {
                generation: Value::Null,
                caller,
                reason,
                opening,
                at: now_ms,
                live: Some(live),
            })),
            Ok(Err(place)) => Ok(Admission::Again(json!({
                "ok": true, "ring": self.rings[place].told(now_ms), "changed": false,
                "summary": "This event already rang the user; no second call.",
            }))),
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

    /// The snapshot of a call of this agent, from its profile as the Host has it.
    fn frozen(caller: &super::Caller, context: &Value, world: &World) -> Result<Value, Refused> {
        let profile_id = context["profile_id"].as_str().unwrap_or("");
        let held = world.profiles.profile(profile_id);
        let read = |name: &str| world.profiles.file(profile_id, name);
        let folder = Folder {
            profile: held.as_deref(),
            file: &read,
        };
        let agent = Agent {
            agent_id: &caller.agent_id,
            session: &caller.session,
            persona: &caller.persona,
        };
        let snapshot = live_call::snapshot(agent, context, folder, (world.host_id, world.has_key))?;
        // A voice that is told it has tools promises work: where nobody would do it, the
        // call is not held at all.
        if !world.runs_tools && snapshot.get("live_tools").is_some() {
            let none = "this Host does not run the tools of a live call";
            return Err(refused(501, none));
        }
        Ok(snapshot)
    }

    /// The grant of a live ring: one for each ring, and the first phone has it. `Ok(true)`
    /// when this request answered the ring, `Ok(false)` for the same phone asking again with
    /// the same request.
    fn grant(
        &mut self,
        place: usize,
        asked: (&str, Option<&str>, &Value, &Value, Option<&str>),
        minted: Minted,
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<bool, Refused> {
        let (principal, mode, request_id, client_nonce, client) = asked;
        let ring = &self.rings[place];
        let Some(live) = ring.live.as_ref() else {
            return Err(refused(409, "live_mode_conflict"));
        };
        if !live.user_call.is_empty() && live.user_call != principal {
            return Err(refused(403, "live_auth"));
        }
        if mode != Some(LIVE) {
            return Err(refused(409, "live_client_required"));
        }
        // The grant binds a paired phone with all rights: who it is, is the device.
        if !world.a_phone(principal) {
            return Err(refused(403, "live_auth"));
        }
        if ring.state == ANSWERED {
            if ring.answered_by != principal {
                return Err(not_the_owner());
            }
            // Asking again is the very same asking: its request, and its nonce.
            let (request_id, client_nonce) = intent(request_id, client_nonce)?;
            if request_id != live.request_id {
                return Err(stale());
            }
            if client_nonce != live.client_nonce {
                return Err(no_request());
            }
            return Ok(false);
        }
        if !ring.rings() {
            return Err(stale());
        }
        let (request_id, client_nonce) = intent(request_id, client_nonce)?;
        let ring = &mut self.rings[place];
        ring.state = ANSWERED.to_string();
        ring.ended_at = Some(now_ms);
        ring.answered_by = principal.to_string();
        if let Some(live) = ring.live.as_mut() {
            live.call_id = format!("alv_{}", minted.call);
            live.call_epoch = 1;
            live.phase = WAITING.to_string();
            live.attempt_id = format!("att_{}", minted.attempt);
            live.owner_nonce = minted.nonce.to_string();
            live.join_ticket = minted.ticket.to_string();
            live.join_expires_at = now_ms + JOIN_MS;
            live.request_id = request_id.to_string();
            live.client_nonce = client_nonce.to_string();
            // The model is chosen for the client that answers, and kept with the call: a
            // repeat of the answer is given the same.
            let model =
                live_call::agent_live_model(client, world.live_model, world.live_model_min_client);
            live.model = model.to_string();
        }
        out.changed.rings = true;
        let ring = &self.rings[place];
        let call_id = ring.live.as_ref().map_or("", |live| live.call_id.as_str());
        let entry = json!({
            "outcome": ANSWERED, "ring_id": ring.ring_id, "session": ring.session, "by": principal,
            "mode": LIVE, "live_call_id": call_id,
        });
        self.write(entry, now_ms, out);
        Ok(true)
    }

    /// What a phone is answered with for a live call: no call of the ordinary kind and no
    /// opening, but the grant.
    fn granted(ring: &Ring, changed: bool, host_id: &str, now_ms: i64) -> Value {
        let none = Live::default();
        let live = ring.live.as_deref().unwrap_or(&none);
        json!({
            "ok": true, "ring": ring.told(now_ms), "changed": changed, "conversation_mode": LIVE,
            "live": {
                "protocol": live_call::PROTOCOL, "host_id": host_id, "ring_id": ring.ring_id,
                "agent_id": ring.agent_id, "call_id": live.call_id, "call_epoch": live.call_epoch,
                "attempt_id": live.attempt_id, "context_digest": live.context_digest,
                "provider": live_call::PROVIDER, "model": live.running(), "voice": live_call::VOICE,
                "owner_nonce": live.owner_nonce, "join_ticket": live.join_ticket,
                "expires_at": live.join_expires_at, "join_consumed": live.join_consumed,
                "opening_owner": "live",
            },
        })
    }

    /// `POST /calls/ring/answer` for a live ring: the phone that asks to hold it live is
    /// given its grant. The same phone asking again is given the same, or what its call has
    /// become; another phone, and another way of answering, is refused.
    pub(super) fn answer_live(
        &mut self,
        asked: &Answer,
        (calls, minted): (&Calls, Minted),
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        let mode = live_call::mode_of(asked.conversation_mode, None)?;
        self.settle(now_ms, out);
        let place = self.place_of(asked.ring_id)?;
        let wanted = (
            asked.principal,
            mode,
            asked.request_id,
            asked.client_nonce,
            asked.client,
        );
        let fresh = self.grant(place, wanted, minted, world, now_ms, out)?;
        let ring = &self.rings[place];
        if fresh {
            Self::announce(ring, ANSWERED, now_ms, out);
        } else {
            // The owner's repeat is told what its call is now, never a grant it cannot use.
            let live = ring.live.as_deref();
            if live.is_some_and(|live| live.phase == SWITCHED) {
                let call = calls.by_id(&ring.call_id, now_ms).unwrap_or(Value::Null);
                let mut answer = Self::answered(ring, call, false, None, now_ms);
                answer["conversation_mode"] = json!(super::CLASSIC);
                return Ok(answer);
            }
            if !live.is_some_and(Live::holds) {
                return Err(stale());
            }
        }
        Ok(Self::granted(ring, fresh, world.host_id, now_ms))
    }

    /// `POST /calls/live`: the user calls an agent that takes calls live. The call is this
    /// phone's own: placed already answered, bound to what the phone named it by, announced
    /// to nobody. The same request is answered with the same grant.
    pub fn user_call(
        &mut self,
        (principal, agent, client): (&str, &Value, Option<&str>),
        (request_id, client_nonce): (&Value, &Value),
        known: &dyn Fn(&str) -> Option<super::Caller>,
        world: &World,
        (new_id, minted, now_ms): (&str, Minted, i64),
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        if !world.a_phone(principal) {
            return Err(refused(403, "live_auth"));
        }
        let (asked_as, nonce) = intent(request_id, client_nonce)?;
        let agent = live_call::request_id(agent).ok_or_else(no_request)?;
        let caller = known(agent).ok_or_else(|| refused(404, "unknown agent"))?;
        self.settle(now_ms, out);
        let of_this_request = |ring: &Ring| {
            let live = ring.live.as_ref();
            live.is_some_and(|live| live.user_call == principal && live.request_id == asked_as)
        };
        if let Some(place) = self.rings.iter().rposition(of_this_request) {
            let ring = &self.rings[place];
            let none = Live::default();
            let live = ring.live.as_deref().unwrap_or(&none);
            if ring.agent_id != agent || live.client_nonce != nonce {
                return Err(no_request());
            }
            if live.retired_reason == JOIN_EXPIRED {
                return Err(refused(409, JOIN_EXPIRED));
            }
            if !live.holds() {
                return Err(stale());
            }
            return Ok(Self::granted(ring, false, world.host_id, now_ms));
        }
        let unavailable = || refused(409, "live_context_unavailable");
        let profile_id = world.profiles.takes_calls(&caller.session)?;
        let profile_id = profile_id.ok_or_else(unavailable)?;
        let held = world.profiles.profile(&profile_id);
        let who = Agent {
            agent_id: &caller.agent_id,
            session: &caller.session,
            persona: &caller.persona,
        };
        let moment = live_call::moment(now_ms);
        let context = live_call::user_call(held.as_deref(), &profile_id, who, request_id, &moment)?;
        let holds_one = |ring: &Ring| {
            ring.answered_by == principal && ring.live.as_ref().is_some_and(|live| live.holds())
        };
        if self.rings.iter().any(holds_one) {
            return Err(refused(409, "live_call_active"));
        }
        // The user calling an agent is no ring of the agent's: only that it can be spoken
        // to, and that nothing rings now.
        if !caller.can_call {
            return Err(refused(403, "this agent cannot be called"));
        }
        if self.rings.iter().any(Ring::rings) {
            return Err(refused(409, "the user is already being called"));
        }
        if !self.live_capable(principal, world) {
            return Err(refused(409, "live_client_required"));
        }
        let snapshot = Self::frozen(&caller, &context, world)?;
        let live = Live {
            context_digest: live_call::digest(&snapshot),
            snapshot,
            user_call: principal.to_string(),
            ..Live::default()
        };
        self.rings.push(Ring {
            opening_mode: String::new(),
            generation: Value::Null,
            opening: Value::Null,
            ring_id: format!("rng_{new_id}"),
            session: caller.session,
            persona: caller.persona,
            agent_id: caller.agent_id,
            reason: "User-initiated call".to_string(),
            opening_line: context["opening_text"].as_str().unwrap_or("").to_string(),
            opening_audio_url: String::new(),
            state: RINGING.to_string(),
            created_at: now_ms,
            expires_at: now_ms + RING_MS,
            ended_at: None,
            answered_by: String::new(),
            call_id: String::new(),
            conversation_mode: LIVE.to_string(),
            live: Some(Box::new(live)),
        });
        if self.rings.len() > KEPT {
            let dropped = self.rings.len() - KEPT;
            self.rings.drain(..dropped);
        }
        let place = self.rings.len() - 1;
        // Granted as an answer is: its model chosen for the client that asks.
        let wanted = (principal, Some(LIVE), request_id, client_nonce, client);
        if let Err(why) = self.grant(place, wanted, minted, world, now_ms, out) {
            // A call that is not granted was never placed.
            self.rings.pop();
            return Err(why);
        }
        Ok(Self::granted(
            &self.rings[place],
            true,
            world.host_id,
            now_ms,
        ))
    }

    /// The call of this id, when a ring has one.
    fn call(&self, call_id: &str) -> Option<usize> {
        let of_the_call = |ring: &Ring| {
            let live = ring.live.as_ref();
            !call_id.is_empty() && live.is_some_and(|live| live.call_id == call_id)
        };
        self.rings.iter().position(of_the_call)
    }

    /// The first thing a phone says on the socket of a call: its join, with the grant. Every
    /// part of it is held against the ring before anything is opened, and its ticket is used
    /// up for good, whatever becomes of the session. `Err`: the code it is refused with.
    pub fn join(
        &mut self,
        principal: &str,
        event: &Value,
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Joined, &'static str> {
        const CONFLICT: &str = "live_owner_conflict";
        const STALE: &str = "live_call_stale";
        const NAMED: [&str; 9] = [
            "protocol",
            "host_id",
            "ring_id",
            "call_id",
            "call_epoch",
            "attempt_id",
            "context_digest",
            "owner_nonce",
            "join_ticket",
        ];
        let said = event.as_object().ok_or(CONFLICT)?;
        if event["type"] != "agent_live.join" || !NAMED.iter().all(|name| said.contains_key(*name))
        {
            return Err(CONFLICT);
        }
        let place = self.call(event["call_id"].as_str().unwrap_or(""));
        let place = place.ok_or(CONFLICT)?;
        let ring = &self.rings[place];
        let none = Live::default();
        let live = ring.live.as_deref().unwrap_or(&none);
        let nonce = event["owner_nonce"].as_str().ok_or(CONFLICT)?;
        if ring.answered_by != principal
            || !same(&live.owner_nonce, nonce)
            || event["attempt_id"] != live.attempt_id.as_str()
        {
            return Err(CONFLICT);
        }
        // What is no number is a join that is not well made, and not one that came late.
        let protocol = counted(&event["protocol"]).ok_or(CONFLICT)?;
        let epoch = counted(&event["call_epoch"]).ok_or(CONFLICT)?;
        if protocol != live_call::PROTOCOL
            || event["host_id"] != world.host_id
            || event["ring_id"] != ring.ring_id.as_str()
            || epoch != live.call_epoch
            || event["context_digest"] != live.context_digest.as_str()
        {
            return Err(STALE);
        }
        let ticket = event["join_ticket"].as_str().ok_or(CONFLICT)?;
        if !same(&live.join_ticket, ticket) {
            return Err(CONFLICT);
        }
        if live.join_consumed {
            return Err("live_join_consumed");
        }
        if live.retired_reason == JOIN_EXPIRED {
            return Err(JOIN_EXPIRED);
        }
        if live.phase != WAITING {
            return Err(STALE);
        }
        if now_ms >= live.join_expires_at {
            return Err(JOIN_EXPIRED);
        }
        if !(world.device_ok)(principal) {
            return Err("live_auth");
        }
        let joined = Joined {
            ring_id: ring.ring_id.clone(),
            agent_id: ring.agent_id.clone(),
            call_id: live.call_id.clone(),
            call_epoch: live.call_epoch,
            attempt_id: live.attempt_id.clone(),
            context_digest: live.context_digest.clone(),
            snapshot: live.snapshot.clone(),
            principal: principal.to_string(),
            model: live.running().to_string(),
        };
        if let Some(live) = self.rings[place].live.as_mut() {
            live.join_consumed = true;
            live.phase = CONNECTING.to_string();
        }
        out.changed.rings = true;
        let entry = json!({
            "outcome": "live_joined", "ring_id": joined.ring_id, "by": principal,
            "live_call_id": joined.call_id,
        });
        self.write(entry, now_ms, out);
        Ok(joined)
    }

    /// The call a control of its phone is about. Whatever does not fit (the call, the phone,
    /// the nonce, the attempt) is refused in one way, so that nobody learns of a call that is
    /// not theirs.
    fn owned(&self, principal: &str, data: &Value) -> Result<usize, Refused> {
        let named = |name: &str| data[name].as_str().filter(|text| !text.is_empty());
        let (Some(call_id), Some(nonce), Some(attempt)) =
            (named("call_id"), named("owner_nonce"), named("attempt_id"))
        else {
            return Err(no_request());
        };
        let place = self.call(call_id).ok_or_else(not_the_owner)?;
        let ring = &self.rings[place];
        let none = Live::default();
        let live = ring.live.as_deref().unwrap_or(&none);
        if ring.answered_by != principal
            || !same(&live.owner_nonce, nonce)
            || live.attempt_id != attempt
        {
            return Err(not_the_owner());
        }
        Ok(place)
    }

    /// Whether the call of this id and epoch still has its authority: what its session asks
    /// before it does anything for the call.
    pub fn holds(&self, call_id: &str, call_epoch: i64) -> bool {
        let live = self
            .call(call_id)
            .and_then(|place| self.rings[place].live.as_deref());
        live.is_some_and(|live| live.call_epoch == call_epoch && live.holds())
    }

    /// The session of a call writes on its ring, while the call has its authority.
    fn session(
        &mut self,
        (call_id, call_epoch): (&str, i64),
        change: impl FnOnce(&mut Live) -> bool,
        out: &mut Effects,
    ) -> bool {
        if !self.holds(call_id, call_epoch) {
            return false;
        }
        let live = self
            .call(call_id)
            .and_then(|place| self.rings[place].live.as_mut());
        let changed = live.is_some_and(|live| change(live));
        out.changed.rings |= changed;
        changed
    }

    /// The voice of a call is given its opening: once. `false`: it was given before, or the
    /// call has no authority any more, and the opening is not given.
    pub fn opening(&mut self, call: (&str, i64), out: &mut Effects) -> bool {
        let consume = |live: &mut Live| {
            if live.opening_consumed {
                return false;
            }
            live.opening_consumed = true;
            live.phase = READY.to_string();
            true
        };
        self.session(call, consume, out)
    }

    /// The voice took the opening: the call is under way.
    pub fn under_way(&mut self, call: (&str, i64), out: &mut Effects) -> bool {
        let activate = |live: &mut Live| {
            live.phase = ACTIVE.to_string();
            true
        };
        self.session(call, activate, out)
    }

    /// The user's sound went to the voice.
    pub fn heard(&mut self, call: (&str, i64), out: &mut Effects) -> bool {
        let forward = |live: &mut Live| {
            live.audio_forwarded = true;
            true
        };
        self.session(call, forward, out)
    }

    /// The session of a call ends it: its authority is retired on the ring first.
    pub fn retired(&mut self, call: (&str, i64), reason: &str, out: &mut Effects) -> bool {
        self.session(call, |live| live.retire(reason), out)
    }

    /// When a Host starts, no call of an earlier one has a session: what was joined then is
    /// over (the Python Host's `agent_rings.recover_joined_live_calls`). A call that was
    /// granted and not yet joined keeps its minute. Which calls were retired so.
    pub fn orphaned(&mut self, out: &mut Effects) -> Vec<String> {
        let mut ended = Vec::new();
        for ring in &mut self.rings {
            let Some(live) = ring.live.as_mut() else {
                continue;
            };
            if live.holds() && live.phase != WAITING && live.retire(HOST_RESTART) {
                ended.push(live.call_id.clone());
                out.changed.rings = true;
            }
        }
        ended
    }

    /// `POST /calls/ring/live/close`: the phone hangs up. The call is retired on its ring
    /// first; its session, named beside the answer, is to be stopped after. The request that
    /// closed a call may be asked again and is answered the same; any other is too late.
    pub fn close(
        &mut self,
        principal: &str,
        data: &Value,
        world: &World,
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<(Value, String), Refused> {
        if !world.a_phone(principal) {
            return Err(refused(403, "live_auth"));
        }
        if !data.is_object() {
            return Err(no_request());
        }
        let place = self.owned(principal, data)?;
        let epoch = live_call::epoch(data.get("call_epoch"))?;
        let reason = data["reason"].as_str().unwrap_or("");
        let request_id = live_call::request_id(&data["request_id"]);
        let (true, Some(request_id)) = (live_call::CLOSE_REASONS.contains(&reason), request_id)
        else {
            return Err(no_request());
        };
        let fingerprint =
            json!({ "request_id": request_id, "call_epoch": epoch, "reason": reason });
        let ring_id = self.rings[place].ring_id.clone();
        let Some(live) = self.rings[place].live.as_mut() else {
            return Err(not_the_owner());
        };
        match &live.close_receipt {
            Some(receipt) => {
                // Only the request that closed it is answered again, and as it was made.
                if receipt["request_id"] != request_id {
                    return Err(stale());
                }
                if *receipt != fingerprint {
                    return Err(no_request());
                }
            }
            None if epoch != live.call_epoch => return Err(stale()),
            None => {}
        }
        let mut changed = live.retire(reason);
        // A switch to an ordinary call that has not started it yet is called off.
        if !live.fallback_request_id.is_empty() && live.phase == RETIRED && !live.fallback_cancelled
        {
            live.fallback_cancelled = true;
            changed = true;
        }
        if live.close_receipt.is_none() && live.phase == RETIRED {
            live.close_receipt = Some(fingerprint);
            changed = true;
        }
        let call_id = live.call_id.clone();
        let closed_at = live
            .close_receipt
            .as_ref()
            .map(|receipt| receipt["call_epoch"].clone());
        let answer = json!({
            "ok": true, "call_id": call_id,
            "call_epoch": closed_at.unwrap_or_else(|| json!(live.call_epoch)),
            "state": if live.phase == SWITCHED { SWITCHED } else { RETIRED },
        });
        if changed {
            out.changed.rings = true;
            let entry = json!({
                "outcome": "live_closed", "ring_id": ring_id, "by": principal,
                "live_call_id": call_id, "reason": reason,
            });
            self.write(entry, now_ms, out);
        }
        Ok((answer, call_id))
    }

    /// `POST /calls/ring/live/fallback`, its first half: the one switch of a live call to an
    /// ordinary one, allowed before its voice was given its opening and before the user was
    /// heard, with the phone's word that its sound has stopped. The call is retired for it
    /// and its epoch moves on; its session is stopped next, and only then the ordinary call
    /// started ([`Rings::switched`]). A call the user started is never switched.
    pub fn switch(
        &mut self,
        principal: &str,
        data: &Value,
        (calls, world): (&Calls, &World),
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Claim, Refused> {
        if !world.a_phone(principal) {
            return Err(refused(403, "live_auth"));
        }
        if !data.is_object() {
            return Err(no_request());
        }
        let place = self.owned(principal, data)?;
        if self.rings[place].of_the_user().is_some() {
            return Err(not_allowed());
        }
        let epoch = live_call::epoch(data.get("expected_epoch"))?;
        let request_id = live_call::request_id(&data["request_id"]);
        let (true, Some(request_id)) = (data["audio_stopped"] == true, request_id) else {
            return Err(no_request());
        };
        let ring = &self.rings[place];
        let none = Live::default();
        let live = ring.live.as_deref().unwrap_or(&none);
        if live.phase == SWITCHED {
            if request_id != live.fallback_request_id {
                return Err(not_allowed());
            }
            let call = calls.by_id(&ring.call_id, now_ms).unwrap_or(Value::Null);
            let mut answer = Self::answered(ring, call, false, None, now_ms);
            answer["conversation_mode"] = json!(super::CLASSIC);
            return Ok(Claim::Before(answer));
        }
        // A call that a Host's start ended is not switched: its phone calls anew.
        if live.retired_reason == HOST_RESTART {
            return Err(not_allowed());
        }
        // Claimed before, by a switch under way or one that failed; or the owner hung up.
        if !live.fallback_request_id.is_empty() {
            return Err(not_allowed());
        }
        let hung_up = live.close_receipt.as_ref();
        if hung_up.is_some_and(|receipt| receipt["reason"] == "user_hangup") {
            return Err(not_allowed());
        }
        if epoch != live.call_epoch {
            return Err(stale());
        }
        if live.opening_consumed || live.audio_forwarded {
            return Err(not_allowed());
        }
        let Some(live) = self.rings[place].live.as_mut() else {
            return Err(not_allowed());
        };
        if !live.retire("fallback") && live.phase != RETIRED {
            return Err(not_allowed());
        }
        live.retired_reason = "fallback".to_string();
        live.call_epoch = epoch + 1;
        live.fallback_request_id = request_id.to_string();
        out.changed.rings = true;
        Ok(Claim::Stop {
            call_id: live.call_id.clone(),
        })
    }

    /// `POST /calls/ring/live/fallback`, its second half, once the session of the call was
    /// stopped: `settled` says whether it ended for certain. Only then the ordinary call
    /// starts, under the ring's own id, with the opening line voiced at `opening_audio_url`.
    /// A close in between called the switch off.
    pub fn switched(
        &mut self,
        calls: &mut Calls,
        (principal, data): (&str, &Value),
        (settled, opening_audio_url): (bool, &str),
        with: (&[Contact], &str),
        at: calls::At,
        (out, of_calls): (&mut Effects, &mut calls::Effects),
    ) -> Result<Value, Refused> {
        if !settled {
            return Err(refused(503, "live_unavailable"));
        }
        let now_ms = at.now_ms;
        let call_id = data["call_id"].as_str().unwrap_or("");
        let place = self.call(call_id).ok_or_else(not_allowed)?;
        let ring = &self.rings[place];
        let none = Live::default();
        let live = ring.live.as_deref().unwrap_or(&none);
        if data["request_id"] != live.fallback_request_id.as_str()
            || live.phase != RETIRED
            || live.fallback_cancelled
        {
            return Err(not_allowed());
        }
        let placed = json!({
            "ring_id": ring.ring_id, "reason": ring.reason, "opening_line": ring.opening_line,
        });
        let asker = Asker {
            caller: principal,
            by: &ring.session,
            ..Asker::default()
        };
        let agents = [ring.session.clone()];
        let started = calls.start(
            asker,
            (&agents, &ring.ring_id, Some(placed)),
            with,
            at,
            of_calls,
        )?;
        let call = started["call"].clone();
        let live_call_id = live.call_id.clone();
        let ring = &mut self.rings[place];
        ring.call_id = call["call_id"].as_str().unwrap_or("").to_string();
        if ring.opening_audio_url.is_empty() {
            ring.opening_audio_url = opening_audio_url.to_string();
        }
        if let Some(live) = ring.live.as_mut() {
            live.phase = SWITCHED.to_string();
        }
        out.changed.rings = true;
        let ring = self.rings[place].clone();
        let entry = json!({
            "outcome": "live_fallback", "ring_id": ring.ring_id, "by": principal,
            "live_call_id": live_call_id, "call_id": ring.call_id,
        });
        self.write(entry, now_ms, out);
        let mut answer = Self::answered(&ring, call, true, None, now_ms);
        answer["conversation_mode"] = json!(super::CLASSIC);
        Ok(answer)
    }

    /// Whether a phone that asks about its own switch may: a paired phone with all rights.
    fn its_own_switch(principal: &str, world: &World) -> Result<(), Refused> {
        if world.a_phone(principal) {
            Ok(())
        } else {
            Err(refused(403, "ring_device_required"))
        }
    }

    /// What a phone is told of its own switch. `unbound`: how many alert tokens nobody is
    /// known to own: while there is one, a phone that switched rings off may still be told
    /// of a ring by it, and is told so.
    fn switch_state(&self, principal: &str, unbound: usize) -> Value {
        let off = !self.receives(principal);
        let suppressed = off && unbound == 0;
        let state = match (off, suppressed) {
            (false, _) => "on",
            (true, true) => "off",
            (true, false) => "partial",
        };
        let mut answer = json!({
            "ok": true, "receive_rings": !off, "alerts_suppressed": suppressed, "state": state,
        });
        if off && !suppressed {
            answer["reason"] = json!("unbound_alert_tokens");
            answer["unbound_alert_tokens"] = json!(unbound);
        }
        answer
    }

    /// `GET /calls/ring/device`: whether this phone receives rings.
    pub fn receiving(
        &self,
        principal: &str,
        world: &World,
        unbound: usize,
    ) -> Result<Value, Refused> {
        Self::its_own_switch(principal, world)?;
        Ok(self.switch_state(principal, unbound))
    }

    /// `POST /calls/ring/device`: a phone switches rings on or off for itself alone.
    pub fn receive(
        &mut self,
        principal: &str,
        said: &Value,
        (world, unbound): (&World, usize),
        now_ms: i64,
        out: &mut Effects,
    ) -> Result<Value, Refused> {
        Self::its_own_switch(principal, world)?;
        let fields = said.as_object().filter(|fields| fields.len() == 1);
        let Some(receive) = fields.and_then(|fields| fields.get("receive_rings")?.as_bool()) else {
            return Err(refused(400, "ring_device_invalid"));
        };
        self.prefs.retain(|row| row["principal"] != principal);
        if !receive {
            self.prefs
                .push(json!({ "principal": principal, "updated_at": now_ms }));
        }
        out.changed.prefs = true;
        Ok(self.switch_state(principal, unbound))
    }
}
