//! The stream of speech: what is pure in it.
//!
//! When the engine that hears finds the end of a turn of speech itself, a phone streams the
//! sound to its Host over a socket and the Host carries it to the engine (Deepgram Flux),
//! and tells the phone what the engine says of the turns. The Python Host does that in
//! `server/lib/stt_stream.py`. What is no carrying in it is here, held to that module by
//! its own answers (`tools/stream-vectors.py`): the address the engine is reached at, by
//! how a phone tunes the listening; what a message of the engine means; the books of one
//! stream, which say which sound belongs to which turn and what the phone is told; and the
//! sound of a turn as a file.

use serde_json::{Map, Value, json};

use crate::python;

/// How many times a second the sound is measured.
pub const SAMPLE_RATE: u32 = 16_000;
/// Where the engine listens.
pub const FLUX_URL: &str = "wss://api.deepgram.com/v2/listen";
/// How sure the engine must be that a turn is over, where a phone does not say.
pub const EOT_THRESHOLD: f64 = 0.7;
/// From how sure on it says that a turn may be over, where a phone does not say.
pub const EAGER_EOT_THRESHOLD: f64 = 0.5;
/// After how long a silence the engine ends a turn whatever it thinks: its longest, so
/// that the model decides and not the clock.
pub const EOT_TIMEOUT_MS: i64 = 60_000;
/// How long the engine is given to say its last turn after the phone said it is done.
pub const FLUSH_GRACE_S: f64 = 4.0;
/// How long the engine is given to take the socket up.
pub const OPENS_WITHIN_S: f64 = 15.0;
/// How much sound of one turn is held: two minutes of it.
pub const HELD_MOST: usize = SAMPLE_RATE as usize * 2 * 120;

/// A text as Python's `float()` reads it: the space around it left out, a sign, digits with
/// a point and an exponent, an underscore only between two digits, or one of the words for
/// what is no number and for what has no end. `None`: Python says it is no number. Digits
/// are the ten of ASCII: Python reads those of other scripts too.
pub fn float_of(text: &str) -> Option<f64> {
    let text = python::strip(text);
    let signs: Vec<char> = text.chars().collect();
    let mut plain = String::with_capacity(text.len());
    for (at, sign) in signs.iter().enumerate() {
        let digit = |place: Option<&char>| place.is_some_and(char::is_ascii_digit);
        match sign {
            '_' if at > 0 && digit(signs.get(at - 1)) && digit(signs.get(at + 1)) => {}
            '_' => return None,
            sign => plain.push(*sign),
        }
    }
    plain.parse::<f64>().ok()
}

/// What a phone says of one side of the tuning, as the Python Host reads it: the number it
/// names, and where it names none or what is no number, the usual one.
fn tuned(said: Option<&str>, usual: f64) -> f64 {
    said.and_then(float_of).unwrap_or(usual)
}

/// A side of the tuning kept within what the engine takes, as Python's
/// `max(least, min(most, side))` keeps it: what is no number at all is the most.
fn within(side: f64, least: f64, most: f64) -> f64 {
    side.min(most).max(least)
}

/// How the engine's listening is tuned.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Tuning {
    pub eot_threshold: f64,
    pub eager_eot_threshold: f64,
    /// `None`: what the phone named is no number of milliseconds (nothing that is a
    /// number, or a number without an end). The Python Host fails such a request.
    pub eot_timeout_ms: Option<i64>,
}

impl Tuning {
    /// The tuning a phone's request names, by what it says under each name.
    pub fn of<'a>(said: impl Fn(&str) -> Option<&'a str>) -> Self {
        let timeout = tuned(said("eot_timeout_ms"), EOT_TIMEOUT_MS as f64);
        Self {
            eot_threshold: tuned(said("eot_threshold"), EOT_THRESHOLD),
            eager_eot_threshold: tuned(said("eager_eot_threshold"), EAGER_EOT_THRESHOLD),
            // A number is cut at its point, as Python's `int()` cuts one.
            eot_timeout_ms: timeout.is_finite().then(|| timeout.trunc() as i64),
        }
    }
}

