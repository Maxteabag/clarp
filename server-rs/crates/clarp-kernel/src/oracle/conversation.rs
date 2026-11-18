//! A call of the Oracle: one conversation, with the Host between a phone and a voice.
//!
//! The phone sends sound and a few words of its own ([`super::phone`]). The voice model sends
//! sound, what it heard and what it said, and a word whenever it wants something done: it
//! holds no tools. The Host forwards the sound, keeps what was said as the talk of a thread,
//! has a router turn what the user asked into work for agents ([`Route`], in `routing.rs`),
//! and tells the voice what came of that work: between two turns of the talk and never into
//! one, a long answer in numbered parts ([`super::relay`]), each after the one before was
//! spoken, with a cue of sound for the phone ([`super::earcons`]). It tells the voice of what
//! else waits for the user ([`super::attention`]), one thing at a time, and holds everything
//! back while the call is put through to an agent.
//!
//! This is one machine with no clock, socket, store or model of its own. It is told what each
//! side said, that time passed and what the book of delegations has; it says what each side
//! is to be told through a [`Wire`], and what is to be kept and written is asked of it after.
//! One task owns it: what the Python Host orders with locks and threads is here the order in
//! which the machine is called.
//!
//! The Python Host's `oracle_live_stable.Conversation`, held to it by
//! `tests/fixtures/oracle-conversation-vectors.json`.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::delegations::{Delegation, Status};
use super::relay::{self, APPEND_CHARS, AUTO_PARTS, Handed, Relay, Said};
use super::{attention, earcons, phone, said, voice};
use crate::python;

/// How long the voice must have been silent before the phone is told its reply is over.
pub const QUIET_AFTER_MS: i64 = 1500;
/// How long the user must have been silent before a result, or what waits for them, is told.
pub const RESULT_SILENCE_MS: i64 = 4500;
/// A part the voice never began to speak stops holding back newer results after this long.
pub const RELAY_STALL_MS: i64 = 15_000;
/// A cue waits until the voice has been silent this long.
pub const CUE_QUIET_MS: i64 = 600;
/// How often it is looked what waits for the user, and how long after one thing was told
/// nothing else is.
pub const ATTENTION_EVERY_MS: i64 = 1000;
pub const ATTENTION_APART_MS: i64 = 4000;
/// A call that begins within this long of the one before goes on as that call.
pub const RESUME_WITHIN_MS: i64 = 30_000;
/// Silence of the voice is forwarded for this long after sound of it.
pub const HANGOVER_MS: i64 = 1200;
/// How long the user must have stopped talking before what they asked is routed.
pub const ROUTE_QUIET_MS: i64 = 1000;
/// A result is not told so soon after the voice spoke, or after the Host told it something.
const AFTER_OUTPUT_MS: i64 = 800;
const AFTER_APPEND_MS: i64 = 4000;
/// Words that follow within this long are one fragment, and how long one may grow.
const FRAGMENT_GAP_MS: i64 = 1100;
const FRAGMENT_SIGNS: usize = 32_000;
/// How many fragments of the talk are kept, and of how many relays where they stopped.
const FRAGMENTS_KEPT: usize = 30;
const CURSORS_KEPT: usize = 50;
/// How many of the contact's answers are kept as told under a delegation, the newest: so
/// that the same answer under another delegation in a later call is not told twice, while
/// what a thread keeps stays bounded.
const FINDINGS_KEPT: usize = 500;
/// How loud sound must be to count as somebody speaking: the mean of its samples' squares.
const AUDIBLE: f64 = 10_000.0;
/// Before anything happened.
const NEVER: i64 = i64::MIN / 4;

const STOP_SPEAKING: &str = "Stop speaking now and listen. Do not cancel agent work.";
const NOTHING_KEPT: &str = "There is no stored agent reply to read in this call yet.";
const UNSUPPORTED: &str = "Unsupported Oracle v2 command";
pub(super) const ROUTE_FAILED: &str =
    "Oracle v2 could not complete a backend request. Please try again.";

/// The moment something happens: on the clock a call is timed by, and on the wall's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Now {
    pub ms: i64,
    pub wall_ms: i64,
}

/// What a conversation tells the two sides of its call.
pub trait Wire {
    fn voice(&mut self, said: Value);
    fn phone(&mut self, said: Value);
}

/// How what the user asks becomes work: a router decides, or everything goes to the contact.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strategy {
    Operator,
    DirectContact,
}

impl Strategy {
    /// The strategy of this name; a call that names none has the router decide.
    pub fn named(name: Option<&str>) -> Option<Self> {
        match name {
            None | Some("operator") => Some(Self::Operator),
            Some("direct_contact") => Some(Self::DirectContact),
            Some(_) => None,
        }
    }

    pub fn word(self) -> &'static str {
        match self {
            Self::Operator => "operator",
            Self::DirectContact => "direct_contact",
        }
    }
}

/// A piece of the talk: what one side said until the other spoke or a pause was made.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fragment {
    /// `user` or `assistant`.
    pub role: String,
    #[serde(default)]
    pub text: String,
    /// The connection it was said on.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_session: Option<String>,
    #[serde(default)]
    pub end_ms: i64,
    /// The number the talk had when it last grew; none on what was kept long ago.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub at: Option<i64>,
}

impl Fragment {
    pub(super) fn of_user(&self) -> bool {
        self.role == "user"
    }

    pub(super) fn says_something(&self) -> bool {
        !python::strip(&self.text).is_empty()
    }
}

/// The names the Host makes for what it tells the voice: each once.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ids {
    stem: String,
    made: u64,
}

impl Ids {
    /// Names that begin with this, numbered from one.
    pub fn new(stem: &str) -> Self {
        Self {
            stem: stem.to_string(),
            made: 0,
        }
    }

