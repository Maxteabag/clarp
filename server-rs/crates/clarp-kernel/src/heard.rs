//! Speech in: what a person says to an agent, turned into text (`POST /transcribe`).
//!
//! An app records a clip and hands it to the Host under a name for the job. The Host has an
//! engine turn the clip into text and answers with the text and the trace the message will
//! travel under; the app then sends that text as the user's message. A request that comes
//! again under the same name, because its answer was lost on the way, is answered with what
//! was answered before: no engine runs twice for one thing said.
//!
//! This file holds the rules that are no engine's: what a job may be called, when two requests
//! are the same job, which engine runs a clip, which of an engine's segments are kept, when a
//! text is something an engine invented on silence, and what the answer looks like. The rules
//! and their words are the Python Host's (`transcription_results.py`, `stt_router.py`,
//! `stt.py`, `hallucinations.py`, `stt_providers.py`), held by `tests/speech_vectors.rs`.

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// The largest clip that is taken: 64 MiB.
pub const LARGEST_CLIP: usize = 64 * 1024 * 1024;

/// What a clip of no bytes, or of too many, is refused with.
pub const BAD_SIZE: &str = "bad size";
/// What a job's name that is none is refused with.
pub const INVALID_ID: &str = "invalid transcription id";
/// What a job's name is refused with when it comes again with another clip.
pub const REUSED_ID: &str = "transcription id reused for different audio";
/// What a Host says that has no engine at all.
pub const DISABLED: &str = "server transcription disabled";
/// What a Host says whose own engine is still loading its model.
pub const LOADING: &str = "whisper model loading";
/// What a Host says whose own engine did not come free in time.
pub const BUSY: &str = "whisper busy";

/// What an engine nobody installed is refused with.
pub fn not_installed(model: &str) -> String {
    format!("transcription model not installed: {model}")
}

/// What Python's `str.strip()` takes off both ends: what `str.isspace()` calls space.
fn is_space(sign: char) -> bool {
    sign.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&sign)
}

fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// The name an app gives a job, as it is kept: letters, digits, `.`, `_` and `-`, 128 at
/// most, without the space around it. A job without a name is one nobody can ask about
/// again: it has the empty name.
pub fn job_id(given: Option<&str>) -> Result<String, &'static str> {
    let name = strip(given.unwrap_or(""));
    let plain = |sign: char| sign.is_ascii_alphanumeric() || matches!(sign, '.' | '_' | '-');
    if !name.is_empty() && !(name.chars().count() <= 128 && name.chars().all(plain)) {
        return Err(INVALID_ID);
    }
    Ok(name.to_string())
}

/// What makes two requests the same job: the clip, what it says it is, the engine that was
/// asked for and whether it was said hands-free.
pub fn fingerprint(audio: &[u8], content_type: &str, model: &str, hands_free: bool) -> String {
    let mut digest = Sha256::new();
    digest.update(audio);
    digest.update([0]);
    digest.update(content_type.as_bytes());
    digest.update([0]);
    digest.update(model.as_bytes());
    digest.update([0]);
    digest.update(if hands_free { b"1" } else { b"0" });
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Whether a header says yes.
pub fn truthy(value: Option<&str>) -> bool {
    let said = strip(value.unwrap_or("")).to_lowercase();
    matches!(said.as_str(), "1" | "true" | "yes" | "on")
}

/// The whole number a header carries, when it carries a number: `12`, `12.9` (twelve),
/// `1e3`. An app's clock is sent this way.
pub fn whole(value: Option<&str>) -> Option<i64> {
    let text = strip(value?);
    // Python reads `1_000` as a thousand: an underscore between two digits is no sign.
    let signs: Vec<char> = text.chars().collect();
    let mut plain = String::with_capacity(text.len());
    for (at, sign) in signs.iter().enumerate() {
        if *sign == '_' {
            let digit = |place: Option<&char>| place.is_some_and(char::is_ascii_digit);
            let before = at.checked_sub(1).and_then(|at| signs.get(at));
            if !(digit(before) && digit(signs.get(at + 1))) {
                return None;
            }
            continue;
        }
        plain.push(*sign);
    }
    let number: f64 = plain.parse().ok()?;
    if !number.is_finite() || !plain.bytes().any(|byte| byte.is_ascii_digit()) {
        return None;
    }
    // Towards zero, as Python's `int()` cuts.
    #[allow(clippy::cast_possible_truncation)]
    Some(number.trunc() as i64)
}

/// Which engine runs a clip, and which are tried after it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Plan {
    /// The engine, by the name a client gives it. Empty: the Host's own.
    pub model: String,
    /// The engine the agent that is spoken to has of its own, when it has one.
    pub agent_engine: Option<String>,
    /// The agent's other engines, tried in order when the first fails.
    pub fallbacks: Vec<String>,
    /// The engine a long recording took the place of, when it took one's place.
    pub escalated_from: Option<String>,
}