/// A text as it stands in an address after a name: letters, digits and four other signs as
/// they are, a space as a plus, every other byte as its number (`urllib.parse.quote_plus`).
fn in_an_address(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(byte));
            }
            b' ' => out.push('+'),
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// The address the engine is reached at for one stream: its model, how the sound comes,
/// how its listening is tuned (each side kept within what the engine takes), and the words
/// it is asked to listen for. `None`: the tuning names no time (see [`Tuning`]).
pub fn flux_url(model: &str, tuning: &Tuning, keyterms: &[String]) -> Option<String> {
    let threshold = within(tuning.eot_threshold, 0.5, 1.0);
    let eager = within(tuning.eager_eot_threshold, 0.3, 0.9);
    let timeout = tuning.eot_timeout_ms?.clamp(500, 60_000);
    let mut named = vec![
        ("model", model.to_string()),
        ("encoding", "linear16".to_string()),
        ("sample_rate", SAMPLE_RATE.to_string()),
        ("eot_threshold", format!("{threshold:.2}")),
        ("eot_timeout_ms", timeout.to_string()),
        ("eager_eot_threshold", format!("{eager:.2}")),
    ];
    named.extend(keyterms.iter().map(|term| ("keyterm", term.clone())));
    let named: Vec<String> = named
        .iter()
        .map(|(name, value)| format!("{name}={}", in_an_address(value)))
        .collect();
    Some(format!("{FLUX_URL}?{}", named.join("&")))
}

/// What the engine says of a turn of speech, in the Host's words for it.
#[derive(Debug, Clone, PartialEq)]
pub struct TurnEvent {
    /// `start`, `update`, `eager`, `resumed` or `end`.
    pub kind: &'static str,
    /// Which turn of the stream, as the engine counts them.
    pub turn: Value,
    /// What was understood so far.
    pub text: String,
    /// How sure the engine is that the turn is over, when it says.
    pub confidence: Option<f64>,
}

/// A value as Python's `int()` reads it where it can: a whole number, the whole part of
/// one that is not, a text of digits. `None`: Python reads no number in it.
fn whole(value: &Value) -> Option<Value> {
    match value {
        Value::Bool(yes) => Some(json!(i64::from(*yes))),
        Value::Number(number) if number.is_f64() => {
            let cut = number.as_f64()?.trunc();
            Some(json!(cut as i64))
        }
        Value::Number(_) => Some(value.clone()),
        Value::String(text) => {
            let number = python::int(text)?;
            let fits = i64::try_from(number).ok()?;
            Some(json!(fits))
        }
        _ => None,
    }
}

/// A value as Python's `float()` reads it where it can.
fn fraction(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(yes) => Some(f64::from(u8::from(*yes))),
        Value::Number(number) => number.as_f64(),
        Value::String(text) => float_of(text),
        _ => None,
    }
}

/// A value as Python's `str()` writes it: a text as it is, anything else as Python shows it.
fn written(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// What a message of the engine means: an event of a turn, or nothing (what the engine says
/// of other things, and what is no message of its kind).
pub fn meant(message: &Value) -> Option<TurnEvent> {
    let message = message.as_object()?;
    if message.get("type") != Some(&json!("TurnInfo")) {
        return None;
    }
    let kind = match message.get("event")?.as_str()? {
        "StartOfTurn" => "start",
        "Update" => "update",
        "EagerEndOfTurn" => "eager",
        "TurnResumed" => "resumed",
        "EndOfTurn" => "end",
        _ => return None,
    };
    let turn = message.get("turn_index").and_then(whole);
    // What was understood, where anything is said of it: nothing, and what Python takes
    // for nothing, is no text.
    let understood = message.get("transcript");
    let understood = understood.filter(|value| python::truthy(value));
    let confidence = message.get("end_of_turn_confidence");
    Some(TurnEvent {
        kind,
        turn: turn.unwrap_or_else(|| json!(0)),
        text: understood.map(written).unwrap_or_default(),
        confidence: confidence.and_then(fraction),
    })
}

/// A number to three places after its point, as Python's `round(number, 3)` leaves it.
fn to_three_places(number: f64) -> f64 {
    format!("{number:.3}").parse().unwrap_or(number)
}

/// The sound of a turn as a file: one voice, sixteen bits to a measure, as it came.
pub fn wav(sound: &[u8]) -> Vec<u8> {
    let length = u32::try_from(sound.len()).unwrap_or(u32::MAX);
    let mut file = Vec::with_capacity(44 + sound.len());
    file.extend_from_slice(b"RIFF");
    file.extend_from_slice(&length.saturating_add(36).to_le_bytes());
    file.extend_from_slice(b"WAVEfmt ");
    file.extend_from_slice(&16u32.to_le_bytes());
    // Plain measures, one voice.
    file.extend_from_slice(&1u16.to_le_bytes());
    file.extend_from_slice(&1u16.to_le_bytes());
    file.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
    file.extend_from_slice(&(SAMPLE_RATE * 2).to_le_bytes());
    file.extend_from_slice(&2u16.to_le_bytes());
    file.extend_from_slice(&16u16.to_le_bytes());
    file.extend_from_slice(b"data");
    file.extend_from_slice(&length.to_le_bytes());
    file.extend_from_slice(sound);
    file
}

/// A turn that ended, as it is written down.
#[derive(Debug, Clone, PartialEq)]
pub struct Run<'a> {
    pub session: &'a str,
    pub trace_id: &'a str,
    pub provider: &'a str,
    pub model: &'a str,
    pub keyterms: &'a [String],
    pub capacity: u32,
    pub transcript: &'a str,
    pub latency_ms: i64,
}

