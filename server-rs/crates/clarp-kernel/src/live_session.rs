//! The session of a live call.
//!
//! Once a phone has joined a ring held as a live voice call ([`crate::rings`]), the call is
//! one session: the phone on one side, the voice model on the other, the Host between them.
//! The Host reaches the voice and tells both sides of the call. When both are ready it gives
//! the voice its opening, once, and the call is under way when the voice says that it took
//! it. Until then no sound of the voice reaches the phone (what comes after the opening was
//! handed over is held, in its order, and what comes before it ends the call), and no sound
//! of the user reaches the voice. The call ends for whoever leaves, and for whatever goes
//! wrong, with a reason; and the first thing that happens then is that the call loses its
//! authority on its ring.
//!
//! This is one machine with no clock, socket or store of its own: it is told what each side
//! said and that time passed, writes on its ring through a [`Book`], and says what each side
//! is to be told through a [`Wire`]. One task owns it, so what the Python Host orders with
//! locks is here the order in which the machine is called. What must hold is the same:
//! nothing is sent to either side for a call that has lost its authority, and nothing after
//! the session is over but the word that it is.
//!
//! The Python Host's `agent_ring_live_socket.py` (`LiveSocket`), held to it by
//! `tests/fixtures/live-session-vectors.json`.

use serde_json::{Value, json};

use crate::live_call;
use crate::python;
use crate::rings::Joined;

/// How long both sides have to be ready, from the join.
pub const READY_S: f64 = 15.0;
/// How long the voice has to take its opening.
pub const OPENING_TAKEN_S: f64 = 10.0;
/// How often time is told to pass.
pub const TICK_S: f64 = 0.1;
/// After how many ticks it is looked whether the phone is still paired, and the call still
/// has its authority.
pub const LOOKED_EVERY: u64 = 10;
/// How much sound of the voice is held until it took its opening, in bytes: three seconds.
pub const HELD_MOST: usize = 144_000;
/// The longest piece of sound that could be held, as it is written.
pub const HELD_PIECE_SIGNS: usize = 4 * HELD_MOST.div_ceil(3);
/// The longest piece of sound a phone sends, as it is written.
pub const SOUND_SIGNS: usize = 262_144;
/// How long a phone may say nothing, and how long it has for its join.
pub const PHONE_IDLE_S: f64 = 30.0;
pub const JOIN_WITHIN_S: f64 = 10.0;

/// What a session writes on the ring of its call: each only while the call has its
/// authority, and each says whether it was written.
pub trait Book {
    /// Whether the call still has its authority.
    fn holds(&self) -> bool;
    /// The voice is given its opening: once.
    fn opening(&mut self) -> bool;
    /// The voice took it: the call is under way.
    fn under_way(&mut self) -> bool;
    /// The user's sound goes to the voice.
    fn heard(&mut self) -> bool;
    /// The call loses its authority.
    fn retire(&mut self, reason: &str) -> bool;
}

/// What a session tells the two sides of its call.
pub trait Wire {
    fn phone(&mut self, said: Value);
    /// `false`: the voice takes nothing more.
    fn voice(&mut self, said: Value) -> bool;
    /// The voice is told that the call is over, and let go.
    fn voice_over(&mut self);
    /// The phone's socket is ended.
    fn phone_over(&mut self);
}

/// A tenth of a second of silence, as it is sent to a voice.
pub fn silence() -> String {
    python::b64encode(&[0u8; 4800])
}

/// Sound as it goes between a phone, a Host and a voice: in base64, a whole number of
/// samples of two bytes, and at least one.
fn sound(written: &str) -> Option<usize> {
    let bytes = python::b64decode(written)?;
    (!bytes.is_empty() && bytes.len().is_multiple_of(2)).then_some(bytes.len())
}

/// The voice took nothing: what follows depends on who was being served.
struct Deaf;