/// Whether the cloud engine chosen in the settings could stand in for the Host's own: so,
/// whether it is worth reading.
pub fn wants_cloud_engine(requested: &str, agent_engines: &[String], in_call: bool) -> bool {
    let agent_engine = agent_engines.first().filter(|engine| !engine.is_empty());
    !in_call && agent_engine.is_none() && requested.is_empty()
}

/// Which engine runs a clip outside a call. An agent's own engine is more particular than
/// the phone's choice for every agent, so it comes before the header; a cloud engine chosen
/// in the settings stands in for the Host's own (a header still comes before it); a long
/// recording goes to the stronger engine set for long ones, also past a header, but never
/// past an agent's engine, which was chosen for its language.
pub fn plan(
    requested: &str,
    agent_engines: &[String],
    cloud_engine: &str,
    long_model: &str,
    in_call: bool,
) -> Plan {
    let agent_engine = agent_engines.first().filter(|engine| !engine.is_empty());
    let mut model = agent_engine.map_or(requested, String::as_str).to_string();
    if model.is_empty() && !in_call && !cloud_engine.is_empty() {
        model = cloud_engine.to_string();
    }
    let mut escalated_from = None;
    if !long_model.is_empty() && long_model != model && agent_engine.is_none() && !in_call {
        escalated_from = Some(std::mem::replace(&mut model, long_model.to_string()));
    }
    Plan {
        model,
        agent_engine: agent_engine.cloned(),
        fallbacks: agent_engines.iter().skip(1).cloned().collect(),
        escalated_from,
    }
}

/// The provider an engine's name says it is of (`elevenlabs:scribe_v2`), in lower case.
/// Empty for a name that says none.
pub fn provider_of(model: &str) -> String {
    match model.split_once(':') {
        Some((provider, _)) => strip(provider).to_lowercase(),
        None => String::new(),
    }
}

/// The providers of cloud engines the contract knows.
pub const CLOUD_PROVIDERS: [&str; 4] = ["deepgram", "elevenlabs", "cartesia", "google"];

/// Whether an engine's name is one of a cloud provider's.
pub fn is_cloud(model: &str) -> bool {
    CLOUD_PROVIDERS.contains(&provider_of(model).as_str())
}

/// The words an engine is asked to listen for, from the one line they are handed over in.
pub fn terms(payload: &str) -> Vec<String> {
    payload
        .split(',')
        .map(strip)
        .filter(|term| !term.is_empty())
        .map(str::to_string)
        .collect()
}

/// One piece of what the Host's own engine heard, with how likely it is that nobody spoke.
#[derive(Debug, Clone, PartialEq)]
pub struct Segment {
    pub text: String,
    pub no_speech_prob: f64,
}

/// What was heard, from the pieces the engine is sure enough were speech.
pub fn joined(segments: &[Segment]) -> String {
    let kept: String = segments
        .iter()
        .filter(|segment| segment.no_speech_prob < 0.6)
        .map(|segment| segment.text.as_str())
        .collect();
    strip(&kept).to_string()
}

/// What an engine trained on subtitles writes when nobody speaks: thanks to viewers, calls to
/// subscribe, the captions of music, single stray words. In lower case, without the signs
/// around them.
const INVENTED: &[&str] = &[
    "(applause)",
    "(music)",
    ".",
    "..",
    "...",
    "[applause]",
    "[music]",
    "a",
    "and",
    "bye",
    "bye bye",
    "don't forget to subscribe",
    "good evening",
    "good morning",
    "good night",
    "goodbye",
    "goodnight",
    "have a good night",
    "hm",
    "hmm",
    "hope you enjoyed it",
    "i hope you enjoyed",
    "i hope you enjoyed it",
    "i hope you enjoyed this video",
    "i'll see you in the next one",
    "like and subscribe",
    "like comment and subscribe",
    "mm-hmm",
    "mmm",
    "music",
    "ok",
    "okay",
    "please",
    "please subscribe",
    "please transfer",
    "see you",
    "see you in the next video",
    "see you next time",
    "subscribe",
    "thank you",
    "thank you for watching",
    "thank you so much",
    "thank you very much",
    "thanks",
    "thanks a lot",
    "thanks for watching",
    "thanks so much",
    "the",
    "uh-huh",
    "uhm",
    "um",
    "yeah",
    "you",
    "you know",
    "you know what i mean",
    "♪",
    "♪ ♪",
    "♪♪",
];

/// A sentence in lower case and without the space and the signs around it.
fn bare(sentence: &str) -> String {
    let lower = sentence.to_lowercase();
    let signs = [' ', '.', ',', '!', '?', '\u{2014}', '\u{2013}', '-'];
    strip(strip(&lower).trim_matches(signs)).to_string()
}