    fn next(&mut self) -> String {
        self.made += 1;
        format!("{}{}", self.stem, self.made)
    }
}

/// The thread a call belongs to: what was kept of it.
#[derive(Debug, Clone, Copy)]
pub struct Thread<'a> {
    pub thread_id: &'a str,
    /// What was kept of the talk when it last stopped; null on a new thread.
    pub saved: &'a Value,
    /// The delegations of this thread.
    pub work: &'a [String],
    /// What the user added to the thread for the voice to know.
    pub contexts: &'a [Value],
    /// What waits for the user that an earlier call of this thread told its voice of, each
    /// as its kind, a colon and its id.
    pub told: &'a [String],
}

/// What a call begins with.
#[derive(Debug, Clone)]
pub struct Begin<'a> {
    /// The connection of this call.
    pub provider_session: &'a str,
    /// Whose call it is.
    pub principal: &'a str,
    pub strategy: Strategy,
    /// The Oracle's contact, where one is chosen.
    pub contact: &'a str,
    /// What the ids of a call's own requests to its contact begin with.
    pub direct_prefix: &'a str,
    /// The thread the call belongs to.
    pub thread: Thread<'a>,
    pub ids: Ids,
}

/// What became of what a phone sent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Phoned {
    /// Nothing the Host takes: the phone was told.
    Unsupported,
    Taken,
    /// The phone ends the call; after it was put through to an agent, it names that handoff.
    Close {
        handoff_id: Option<String>,
    },
}

/// What waits for the user, as the Host has it.
#[derive(Debug, Clone, Copy)]
pub struct Waiting<'a> {
    pub decisions: &'a [Value],
    pub completions: &'a [Value],
}

/// The voice was told of something that waits for the user: to be kept, so that no later
/// call of the thread tells it again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Claim {
    pub kind: &'static str,
    pub id: String,
    pub reference_ts: i64,
}

/// An action of the router's that was begun for the words of the user: to be written, so
/// that it is not begun again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Admission {
    pub revision: i64,
    pub index: usize,
    pub call_id: String,
    pub tool: String,
    /// What the tool is handed, in the one form two writers agree on.
    pub arguments: String,
}

/// The conversation of one call.
#[derive(Debug, Clone)]
pub struct Conversation {
    pub(super) provider_session: String,
    pub(super) principal: String,
    pub(super) thread_id: String,
    contexts: Vec<Value>,
    pub(super) strategy: Strategy,
    pub(super) contact: String,
    pub(super) direct_prefix: String,
    ids: Ids,
    pub(super) fragments: Vec<Fragment>,
    pub(super) revision: i64,
    pub(super) routed_revision: i64,
    pub(super) resume_revision: i64,
    seen_sources: BTreeSet<String>,
    last_input: i64,
    last_output: i64,
    pub(super) last_transcript: i64,
    last_append: i64,
    speaking: bool,
    user_spoke_at: Option<i64>,
    opening_answered: bool,
    pub(super) narration: String,
    earcons: bool,
    progress_interval: i64,
    cues: Vec<(String, String)>,
    pub(super) owned: BTreeSet<String>,
    pub(super) background: BTreeSet<String>,
    /// The answers that were told: their last part was handed over.
    results_sent: BTreeSet<String>,
    results_seen: BTreeSet<String>,
    /// The answers a stop of their agent left untold: never told by themselves, in this call
    /// or a later one of the thread.
    silenced: BTreeSet<String>,
    pending: Vec<Delegation>,
    forwarded: Map<String, Value>,
    pub(super) relays: BTreeMap<String, Relay>,
    /// The relays that are answers of delegations, each with what its answer is known by
    /// where it is the contact's own.
    findings: BTreeMap<String, Option<String>>,
    cursors: Map<String, Value>,
    active: Option<String>,
    wanted: Vec<String>,
    seen: BTreeSet<String>,
    pub(super) routing: usize,
    pub(super) admitted: BTreeSet<i64>,
    pub(super) admissions: Vec<Admission>,
    /// What waits for the user that the voice was told of, or is not to be told of: each
    /// as its kind, a colon and its id.
    told: BTreeSet<String>,
    last_looked: Option<i64>,
    last_told: Option<i64>,
    pub(super) held: bool,
    closed: bool,
    /// The wall's time of the last moment the talk was to be kept, until it was taken.
    dirty: Option<i64>,
}

/// Whether sound is somebody speaking: PCM16, loud enough on the whole.
fn audible(sound: &[u8]) -> bool {
    let (samples, _) = sound.as_chunks::<2>();
    let squares = samples
        .iter()
        .map(|pair| f64::from(i16::from_le_bytes(*pair)).powi(2));
    #[allow(clippy::cast_precision_loss)]
    let mean = squares.sum::<f64>() / samples.len() as f64;
    !samples.is_empty() && mean > AUDIBLE
}

/// A whole number of an event, where it has one.
fn whole(value: &Value) -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    let cut = |fraction: f64| fraction as i64;
    value
        .as_i64()
        .or_else(|| value.as_f64().map(cut))
        .unwrap_or(0)
}