/// The session of one call.
#[derive(Debug, Clone, PartialEq)]
pub struct Session {
    call: Joined,
    /// The name the opening is handed to the voice under: what its taking is known by.
    opening_id: String,
    joined_at_s: f64,
    has_voice: bool,
    phone_ready: bool,
    voice_ready: bool,
    /// The opening: reserved, handed to the voice, taken by it; and what was held is out.
    opening_sent: bool,
    opening_handed: bool,
    opening_failed: bool,
    opening_sent_at_s: f64,
    admitted: bool,
    drained: bool,
    /// The user's sound goes to the voice.
    forwarding: bool,
    held: Vec<String>,
    held_bytes: usize,
    ending: bool,
    over: bool,
    reason: String,
    ticks: u64,
}

impl Session {
    /// The session of a call that was just joined.
    pub fn new(call: Joined, opening_id: &str, now_s: f64) -> Self {
        Self {
            call,
            opening_id: opening_id.to_string(),
            joined_at_s: now_s,
            has_voice: false,
            phone_ready: false,
            voice_ready: false,
            opening_sent: false,
            opening_handed: false,
            opening_failed: false,
            opening_sent_at_s: 0.0,
            admitted: false,
            drained: false,
            forwarding: false,
            held: Vec::new(),
            held_bytes: 0,
            ending: false,
            over: false,
            reason: String::new(),
            ticks: 0,
        }
    }

    pub fn is_over(&self) -> bool {
        self.over
    }

    /// Why it ended, once it has.
    pub fn reason(&self) -> &str {
        &self.reason
    }

    /// Whether the call was under way: its voice took the opening.
    pub fn was_under_way(&self) -> bool {
        self.admitted
    }

    pub fn call(&self) -> &Joined {
        &self.call
    }

    /// The names of the call, as every word about it carries them.
    fn named(&self, kind: &str) -> Value {
        json!({
            "type": kind, "call_id": self.call.call_id, "call_epoch": self.call.call_epoch,
            "attempt_id": self.call.attempt_id,
        })
    }

    /// Tells the phone. After the session is over only that it is, and why.
    fn tell(&self, wire: &mut dyn Wire, said: Value) {
        let last_words = said["type"] == "agent_live.closed" || said["type"] == "agent_live.error";
        if !self.over || last_words {
            wire.phone(said);
        }
    }

    /// Tells the phone something of the call itself, which only a call with its authority
    /// has. Whether it was told.
    fn tell_of_call(&self, book: &dyn Book, wire: &mut dyn Wire, said: Value) -> bool {
        if self.over || !book.holds() {
            return false;
        }
        wire.phone(said);
        true
    }

    /// Tells the voice, for a call with its authority. `Ok(false)`: nothing was sent.
    fn say(&mut self, book: &dyn Book, wire: &mut dyn Wire, said: Value) -> Result<bool, Deaf> {
        if !self.has_voice || self.over || !book.holds() {
            return Ok(false);
        }
        if !wire.voice(said) {
            return Err(Deaf);
        }
        Ok(true)
    }

    /// The session is over: both sides are told and let go. Its authority was retired first.
    fn stop_with(&mut self, wire: &mut dyn Wire, reason: &str) {
        if self.over {
            return;
        }
        self.over = true;
        self.reason = reason.to_string();
        self.held.clear();
        self.held_bytes = 0;
        let mut closed = self.named("agent_live.closed");
        closed["reason"] = json!(reason);
        wire.phone(closed);
        if self.has_voice {
            wire.voice_over();
        }
        wire.phone_over();
    }

    /// The call fails: its authority is retired, the phone is told why in a code, and the
    /// session is over.
    fn fail(&mut self, book: &mut dyn Book, wire: &mut dyn Wire, code: &str, reason: &str) {
        if self.over {
            return;
        }
        self.ending = true;
        book.retire(reason);
        let mut error = self.named("agent_live.error");
        error["code"] = json!(code);
        error["message"] = json!(code);
        error["retryable"] = json!(false);
        wire.phone(error);
        self.stop_with(wire, reason);
    }

    /// The call ends as it should: its authority is retired, and the session is over.
    fn end(&mut self, book: &mut dyn Book, wire: &mut dyn Wire, reason: &str) {
        self.ending = true;
        book.retire(reason);
        self.stop_with(wire, reason);
    }