/// The books of one stream: which sound belongs to which turn, and what the phone is told
/// of each event. A turn opens with the first sound or the first word of the engine after
/// the turn before, so that the engine's late word of a turn's start loses no syllable.
#[derive(Debug, Clone, PartialEq)]
pub struct Ledger {
    pub session: String,
    pub provider: String,
    pub model: String,
    pub keyterms: Vec<String>,
    pub capacity: u32,
    /// The name of the turn that is open: empty between two turns.
    trace_id: String,
    /// When the open turn opened, on a clock that only goes forward, in seconds.
    started_at: f64,
    /// The sound of the open turn: the last of it, where a turn is longer than is held.
    sound: Vec<u8>,
    /// How many turns have ended.
    pub turns_ended: u64,
}

/// What a ledger needs of its Host when a turn opens or ends.
pub trait Keeps {
    /// A name for a turn that opens.
    fn new_trace(&mut self) -> String;
    /// Writes a turn that ended down. The number it was written under: `None` where it
    /// could not be written, which is not worth a turn.
    fn record(&mut self, run: &Run<'_>) -> Option<i64>;
    /// Keeps the sound of a turn that ended, as a file. That it could not be kept is not
    /// worth a turn either.
    fn retain(&mut self, trace_id: &str, wav: Vec<u8>);
}

impl Ledger {
    pub fn new(
        session: &str,
        provider: &str,
        model: &str,
        keyterms: Vec<String>,
        capacity: u32,
    ) -> Self {
        Self {
            session: session.to_string(),
            provider: provider.to_string(),
            model: model.to_string(),
            keyterms,
            capacity,
            trace_id: String::new(),
            started_at: 0.0,
            sound: Vec::new(),
            turns_ended: 0,
        }
    }

    /// The name of the turn that is open, and how much of its sound is held.
    pub fn open_turn(&self) -> (&str, usize) {
        (&self.trace_id, self.sound.len())
    }

    fn open_if_needed(&mut self, now_s: f64, keeps: &mut dyn Keeps) {
        if self.trace_id.is_empty() {
            self.trace_id = keeps.new_trace();
            self.started_at = now_s;
        }
    }

    /// Sound that came: it belongs to the turn that is open, and opens one where none is.
    pub fn heard(&mut self, sound: &[u8], now_s: f64, keeps: &mut dyn Keeps) {
        self.open_if_needed(now_s, keeps);
        self.sound.extend_from_slice(sound);
        if self.sound.len() > HELD_MOST {
            let over = self.sound.len() - HELD_MOST;
            self.sound.drain(..over);
        }
    }