pub(super) fn digest(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

impl Conversation {
    /// The conversation of a call that begins now.
    pub fn begin(begin: Begin, wall_ms: i64) -> Self {
        let mut call = Self {
            provider_session: begin.provider_session.to_string(),
            principal: begin.principal.to_string(),
            thread_id: begin.thread.thread_id.to_string(),
            contexts: Vec::new(),
            strategy: begin.strategy,
            contact: begin.contact.to_string(),
            direct_prefix: begin.direct_prefix.to_string(),
            ids: begin.ids,
            fragments: Vec::new(),
            revision: 0,
            routed_revision: 0,
            resume_revision: 0,
            seen_sources: BTreeSet::new(),
            last_input: NEVER,
            last_output: NEVER,
            last_transcript: NEVER,
            last_append: NEVER,
            speaking: false,
            user_spoke_at: None,
            opening_answered: false,
            narration: "on".to_string(),
            earcons: true,
            progress_interval: 0,
            cues: Vec::new(),
            owned: BTreeSet::new(),
            background: BTreeSet::new(),
            results_sent: BTreeSet::new(),
            results_seen: BTreeSet::new(),
            silenced: BTreeSet::new(),
            pending: Vec::new(),
            forwarded: Map::new(),
            relays: BTreeMap::new(),
            findings: BTreeMap::new(),
            cursors: Map::new(),
            active: None,
            wanted: Vec::new(),
            seen: BTreeSet::new(),
            routing: 0,
            admitted: BTreeSet::new(),
            admissions: Vec::new(),
            told: begin.thread.told.iter().cloned().collect(),
            last_looked: None,
            last_told: None,
            held: false,
            closed: false,
            dirty: None,
        };
        let mut resumed = BTreeSet::new();
        let thread = begin.thread;
        let saved = thread.saved;
        let texts = |name: &str| -> BTreeSet<String> {
            let listed = saved[name].as_array().into_iter().flatten();
            listed
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        };
        // A call that begins soon after the one before goes on as that call: its work
        // is still told as it ends, and nothing waits for the user to speak first.
        let since = saved["call_active_ms"]
            .as_i64()
            .map(|active| wall_ms - active);
        if since.is_some_and(|since| (0..=RESUME_WITHIN_MS).contains(&since)) {
            resumed = texts("call_work");
            call.opening_answered = true;
        }
        let kept = saved["fragments"].as_array().map_or(&[][..], Vec::as_slice);
        let from = kept.len().saturating_sub(FRAGMENTS_KEPT);
        let kept = kept[from..].iter();
        call.fragments = kept
            .filter_map(|row| serde_json::from_value(row.clone()).ok())
            .collect();
        call.revision = saved["revision"].as_i64().unwrap_or(0);
        call.resume_revision = call.revision;
        let routed = saved["routed_revision"].as_i64();
        call.routed_revision = routed.unwrap_or(call.resume_revision);
        call.results_sent = texts("results_sent");
        // What a stop silenced, where a Host that keeps it kept it: none where one did not.
        call.silenced = texts("silenced_results");
        call.results_seen = call.results_sent.union(&call.silenced).cloned().collect();
        if let Some(forwarded) = saved["forwarded_findings"].as_object() {
            call.forwarded = forwarded.clone();
        }
        if let Some(cursors) = saved["relay_cursors"].as_object() {
            call.cursors = cursors.clone();
        }
        if saved["narration"] == "off" {
            call.narration = "off".to_string();
        }
        call.earcons = saved["earcons"] != false;
        call.owned.extend(thread.work.iter().cloned());
        call.contexts = thread.contexts.to_vec();
        // Work that was handed on before this call is told when it is asked for, and never
        // by itself.
        call.background = call.owned.difference(&resumed).cloned().collect();
        call
    }

    pub fn revision(&self) -> i64 {
        self.revision
    }

    /// Up to where the talk was routed.
    pub fn routed_revision(&self) -> i64 {
        self.routed_revision
    }

    pub fn narration(&self) -> &str {
        &self.narration
    }

    pub fn earcons(&self) -> bool {
        self.earcons
    }

    /// The Host's owner wants no cues, whatever was kept.
    pub fn without_earcons(&mut self) {
        self.earcons = false;
    }

    /// The user has spoken in this call and the voice has answered.
    pub fn opening_answered(&self) -> bool {
        self.opening_answered
    }

    pub fn fragments(&self) -> &[Fragment] {
        &self.fragments
    }

    /// How many routings are wanted or under way.
    pub fn routing(&self) -> usize {
        self.routing
    }

    /// The call is put through to an agent: the voice is told nothing new.
    pub fn held(&self) -> bool {
        self.held
    }

    /// The delegations that are this call's own to tell of, and those of them that were
    /// handed on before it.
    pub fn owned(&self) -> &BTreeSet<String> {
        &self.owned
    }

    pub fn background(&self) -> &BTreeSet<String> {
        &self.background
    }

    /// A delegation is this call's own.
    pub fn owns(&mut self, delegation_id: &str) {
        self.owned.insert(delegation_id.to_string());
    }

    pub fn results_seen(&self) -> &BTreeSet<String> {
        &self.results_seen
    }

    /// The results that wait to be told, in their order.
    pub fn pending(&self) -> impl Iterator<Item = &str> {
        self.pending.iter().map(|row| row.delegation_id.as_str())
    }

    /// The cues that wait for the voice to be quiet: each with the agent it is pitched by.
    pub fn cues(&self) -> &[(String, String)] {
        &self.cues
    }

    /// The voice is heard speaking.
    pub fn speaking(&self) -> bool {
        self.speaking
    }

    pub fn progress_interval(&self) -> i64 {
        self.progress_interval
    }

    /// The text that is being told in parts, and every text that was.
    pub fn active_relay(&self) -> Option<&Relay> {
        self.active.as_ref().and_then(|key| self.relays.get(key))
    }

    pub fn relays(&self) -> impl Iterator<Item = &str> {
        self.relays.keys().map(String::as_str)
    }

    /// The voice said that the session is over.
    pub fn closed(&self) -> bool {
        self.closed
    }

    /// What the voice wants routed next, by the id it gave: taken out of the line.
    pub fn next_wanted(&mut self) -> Option<String> {
        (!self.wanted.is_empty()).then(|| self.wanted.remove(0))
    }

    pub fn wanted(&self) -> &[String] {
        &self.wanted
    }

    /// What is to be kept of the call, where anything changed since it was last asked for.
    pub fn to_keep(&mut self) -> Option<Value> {
        let wall_ms = self.dirty.take()?;
        // Only this call's work can be told by itself in a call that goes on in its place
        // (what is older is handed on there), so only of that is kept what was told and what
        // a stop silenced: the thread is every call's with the contact, and must not grow.
        let work: BTreeSet<&String> = self.owned.difference(&self.background).collect();
        let of_work = |ids: &BTreeSet<String>| -> Vec<String> {
            let ids = ids.iter().filter(|id| work.contains(id));
            ids.cloned().collect()
        };
        let older = self.forwarded.len().saturating_sub(FINDINGS_KEPT);
        let forwarded: Map<String, Value> =
            self.forwarded.clone().into_iter().skip(older).collect();
        let silenced = of_work(&self.silenced);
        let mut kept = json!({
            "revision": self.revision,
            "fragments": self.fragments,
            "results_sent": of_work(&self.results_sent),
            "forwarded_findings": forwarded,
            "provider_session": self.provider_session,
            "relay_cursors": self.cursors,
            "narration": self.narration,
            "earcons": self.earcons,
            "routed_revision": self.routed_revision,
            "call_active_ms": wall_ms,
            "call_work": work,
        });
        if !silenced.is_empty() {
            kept["silenced_results"] = json!(silenced);
        }
        Some(kept)
    }

    /// The actions that were begun since this was last asked: to be written.
    pub fn begun(&mut self) -> Vec<Admission> {
        std::mem::take(&mut self.admissions)
    }

    /// The talk is to be kept as it is now.
    pub(super) fn checkpoint(&mut self, now: Now) {
        self.dirty = Some(now.wall_ms);
    }

    /// The call ends: when, is kept with its thread, so that a call that begins soon after
    /// goes on as this one.
    pub fn ends(&mut self, now: Now) {
        self.checkpoint(now);
    }

    /// Tells the voice something of the Host's own. A text too long for one piece is cut,
    /// and says so: long texts are relayed in parts, and this is only the last guard.
    pub fn append(&mut self, kind: &str, content: &str, wire: &mut dyn Wire) {
        let content = if content.chars().count() > APPEND_CHARS {
            let note = voice::cut_note();
            let room = APPEND_CHARS.saturating_sub(note.chars().count());
            format!("{}{note}", python::first(content, room))
        } else {
            content.to_string()
        };
        wire.voice(json!({
            "type": format!("session.{kind}.append"),
            "delegation_id": null,
            "event_id": self.ids.next(),
            "content": content,
        }));
    }

    /// What a phone sent.
    pub fn phone(&mut self, raw: &str, wire: &mut dyn Wire, now: Now) -> Phoned {
        let Some(event) = phone::event(raw) else {
            wire.phone(json!({ "type": "oracle_v2.notice", "message": UNSUPPORTED }));
            return Phoned::Unsupported;
        };
        match event["type"].as_str().unwrap_or("") {
            "oracle_v2.preferences" => {
                if let Some(seconds) = event.get("progress_interval_seconds") {
                    self.progress_interval = whole(seconds);
                }
                let narration = event["narration"].as_str().unwrap_or("");
                if !narration.is_empty() && narration != self.narration {
                    self.narration = narration.to_string();
                    if !self.held {
                        self.append("instructions", voice::narration(narration == "off"), wire);
                    }
                    self.checkpoint(now);
                }
                if let Some(earcons) = event["earcons"].as_bool()
                    && earcons != self.earcons
                {
                    self.earcons = earcons;
                    if !earcons {
                        self.cues.clear();
                    }
                    self.checkpoint(now);
                }
                wire.phone(json!({
                    "type": "oracle_v2.preferences",
                    "progress_interval_seconds": self.progress_interval,
                    "narration": self.narration,
                    "earcons": self.earcons,
                }));
                Phoned::Taken
            }
            "oracle_v2.interrupt" => {
                if let Some(relay) = self.active_mut() {
                    relay.held = true;
                }
                if !self.held {
                    self.append("instructions", STOP_SPEAKING, wire);
                }
                Phoned::Taken
            }
            "session.close" => {
                // The voice gets a plain close, whatever handoff the phone named.
                wire.voice(json!({ "type": "session.close" }));
                Phoned::Close {
                    handoff_id: event["handoff_id"].as_str().map(str::to_string),
                }
            }
            _ => {
                let sound = python::b64decode(event["audio"].as_str().unwrap_or(""));
                if sound.is_some_and(|sound| audible(&sound)) {
                    self.last_input = now.ms;
                }
                wire.voice(event);
                Phoned::Taken
            }
        }
    }

    fn active_mut(&mut self) -> Option<&mut Relay> {
        let key = self.active.as_ref()?;
        self.relays.get_mut(key)
    }

    /// What the voice sent.
    pub fn voice(&mut self, event: &Value, wire: &mut dyn Wire, now: Now) {
        match event["type"].as_str().unwrap_or("") {
            "session.output_audio.delta" => {
                let sound = python::b64decode(event["delta"].as_str().unwrap_or(""));
                if sound.is_some_and(|sound| audible(&sound)) {
                    self.last_output = now.ms;
                    self.speaking = true;
                    wire.phone(event.clone());
                } else if self.speaking && now.ms - self.last_output < HANGOVER_MS {
                    // Silence right after its words is part of how the voice speaks.
                    wire.phone(event.clone());
                }
            }
            "session.output_audio.interrupted" => {
                // The voice stopped for the user: what the phone still holds of its reply
                // is no longer the answer.
                self.speaking = false;
                if let Some(relay) = self.active_mut() {
                    relay.held = true;
                }
                wire.phone(json!({ "type": "oracle_v2.playback_flush" }));
                wire.phone(json!({ "type": "oracle_v2.quiet" }));
            }
            kind @ ("session.input_transcript.delta" | "session.output_transcript.delta") => {
                self.transcribed(kind == "session.input_transcript.delta", event, wire, now);
            }
            "session.delegation.created" => {
                let ident = event["delegation"]["id"].as_str().unwrap_or("");
                if !ident.is_empty() && self.seen.insert(ident.to_string()) {
                    self.routing += 1;
                    self.wanted.push(ident.to_string());
                }
            }
            "session.closed" => {
                self.closed = true;
                wire.phone(event.clone());
            }
            kind @ ("session.started" | "error") => {
                wire.phone(event.clone());
                if kind == "error" {
                    return;
                }
                if self.narration == "off" {
                    // As it was kept of this thread.
                    self.append("instructions", voice::narration(true), wire);
                }
                wire.phone(json!({
                    "type": "oracle_v2.context",
                    "thread_id": self.thread_id,
                    "items": self.contexts,
                    "revision": self.revision,
                }));
            }
            _ => {}
        }
    }

    /// The voice says what it heard, or what it said: the talk grows by it.
    fn transcribed(&mut self, of_user: bool, event: &Value, wire: &mut dyn Wire, now: Now) {
        // What was told twice is taken once: by its own name where it has one, else by what
        // it says.
        let source = match &event["event_id"] {
            name if python::truthy(name) => said(name),
            _ => digest(&python::canonical(event)),
        };
        if !self.seen_sources.insert(source) {
            return;
        }
        let role = if of_user { "user" } else { "assistant" };
        let text = said(&event["delta"]);
        if of_user && !python::strip(&text).is_empty() {
            self.revision += 1;
            self.last_transcript = now.ms;
            self.user_spoke_at.get_or_insert(now.ms);
        }
        let (routed, revision) = (self.routed_revision, self.revision);
        let session = self.provider_session.as_str();
        // Words go on where the same side last spoke on this connection, while those words
        // were not routed yet, soon enough after them, and not without end.
        let goes_on = self.fragments.last_mut().filter(|last| {
            last.role == role
                && last.provider_session.as_deref() == Some(session)
                && last.revision.is_none_or(|revision| revision > routed)
                && whole(&event["start_ms"]) - last.end_ms < FRAGMENT_GAP_MS
                && last.text.chars().count() + text.chars().count() <= FRAGMENT_SIGNS
        });
        if let Some(last) = goes_on {
            last.text += &text;
            last.end_ms = whole(&event["end_ms"]);
            last.revision = Some(revision);
        } else {
            self.fragments.push(Fragment {
                role: role.to_string(),
                text,
                provider_session: Some(session.to_string()),
                end_ms: whole(&event["end_ms"]),
                revision: Some(revision),
                at: Some(now.wall_ms),
            });
        }
        let over = self.fragments.len().saturating_sub(FRAGMENTS_KEPT);
        self.fragments.drain(..over);
        self.checkpoint(now);
        wire.phone(event.clone());
    }

    /// What an agent was told to leave is not told as a result, in this call or a later one of
    /// the thread; it is still read when it is asked for. `rows`: the delegations of this call,
    /// as the book had them just before: a result that is over only since is told as any other.
    pub fn supersede(&mut self, session: &str, rows: &[Delegation], now: Now) {
        let finished = rows.iter().filter(|row| !row.status.active());
        let stopped: BTreeSet<String> = self
            .pending
            .iter()
            .chain(finished)
            .filter(|row| row.session == session && row.status != Status::Cancelled)
            .map(|row| row.delegation_id.clone())
            .filter(|id| !self.results_sent.contains(id) && !self.background.contains(id))
            .collect();
        self.silenced.extend(stopped.iter().cloned());
        self.results_seen.extend(stopped.iter().cloned());
        self.pending.retain(|row| row.session != session);
        let active = self.active.as_ref().filter(|key| stopped.contains(*key));
        if let Some(relay) = active.and_then(|key| self.relays.get_mut(key)) {
            relay.held = true;
        }
        if !stopped.is_empty() {
            self.checkpoint(now);
        }
    }

    /// The user has spoken in this call and the voice has answered: said once it is so.
    fn opening_done(&mut self) -> bool {
        if !self.opening_answered && self.user_spoke_at.is_some_and(|at| self.last_output > at) {
            self.opening_answered = true;
        }
        self.opening_answered
    }

    /// Nobody speaks, nothing is routed, no part of a text is being read, and the user has
    /// been silent for a while: something new can be told without sounding like an answer.
    fn between_turns(&self, speaking: bool, now: Now) -> bool {
        !speaking
            && self.routing == 0
            && !self.relay_busy(now)
            && now.ms - self.last_input.max(self.last_transcript) >= RESULT_SILENCE_MS
    }

    /// Time passed, the first half: the phone is told when the voice fell silent, and cues
    /// that waited for that are played. Says whether this is the moment to tell the voice of
    /// something that waits for the user: then the Host reads what waits, and hands it to
    /// the second half ([`Self::ticked`]).
    pub fn tick(&mut self, wire: &mut dyn Wire, now: Now) -> bool {
        if self.speaking && now.ms - self.last_output > QUIET_AFTER_MS {
            self.speaking = false;
            wire.phone(json!({ "type": "oracle_v2.quiet" }));
        }
        self.flush_cues(wire, now);
        if self.held || !self.opening_done() {
            return false;
        }
        if self
            .last_looked
            .is_some_and(|at| now.ms - at < ATTENTION_EVERY_MS)
        {
            return false;
        }
        self.last_looked = Some(now.ms);
        self.between_turns(self.speaking, now)
            && self
                .last_told
                .is_none_or(|at| now.ms - at >= ATTENTION_APART_MS)
    }

    /// Time passed, the second half. `rows`: the delegations of this call, as the book has
    /// them now, in the order they are to be told in. `waiting`: what waits for the user,
    /// where the first half asked for it and the Host could read it. What is said back is
    /// to be kept.
    pub fn ticked(
        &mut self,
        rows: &[Delegation],
        waiting: Option<Waiting>,
        wire: &mut dyn Wire,
        now: Now,
    ) -> Option<Claim> {
        if self.held {
            return None;
        }
        let claim = waiting.and_then(|waiting| self.tell_what_waits(waiting, wire, now));
        for row in rows.iter().filter(|row| !row.status.active()) {
            if !self.results_seen.insert(row.delegation_id.clone()) {
                continue;
            }
            let called_off = row.status == Status::Cancelled;
            if !called_off && !self.background.contains(&row.delegation_id) {
                self.pending.push(row.clone());
            }
        }
        // A part that is handed over now is something the Host told the voice: a result
        // waits its while after it, as after anything else.
        self.advance_relay(wire, now);
        // A guard of time, not a turn the voice declared: a result is told when nothing
        // else is going on.
        if self.pending.is_empty()
            || !self.between_turns(false, now)
            || now.ms - self.last_output < AFTER_OUTPUT_MS
            || now.ms - self.last_append < AFTER_APPEND_MS
        {
            return claim;
        }
        let row = self.pending.remove(0);
        self.last_append = now.ms;
        let id = row.delegation_id.clone();
        let Some(next) = self.relay_for(&row) else {
            // Its names leave no room for a text: nothing of it can be told.
            self.results_sent.insert(id);
            self.checkpoint(now);
            return claim;
        };
        // Whether it is told is decided before its cue. An answer the contact gave once is
        // told once, under whatever delegations it ended.
        let finding = self.findings.get(&id).cloned().flatten();
        let previous = finding.as_ref().and_then(|key| self.forwarded.get(key));
        let told_before = previous.is_some_and(|told| python::truthy(told) && *told != json!(id));
        let remaining = self.relays.get(&id).map_or(0, Relay::remaining);
        if told_before || remaining == 0 {
            // Told under another delegation, or read to its end on request.
            self.results_sent.insert(id);
        } else {
            if row.status == Status::Completed {
                self.cue("result", "", wire, now);
            }
            // An answer read part of the way is gone on with where the reading stopped.
            self.relay(&id, next, (false, next > 0), wire, now);
            if let Some(key) = finding {
                self.forwarded.insert(key, json!(id));
            }
        }
        self.checkpoint(now);
        claim
    }

    /// Tells the voice of one thing that waits for the user, and no more: the first it was
    /// not told of yet.
    fn tell_what_waits(
        &mut self,
        waiting: Waiting,
        wire: &mut dyn Wire,
        now: Now,
    ) -> Option<Claim> {
        let decisions = waiting.decisions.iter().map(|row| ("decision", row));
        let completions = waiting.completions.iter().map(|row| ("completion", row));
        for (kind, row) in decisions.chain(completions) {
            let (id, reference) = if kind == "decision" {
                (said(&row["decision_id"]), &row["updated_at"])
            } else {
                (said(&row["source_message_id"]), &row["reference_ts"])
            };
            let key = format!("{kind}:{id}");
            // What this call or an earlier one of the thread told its voice of is not told
            // again; and what is too old to bring up is not told at all: the agent's
            // conversation still has it.
            if !self.told.insert(key) || kind == "completion" && python::truthy(&row["stale"]) {
                continue;
            }
            let text = if kind == "decision" {
                attention::decision_text(row)
            } else {
                attention::completion_text(row)
            };
            self.append("thinking", &text, wire);
            self.last_told = Some(now.ms);
            return Some(Claim {
                kind,
                id,
                reference_ts: whole(reference),
            });
        }
        None
    }

    /// The relay of a delegation's answer, made where there is none yet, with where an
    /// earlier call stopped in it. Says the part it would go on with; `None`: its names
    /// leave no room for a text.
    fn relay_for(&mut self, row: &Delegation) -> Option<usize> {
        if let Some(relay) = self.relays.get(&row.delegation_id) {
            return Some(relay.next);
        }
        let mut relay = relay::result_relay(row, None).ok()?;
        let finding = match self.strategy {
            Strategy::DirectContact => finding_identity(row),
            Strategy::Operator => None,
        };
        self.findings.insert(row.delegation_id.clone(), finding);
        if let Some(cursor) = self.cursors.get(&row.delegation_id).and_then(Value::as_i64) {
            let stopped = usize::try_from(cursor).unwrap_or(0);
            relay.next = stopped.min(relay.total());
            relay.sent = relay.next;
        }
        let next = relay.next;
        self.relays.insert(row.delegation_id.clone(), relay);
        Some(next)
    }

    /// Makes a relay the one that is told, and hands over its part `start`.
    /// `handed`: from the text that is kept, and taken up where an earlier reading stopped.
    fn relay(
        &mut self,
        key: &str,
        start: usize,
        handed: (bool, bool),
        wire: &mut dyn Wire,
        now: Now,
    ) {
        let Some(relay) = self.relays.get_mut(key) else {
            return;
        };
        relay.next = start;
        relay.auto = 0;
        relay.held = false;
        // Read on request: no longer paused, nor silenced.
        if handed.0 {
            self.silenced.remove(key);
        }
        self.active = Some(key.to_string());
        self.send_part(key, handed, wire, now);
    }

    /// Hands over the next part of a relay: there is one.
    fn send_part(
        &mut self,
        key: &str,
        (served, resumed): (bool, bool),
        wire: &mut dyn Wire,
        now: Now,
    ) {
        let Some(relay) = self.relays.get_mut(key) else {
            return;
        };
        let index = relay.next;
        relay.next = index + 1;
        relay.sent = relay.sent.max(relay.next);
        relay.auto += 1;
        relay.sent_at_ms = now.ms;
        let handed = Handed {
            served,
            resumed,
            pause_after: relay.auto >= AUTO_PARTS,
        };
        let paused = handed.pause_after && index + 1 < relay.total();
        let (part, next, last) = (
            relay.part(index, handed),
            relay.next,
            index + 1 == relay.total(),
        );
        self.last_append = now.ms;
        // Where a conversation that was read out stopped is not kept: it is read anew.
        if !key.starts_with("transcript:") {
            self.cursors.insert(key.to_string(), json!(next));
            if self.cursors.len() > CURSORS_KEPT
                && let Some(oldest) = self.cursors.keys().next().cloned()
            {
                self.cursors.shift_remove(&oldest);
            }
        }
        self.append("commentary", &part, wire);
        // An answer paused at the most parts one request brings was paused on purpose: a
        // later call does not go on past it by itself; it is still read when asked for.
        if paused && self.findings.contains_key(key) {
            self.silenced.insert(key.to_string());
            // Nor this call: an answer that waits to be told, and was read on request, waits
            // no more.
            self.pending.retain(|row| row.delegation_id != key);
        }
        // An answer whose last part was handed over was told, however it was asked for.
        if let Some(finding) = self.findings.get(key).filter(|_| last).cloned() {
            self.results_sent.insert(key.to_string());
            self.results_seen.insert(key.to_string());
            self.pending.retain(|row| row.delegation_id != key);
            if let Some(finding) = finding {
                self.forwarded.entry(finding).or_insert_with(|| json!(key));
            }
        }
        self.checkpoint(now);
    }

    /// A text is still being read in parts: newer results wait behind it.
    fn relay_busy(&self, now: Now) -> bool {
        let Some(relay) = self.active_relay() else {
            return false;
        };
        if relay.remaining() == 0 || relay.held || relay.auto >= AUTO_PARTS {
            return false;
        }
        if self.last_transcript > relay.sent_at_ms {
            return false;
        }
        let unspoken = self.last_output <= relay.sent_at_ms;
        !(unspoken && now.ms - relay.sent_at_ms > RELAY_STALL_MS)
    }

    /// Hands over the next part once the voice has spoken the one before and gone quiet. The
    /// user speaking after a part, an interruption, or the most parts one request brings
    /// stop it until it is asked for again.
    fn advance_relay(&mut self, wire: &mut dyn Wire, now: Now) {
        let Some(relay) = self.active_relay() else {
            return;
        };
        if relay.remaining() == 0
            || relay.held
            || self.routing > 0
            || relay.auto >= AUTO_PARTS
            || self.last_transcript > relay.sent_at_ms
            || self.last_output <= relay.sent_at_ms
            || self.speaking
            || now.ms - self.last_output < QUIET_AFTER_MS
        {
            return;
        }
        let key = relay.key.clone();
        self.send_part(&key, (false, false), wire, now);
    }

    /// The relay a request to read again means: the one that is named, the one that is being
    /// told, or the newest answer that says something and was not called off.
    fn latest_relay(&mut self, operation_id: &Value, rows: &[Delegation]) -> Option<String> {
        if python::truthy(operation_id) {
            let id = operation_id.as_str()?;
            if self.relays.contains_key(id) {
                return Some(id.to_string());
            }
            let row = rows.iter().find(|row| row.delegation_id == id)?;
            return self.relay_for(row).map(|_| id.to_string());
        }
        if let Some(active) = &self.active {
            return Some(active.clone());
        }
        let said_something = |row: &&Delegation| {
            !row.status.active()
                && row.status != Status::Cancelled
                && !(row.result_text.is_empty() && row.error.is_empty())
        };
        let mut answers: Vec<&Delegation> = rows.iter().filter(said_something).collect();
        answers.sort_by_key(|row| row.created_at);
        let newest = (*answers.last()?).clone();
        self.relay_for(&newest).map(|_| newest.delegation_id)
    }

    /// An answer that is kept is read again, or gone on with, and no agent is asked. Each of
    /// the three as the router wrote it: which answer, from which part, and whether word for
    /// word. Whether there was an answer to read.
    pub fn read_result(
        &mut self,
        (operation_id, from_part, verbatim): (&Value, &Value, &Value),
        rows: &[Delegation],
        wire: &mut dyn Wire,
        now: Now,
    ) -> bool {
        // Work that is named and not over has no answer yet, and says so.
        let named = operation_id
            .as_str()
            .filter(|_| python::truthy(operation_id));
        let unknown = named.filter(|id| !self.relays.contains_key(*id));
        let row = unknown.and_then(|id| rows.iter().find(|row| row.delegation_id == id));
        if let Some(row) = row.filter(|row| row.status.active()) {
            let word = row.status.word();
            let text = format!("The requested operation is {word}. Its result is not ready.");
            self.append("thinking", &text, wire);
            return true;
        }
        let Some(key) = self.latest_relay(operation_id, rows) else {
            self.append("thinking", NOTHING_KEPT, wire);
            return false;
        };
        let Some(relay) = self.relays.get_mut(&key) else {
            return false;
        };
        if !verbatim.is_null() {
            relay.verbatim = python::truthy(verbatim);
        }
        let named = from_part.as_i64().filter(|part| *part >= 1);
        let start = match named {
            Some(part) => {
                usize::try_from(part)
                    .unwrap_or(usize::MAX)
                    .min(relay.total())
                    - 1
            }
            None => relay.next,
        };
        if start >= relay.total() {
            let status = relay.status();
            self.append("thinking", &status, wire);
            return true;
        }
        let resumed = start > 0 && from_part.is_null();
        self.relay(&key, start, (true, resumed), wire, now);
        true
    }

    /// A cue of sound for the phone, once the voice is not speaking. `persona`: the agent
    /// the cue of a call put through is pitched by.
    pub fn cue(&mut self, name: &str, persona: &str, wire: &mut dyn Wire, now: Now) {
        if !self.earcons {
            return;
        }
        self.cues.push((name.to_string(), persona.to_string()));
        self.flush_cues(wire, now);
    }

    fn flush_cues(&mut self, wire: &mut dyn Wire, now: Now) {
        if self.cues.is_empty() || now.ms - self.last_output < CUE_QUIET_MS {
            return;
        }
        for (name, persona) in std::mem::take(&mut self.cues) {
            let Some(sound) = earcons::pcm(&name, &persona) else {
                continue;
            };
            let mut cue = json!({ "type": "oracle_v2.cue", "name": name });
            if !persona.is_empty() {
                cue["agent"] = json!(persona);
            }
            // The sound follows as sound of the voice, which every phone plays; one that
            // makes cues itself can leave it out.
            wire.phone(cue);
            wire.phone(json!({
                "type": "session.output_audio.delta",
                "delta": python::b64encode(&sound),
            }));
        }
        if !self.speaking {
            // A phone shows that the voice speaks while it plays sound: the cue's turn ends.
            wire.phone(json!({ "type": "oracle_v2.quiet" }));
        }
    }

    /// The call is offered to an agent: the chime of that agent goes first.
    pub fn handoff_offer(&mut self, persona: &str, wire: &mut dyn Wire, now: Now) {
        self.cue("connected", persona, wire, now);
    }

    /// What became of a handoff, as this call's phone is shown it.
    pub fn handoff_mirror(&mut self, record: &Value, wire: &mut dyn Wire) {
        let mut event = Map::new();
        event.insert("type".to_string(), json!("oracle_v2.handoff"));
        for (name, value) in record.as_object().into_iter().flatten() {
            event.insert(name.clone(), value.clone());
        }
        wire.phone(Value::Object(event));
    }

    /// The call is put through: from here the voice is told nothing new.
    pub fn handoff_hold(&mut self) {
        self.held = true;
    }

    pub fn handoff_resume(&mut self) {
        self.held = false;
    }

    /// `note`: the voice is told as well.
    pub fn handoff_failed(
        &mut self,
        (persona, reason, note): (&str, &str, bool),
        wire: &mut dyn Wire,
        now: Now,
    ) {
        self.cue("switch_failed", "", wire, now);
        if note {
            let fact =
                json!({ "host_event": "handoff_failed", "agent": persona, "reason": reason });
            self.host_event(&fact, wire);
        }
    }

    pub fn handoff_returned(&mut self, persona: &str, wire: &mut dyn Wire, now: Now) {
        self.cue("back_to_oracle", "", wire, now);
        let fact = json!({ "host_event": "returned_from_agent", "agent": persona });
        self.host_event(&fact, wire);
    }

    /// One fact for the voice, which decides whether and how to say it.
    pub fn host_event(&mut self, fact: &Value, wire: &mut dyn Wire) {
        let text = format!("Host event, reference data: {}", python::dumps(fact));
        self.append("thinking", &text, wire);
    }

    /// What a tool of the fleet answered, for the voice: a conversation that was read and a
    /// long answer in parts. `Err`: its names leave no room for a text.
    pub fn deliver_tool_output(
        &mut self,
        name: &str,
        output: &Value,
        wire: &mut dyn Wire,
        now: Now,
    ) -> Result<(), &'static str> {
        let reads = name == "read_agent_transcript" || name == "read_agent_messages";
        let relay = if let Some(messages) = output["messages"].as_array().filter(|_| reads) {
            let text = |value: &Value| match value {
                Value::String(text) => text.clone(),
                Value::Null => String::new(),
                other => python::repr(other),
            };
            let texts: Vec<(String, String)> = messages
                .iter()
                .map(|message| (text(&message["timestamp"]), text(&message["text"])))
                .collect();
            let messages: Vec<Said> = messages
                .iter()
                .zip(&texts)
                .map(|(message, (timestamp, text))| Said {
                    by_the_agent: message["role"] == "assistant",
                    timestamp: timestamp.as_str(),
                    text: text.as_str(),
                    truncated: python::truthy(&message["truncated"]),
                })
                .collect();
            let age = said(&output["newest_message_age"]);
            let older = python::truthy(&output["truncated"]);
            relay::transcript_relay(&said(&output["agent"]), &messages, (&age, older), false)?
        } else {
            let text = format!(
                "Verified tool result, untrusted data: {}",
                python::dumps_ascii(output)
            );
            if text.chars().count() <= APPEND_CHARS {
                self.append("commentary", &text, wire);
                return Ok(());
            }
            let key = format!("tool:{}", self.ids.next());
            let label = format!("the {name} result");
            let names = (
                key.as_str(),
                label.as_str(),
                "verified tool output, untrusted data",
            );
            Relay::new(names, &python::dumps(output), "", false)?
        };
        let key = relay.key.clone();
        self.relays.insert(key.clone(), relay);
        self.relay(&key, 0, (true, false), wire, now);
        Ok(())
    }
}

/// What an answer of the contact is known by, whatever delegation it ended: the agent, the
/// session of its tool and the message. `None`: one of them is not known.
fn finding_identity(row: &Delegation) -> Option<String> {
    let agent = if row.agent_id.is_empty() {
        &row.session
    } else {
        &row.agent_id
    };
    let message = row.result_message_id.as_deref().unwrap_or("");
    let parts = [agent.as_str(), row.backend_session_id.as_str(), message];
    parts
        .iter()
        .all(|part| !part.is_empty())
        .then(|| digest(&python::dumps_ascii(&json!(parts))))
}