    /// What the Host does once a phone joined: it tells both sides of the call. `has_key`:
    /// the Host has a key for the voice; `reached`: the voice was reached with it.
    pub fn start(
        &mut self,
        (has_key, reached): (bool, bool),
        host_id: &str,
        book: &mut dyn Book,
        wire: &mut dyn Wire,
    ) {
        // A close or a switch may have come between the join and this.
        if !book.holds() {
            return self.fail(book, wire, "live_call_stale", "stale");
        }
        if !has_key {
            return self.fail(book, wire, "live_unavailable", "provider_unusable");
        }
        if !reached {
            return self.fail(book, wire, "live_unavailable", "provider_connect_failed");
        }
        self.has_voice = true;
        let call = &self.call;
        let of_the_call = json!({
            "type": "agent_live.session", "protocol": live_call::PROTOCOL, "host_id": host_id,
            "ring_id": call.ring_id, "agent_id": call.agent_id, "call_id": call.call_id,
            "call_epoch": call.call_epoch, "attempt_id": call.attempt_id,
            "context_digest": call.context_digest, "provider": live_call::PROVIDER,
            "model": live_call::MODEL, "voice": live_call::VOICE,
        });
        self.tell(wire, of_the_call);
        let begin = json!({
            "type": "session.start",
            "session": {
                "model": live_call::MODEL,
                "instructions": live_call::instructions(&self.call.snapshot),
                "audio": {
                    "output": { "voice": live_call::VOICE },
                    "format": { "type": "audio/pcm", "rate": 24000 },
                },
                "delegation": { "type": "client" },
            },
        });
        if self.say(book, wire, begin).is_err() {
            self.fail(book, wire, "live_unavailable", "host_error");
        }
    }

    /// Both sides are ready: the voice is given its opening, once. It takes an instruction
    /// only while sound comes in, so a moment of silence goes first.
    fn open(&mut self, book: &mut dyn Book, wire: &mut dyn Wire, now_s: f64) -> Result<(), Deaf> {
        if !(self.phone_ready && self.voice_ready) || self.opening_sent || self.over {
            return Ok(());
        }
        self.opening_sent = true;
        self.opening_sent_at_s = now_s;
        if !book.opening() {
            self.fail(book, wire, "live_call_stale", "stale");
            return Ok(());
        }
        let quiet = json!({ "type": "session.input_audio.append", "audio": silence() });
        self.say(book, wire, quiet)?;
        if !self.has_voice || self.over || !book.holds() {
            return Ok(());
        }
        let line = self.call.snapshot["opening_text"].as_str().unwrap_or("");
        let opening = json!({
            "type": "session.instructions.append",
            "content": format!("OPENING: say exactly this, once, now: {line}"),
            "delegation_id": null, "event_id": self.opening_id,
        });
        // From here on what the voice says can be the opening being said.
        self.opening_handed = true;
        if !wire.voice(opening) {
            self.opening_failed = true;
            self.held.clear();
            self.held_bytes = 0;
            self.fail(book, wire, "live_unavailable", "opening_send_failed");
        }
        Ok(())
    }

    /// The voice says that it took an instruction. It is the opening when it names it, and
    /// when it names nothing: until the call is under way the opening is the one instruction
    /// the voice was ever handed. (The Python Host counts the instructions it handed over and
    /// takes a nameless word for the opening's only while that count is one; the tools of a
    /// call, which hand over others, are not run by this Host yet, and the word to be still is
    /// said in a call under way.) Then the call is under way: the phone is told, and what was
    /// held of the voice's sound follows in its order.
    fn taken(&mut self, book: &mut dyn Book, wire: &mut dyn Wire, said: &Value) {
        if self.admitted || self.over || self.ending || !self.opening_handed || self.opening_failed
        {
            return;
        }
        let named = said.get("client_event_id");
        if named.is_some_and(|named| *named != self.opening_id.as_str()) {
            return;
        }
        self.admitted = true;
        if !book.under_way() {
            return self.fail(book, wire, "live_call_stale", "stale");
        }
        let held = std::mem::take(&mut self.held);
        self.held_bytes = 0;
        let mut admitted = self.named("agent_live.opening_admitted");
        admitted["context_digest"] = json!(self.call.context_digest);
        if !self.tell_of_call(book, wire, admitted) {
            return;
        }
        for piece in held {
            let delta = json!({ "type": "session.output_audio.delta", "delta": piece });
            if self.ending || !self.tell_of_call(book, wire, delta) {
                return;
            }
        }
        self.drained = true;
    }