/// Whether a sentence is nothing but one stage direction, as subtitles have them: `(music)`,
/// `[applause]`. Words in brackets inside real speech are none.
fn is_caption(sentence: &str) -> bool {
    let sentence = strip(sentence);
    let inside = |open: char, close: char| {
        sentence
            .strip_prefix(open)
            .and_then(|rest| rest.strip_suffix(close))
            .is_some_and(|inner| !inner.contains(['(', ')', '[', ']', '\r', '\n']))
    };
    inside('(', ')') || inside('[', ']')
}

/// Whether a text is something an engine invented on silence or noise, every sentence of it.
/// Such a text is thrown away: nobody said it. (The list cannot tell an invented "Okay."
/// from one that was said, and throws both away.)
pub fn invented(text: &str) -> bool {
    let known = |sentence: &str| INVENTED.contains(&sentence) || is_caption(sentence);
    let whole = bare(text);
    if whole.is_empty() || known(&whole) {
        return true;
    }
    let sentences: Vec<String> = text
        .split(['.', '!', '?'])
        .filter(|sentence| !strip(sentence).is_empty())
        .map(bare)
        .filter(|sentence| !sentence.is_empty())
        .collect();
    !sentences.is_empty() && sentences.iter().all(|sentence| known(sentence))
}

/// What is kept of what an engine heard: nothing, when the engine invented it.
pub fn kept(heard: &str) -> String {
    let heard = strip(heard);
    if invented(heard) {
        String::new()
    } else {
        heard.to_string()
    }
}

/// Whether what was said ends as a sentence ends. An app that listens on waits less after
/// one that does.
pub fn ends_terminal(text: &str) -> bool {
    match text.trim_end_matches(is_space).chars().next_back() {
        Some(last) => matches!(last, '.' | '!' | '?'),
        // Nothing but space counts as ended, as the Python Host's rule has it; nothing at
        // all does not.
        None => !text.is_empty(),
    }
}

/// What a clip is called when it is handed to an engine over the network, by what it says
/// it is.
pub fn file_name(content_type: &str) -> &'static str {
    let kind = content_type.to_lowercase();
    if kind.contains("mp4") || kind.contains("m4a") {
        "audio.m4a"
    } else if kind.contains("wav") {
        "audio.wav"
    } else if kind.contains("ogg") {
        "audio.ogg"
    } else if kind.contains("mpeg") || kind.contains("mp3") {
        "audio.mp3"
    } else {
        "audio.webm"
    }
}

/// The ending of the file a clip is written to for the Host's own engine, which reads what
/// a clip is off its name.
pub fn suffix(content_type: &str) -> &'static str {
    if content_type.contains("mp4") || content_type.contains("m4a") {
        ".m4a"
    } else if content_type.contains("wav") {
        ".wav"
    } else if content_type.contains("ogg") {
        ".ogg"
    } else {
        ".webm"
    }
}

/// The answer to a clip that was turned into text: the text, whether it ends as a sentence
/// ends, and the trace the message travels under when the app sends it. The fields a Host
/// with heralds, an orchestrator and a vocabulary fills are there and say that none acted.
pub fn answer(text: &str, trace_id: &str, hands_free: bool) -> Value {
    json!({
        "text": text,
        "ends_terminal": ends_terminal(text),
        "trace_id": trace_id,
        "herald_consumed": false,
        "hands_free": hands_free,
        "orchestrator_skip_herald": false,
        "vocab_run_id": null,
        "cached": false,
    })
}

/// A kept answer as it is given again to a request that came twice.
pub fn again(mut kept: Value) -> Value {
    kept["cached"] = json!(true);
    kept
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn an_answer_has_the_python_hosts_fields_in_its_order() {
        let said = answer("Open the log.", "0123456789abcdef", true);
        let fields: Vec<&str> = said
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            fields,
            [
                "text",
                "ends_terminal",
                "trace_id",
                "herald_consumed",
                "hands_free",
                "orchestrator_skip_herald",
                "vocab_run_id",
                "cached"
            ]
        );
        assert_eq!(said["ends_terminal"], true);
        assert_eq!(said["cached"], false);
        assert_eq!(again(said)["cached"], true);
        assert_eq!(answer("", "t", false)["ends_terminal"], false);
    }

    #[test]
    fn what_an_engine_invented_is_not_kept() {
        assert_eq!(kept("  Thank you.  "), "");
        assert_eq!(kept("[Music]"), "");
        assert_eq!(kept("  Open the log. "), "Open the log.");
        assert_eq!(suffix("audio/mp4"), ".m4a");
        assert_eq!(
            suffix("AUDIO/WAV"),
            ".webm",
            "as it is written, like the Python Host"
        );
        assert_eq!(
            not_installed("x:y"),
            "transcription model not installed: x:y"
        );
    }
}
