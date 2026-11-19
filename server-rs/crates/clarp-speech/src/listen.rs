//! The port to what listens: an engine that turns a clip of speech into text.
//!
//! A Host can have several: one of its own on the machine ([`crate::whisper`]) and others it
//! reaches over the network ([`crate::scribe`]). Each is asked the same way and answers the
//! same way, so which one hears a clip is a setting, and another engine is another file here.
//! What is done with what an engine heard (which pieces are kept, what is thrown away as
//! invented) is not an engine's business: the Host does that the same for all of them.

use std::time::Duration;

/// A clip of speech as an app recorded it.
#[derive(Debug, Clone, Copy)]
pub struct Clip<'a> {
    pub audio: &'a [u8],
    /// What the app says the clip is (`audio/mp4`).
    pub content_type: &'a str,
}

/// What an engine is asked besides the clip.
#[derive(Debug, Clone, Copy)]
pub struct Asked<'a> {
    /// Words to listen for, in one line, set apart by commas: names, the words of a trade.
    /// An engine that takes none ignores them.
    pub listen_for: &'a str,
    /// The language that is spoken (`en`), when it is known. `None` leaves it to the engine.
    pub language: Option<&'a str>,
    /// How long to wait for an engine that hears one clip at a time and is hearing another.
    pub wait: Duration,
}

/// One piece of what an engine heard.
#[derive(Debug, Clone, PartialEq)]
pub struct Segment {
    pub text: String,
    /// How likely it is that nobody spoke in this piece. 0 from an engine that does not say.
    pub no_speech_prob: f64,
}

/// What an engine heard in a clip.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Heard {
    pub segments: Vec<Segment>,
    /// How long the clip is, where the engine says so, or how long the engine took: for the
    /// record of the clip, never for the answer.
    pub seconds: f64,
}

impl Heard {
    /// What an engine heard that answers with one text.
    pub fn text(text: impl Into<String>, seconds: f64) -> Self {
        Self {
            segments: vec![Segment {
                text: text.into(),
                no_speech_prob: 0.0,
            }],
            seconds,
        }
    }
}

/// Why an engine heard nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Deaf {
    /// The engine is still getting ready: its model is being loaded.
    Loading,
    /// The engine hears one clip at a time and did not come free in time.
    Busy,
    /// It failed. The words are for whoever asked and for the log, and hold nothing of
    /// what was said.
    Failed(String),
}

/// An engine that turns speech into text.
pub trait Listener: Send + Sync {
    /// Turns a clip into text. It may take as long as the clip is long, so it is never
    /// called where nothing may wait.
    fn hear(&self, clip: Clip<'_>, asked: Asked<'_>) -> Result<Heard, Deaf>;

    /// A call report records the constraint at the adapter boundary, also when it fails.
    fn hear_observed(
        &self,
        clip: Clip<'_>,
        asked: Asked<'_>,
        observed: &mut Option<String>,
    ) -> Result<Heard, Deaf> {
        *observed = Some(asked.language.unwrap_or("auto").to_string());
        self.hear(clip, asked)
    }

    /// Whether the engine could hear a clip now.
    fn ready(&self) -> bool {
        true
    }
}

/// Whether Python's `str.strip()` would take this sign off an end: what `str.isspace()`
/// calls space. The rules of speech are the Python Host's, and are held to its answers.
pub fn is_space(sign: char) -> bool {
    sign.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&sign)
}

/// A text without the space around it, as Python's `str.strip()` leaves it.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// The words an engine is asked to listen for, from the one line they are handed over in.
pub fn terms(listen_for: &str) -> Vec<String> {
    listen_for
        .split(',')
        .map(strip)
        .filter(|term| !term.is_empty())
        .map(str::to_string)
        .collect()
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

/// The ending of the file a clip is written to for an engine that reads what a clip is off
/// its name.
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_small_rules_of_a_clip() {
        assert_eq!(terms(" Clarp , Maximus,, "), ["Clarp", "Maximus"]);
        assert!(terms(" , ").is_empty());
        assert_eq!(file_name("Audio/MP4"), "audio.m4a");
        assert_eq!(file_name(""), "audio.webm");
        assert_eq!(suffix("audio/mp4"), ".m4a");
        assert_eq!(
            suffix("AUDIO/WAV"),
            ".webm",
            "as it is written, like the Python Host"
        );
        assert_eq!(strip("\u{1c} a \u{a0}"), "a");
        let heard = Heard::text("Open the log.", 1.5);
        assert_eq!(heard.segments.len(), 1);
        assert!(heard.segments[0].no_speech_prob < 0.6);
    }
}