    /// Sound of the voice. It reaches the phone once the call is under way. What comes after
    /// the opening was handed over and before it was taken is held, and what comes before the
    /// opening was handed over, or cannot be held, ends the call.
    fn spoken(&mut self, book: &mut dyn Book, wire: &mut dyn Wire, delta: &Value) {
        let Some(piece) = delta.as_str() else {
            return;
        };
        if self.over || self.ending || !book.holds() || self.opening_failed {
            return;
        }
        if self.drained {
            let delta = json!({ "type": "session.output_audio.delta", "delta": piece });
            self.tell_of_call(book, wire, delta);
            return;
        }
        let wrong = if !self.opening_handed {
            Some("provider_output_before_ack")
        } else if piece.chars().count() > HELD_PIECE_SIGNS {
            Some("opening_output_overflow")
        } else {
            match sound(piece) {
                None => Some("provider_output_invalid"),
                Some(bytes) if self.held_bytes + bytes > HELD_MOST => {
                    Some("opening_output_overflow")
                }
                Some(bytes) => {
                    self.held.push(piece.to_string());
                    self.held_bytes += bytes;
                    None
                }
            }
        };
        if let Some(reason) = wrong {
            self.ending = true;
            self.fail(book, wire, "live_unavailable", reason);
        }
    }

    fn of_the_voice(
        &mut self,
        said: &Value,
        book: &mut dyn Book,
        wire: &mut dyn Wire,
        now_s: f64,
    ) -> Result<(), Deaf> {
        match said["type"].as_str().unwrap_or("") {
            "session.started" => {
                self.voice_ready = true;
                self.tell(wire, self.named("agent_live.provider_ready"));
                self.open(book, wire, now_s)?;
            }
            "session.instructions.appended" => self.taken(book, wire, said),
            "session.output_audio.delta" => self.spoken(book, wire, &said["delta"]),
            "session.output_audio.interrupted" => {
                if self.drained && !self.over {
                    self.tell(wire, json!({ "type": "oracle_v2.playback_flush" }));
                    self.tell(wire, json!({ "type": "oracle_v2.quiet" }));
                }
            }
            "session.closed" => self.end(book, wire, "provider_closed"),
            // Something is wrong with the voice: before the call is under way, that ends it.
            "error" if !self.admitted => {
                self.fail(book, wire, "live_unavailable", "provider_error");
            }
            _ => {}
        }
        Ok(())
    }

    /// The voice said something.
    pub fn voice(&mut self, said: &Value, book: &mut dyn Book, wire: &mut dyn Wire, now_s: f64) {
        if self.of_the_voice(said, book, wire, now_s).is_err() {
            // What it said could not be acted on, since it takes nothing: it is gone.
            self.voice_gone(book, wire);
        }
    }

    /// The voice's socket ended.
    pub fn voice_gone(&mut self, book: &mut dyn Book, wire: &mut dyn Wire) {
        if self.over {
            return;
        }
        if self.admitted {
            self.end(book, wire, "provider_closed");
        } else {
            self.fail(book, wire, "live_unavailable", "provider_lost");
        }
    }