    /// What the phone is told of one event. The end of a turn closes it in the books: it
    /// is written down, and its sound is kept where there was any.
    pub fn apply(&mut self, event: &TurnEvent, now_s: f64, keeps: &mut dyn Keeps) -> Value {
        self.open_if_needed(now_s, keeps);
        let mut told = Map::new();
        told.insert("type".to_string(), json!("turn"));
        told.insert("event".to_string(), json!(event.kind));
        told.insert("turn".to_string(), event.turn.clone());
        told.insert("text".to_string(), json!(event.text));
        told.insert("trace_id".to_string(), json!(self.trace_id));
        if let Some(confidence) = event.confidence {
            told.insert("confidence".to_string(), json!(to_three_places(confidence)));
        }
        if event.kind != "end" {
            return Value::Object(told);
        }
        let latency_ms = ((now_s - self.started_at) * 1000.0).trunc() as i64;
        let run = Run {
            session: &self.session,
            trace_id: &self.trace_id,
            provider: &self.provider,
            model: &self.model,
            keyterms: &self.keyterms,
            capacity: self.capacity,
            transcript: &event.text,
            latency_ms,
        };
        // A run written as nothing is no run, as one that could not be written.
        let run_id = keeps.record(&run).filter(|run_id| *run_id != 0);
        if !self.sound.is_empty() {
            keeps.retain(&self.trace_id, wav(&self.sound));
        }
        told.insert("vocab_run_id".to_string(), json!(run_id));
        told.insert("turn_ms".to_string(), json!(latency_ms));
        self.turns_ended += 1;
        self.trace_id.clear();
        self.sound.clear();
        Value::Object(told)
    }
}

/// What the phone is told when the stream is open: which engine hears, that it finds the
/// ends of turns itself, how many words it listens for, and the stream's own name.
pub fn ready(engine: &str, keyterms: usize, trace_id: &str) -> Value {
    json!({
        "type": "ready",
        "engine": engine,
        "turn_detection": "provider",
        "keyterms": keyterms,
        "trace_id": trace_id,
    })
}

/// What the phone is told when the engine says that something failed: the engine's own
/// account of it, the first three hundred signs.
pub fn failed(message: &Value) -> Option<Value> {
    let said = message.as_object()?;
    if !matches!(said.get("type")?.as_str()?, "Error" | "error") {
        return None;
    }
    let of = |name: &str| said.get(name).filter(|value| python::truthy(value));
    let account = of("description")
        .or_else(|| of("message"))
        .unwrap_or(message);
    let account = written(account);
    Some(json!({ "type": "error", "message": python::first(&account, 300) }))
}

/// What the phone is told last: how many turns ended on the stream.
pub fn closed(turns: u64) -> Value {
    json!({ "type": "closed", "turns": turns })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn what_the_engine_says_failed_is_told_in_its_own_account() {
        let told = |message: Value| failed(&message).map(|told| told["message"].clone());
        assert_eq!(
            told(json!({ "type": "Error", "description": "no credit", "message": "m" })),
            Some(json!("no credit"))
        );
        assert_eq!(
            told(json!({ "type": "error", "description": "", "message": "the message" })),
            Some(json!("the message"))
        );
        // With no account of its own, the whole of what it said, as Python shows it.
        assert_eq!(
            told(json!({ "type": "Error", "code": 7 })),
            Some(json!("{'type': 'Error', 'code': 7}"))
        );
        assert_eq!(
            told(json!({ "type": "Error", "description": 404 })),
            Some(json!("404"))
        );
        let long = "x".repeat(400);
        assert_eq!(
            told(json!({ "type": "Error", "description": long })),
            Some(json!("x".repeat(300)))
        );
        for other in [
            json!({ "type": "TurnInfo", "description": "no" }),
            json!({ "type": "ERROR" }),
            json!({ "description": "no" }),
            json!({ "type": 7 }),
            json!("Error"),
            json!(null),
        ] {
            assert_eq!(failed(&other), None, "{other}");
        }
        assert_eq!(
            failed(&json!({ "type": "Error", "description": "no" })),
            Some(json!({ "type": "error", "message": "no" }))
        );
        assert_eq!(
            ready("deepgram:flux-general-en", 2, "trace-1"),
            json!({
                "type": "ready", "engine": "deepgram:flux-general-en", "turn_detection": "provider",
                "keyterms": 2, "trace_id": "trace-1",
            })
        );
        assert_eq!(closed(3), json!({ "type": "closed", "turns": 3 }));
    }
}