    fn of_the_phone(
        &mut self,
        said: &Value,
        book: &mut dyn Book,
        wire: &mut dyn Wire,
        now_s: f64,
    ) -> Result<(), Deaf> {
        if self.over {
            return Ok(());
        }
        match said["type"].as_str().unwrap_or("") {
            "session.input_audio.append" => {
                let audio = said["audio"].as_str().filter(|audio| {
                    (1..=SOUND_SIGNS).contains(&audio.chars().count()) && sound(audio).is_some()
                });
                let Some(audio) = audio else {
                    self.fail(book, wire, "audio_not_ready", "audio_invalid");
                    return Ok(());
                };
                if !self.admitted {
                    self.fail(book, wire, "audio_not_ready", "audio_before_opening");
                    return Ok(());
                }
                if !self.forwarding {
                    // Written before the first of the user's sound goes to the voice: from
                    // here on the call is not switched to an ordinary one.
                    if !book.heard() {
                        self.fail(book, wire, "live_call_stale", "stale");
                        return Ok(());
                    }
                    self.forwarding = true;
                }
                let heard = json!({ "type": "session.input_audio.append", "audio": audio });
                self.say(book, wire, heard)?;
            }
            "agent_live.client_ready" => {
                let call = &self.call;
                let same = said["call_id"] == call.call_id.as_str()
                    && said["call_epoch"] == call.call_epoch
                    && said["attempt_id"] == call.attempt_id.as_str();
                if !same {
                    self.fail(book, wire, "live_call_stale", "stale");
                    return Ok(());
                }
                if said["audio"] == true && said["capture"] == true {
                    self.phone_ready = true;
                    self.open(book, wire, now_s)?;
                }
            }
            "oracle_v2.interrupt" => {
                if self.drained {
                    let be_still = json!({
                        "type": "session.instructions.append",
                        "content": "Stop speaking now and listen to the user.",
                        "delegation_id": null,
                    });
                    self.say(book, wire, be_still)?;
                    self.tell(wire, json!({ "type": "oracle_v2.playback_flush" }));
                    self.tell(wire, json!({ "type": "oracle_v2.quiet" }));
                }
            }
            "session.close" => self.end(book, wire, "user_hangup"),
            _ => {}
        }
        Ok(())
    }

    /// The phone said something.
    pub fn phone(&mut self, said: &Value, book: &mut dyn Book, wire: &mut dyn Wire, now_s: f64) {
        if self.of_the_phone(said, book, wire, now_s).is_err() {
            self.fail(book, wire, "live_unavailable", "host_error");
        }
    }

    /// The phone's socket ended, or it said nothing for too long.
    pub fn phone_gone(&mut self, book: &mut dyn Book, wire: &mut dyn Wire) {
        if !self.over {
            self.end(book, wire, "transport_lost");
        }
    }

    /// The call was ended beside its session, by a close or a switch that retired it on its
    /// ring: the session is over, for that reason.
    pub fn stopped(&mut self, wire: &mut dyn Wire, reason: &str) {
        self.stop_with(wire, reason);
    }

    /// A tenth of a second passed. `paired`: the phone is still paired with all rights.
    pub fn tick(&mut self, paired: bool, book: &mut dyn Book, wire: &mut dyn Wire, now_s: f64) {
        if self.over {
            return;
        }
        self.ticks += 1;
        if self.ticks.is_multiple_of(LOOKED_EVERY) {
            if !paired {
                return self.end(book, wire, "device_revoked");
            }
            // Retired beside the session, whose own stop never came.
            if !self.ending && !book.holds() {
                return self.fail(book, wire, "live_call_stale", "stale");
            }
        }
        let ready = self.phone_ready && self.voice_ready;
        if !ready && now_s - self.joined_at_s > READY_S {
            return self.fail(book, wire, "live_unavailable", "not_ready");
        }
        // The voice is fed silence until the user's own sound goes to it.
        if ready && !self.forwarding {
            let quiet = json!({ "type": "session.input_audio.append", "audio": silence() });
            if self.say(book, wire, quiet).is_err() {
                return self.fail(book, wire, "live_unavailable", "host_error");
            }
        }
        if self.opening_sent && !self.admitted && now_s - self.opening_sent_at_s > OPENING_TAKEN_S {
            self.fail(book, wire, "live_unavailable", "opening_not_taken");
        }
    }
}
