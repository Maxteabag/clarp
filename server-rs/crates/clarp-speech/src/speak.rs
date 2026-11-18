//! The port to what speaks: an engine that turns a line of text into audio.
//!
//! A Host can have several, and tries them in the order its owner chose: when the first
//! cannot speak, the next does. One rule stands above the order: **what was begun in one
//! voice is not begun again in another.** An engine that fails after it handed over audio
//! has spoken the beginning of the line, and a listener has heard it; the line is not said
//! again from its start by the next engine. That is why a failure says whether audio had
//! begun ([`Mute::Failed`]).
//!
//! The audio is MP3, handed over piece by piece as it comes, so that a listener hears the
//! beginning of a long line while its end is still being made.

/// What is to be spoken, and in which voice.
#[derive(Debug, Clone, Copy)]
pub struct Line<'a> {
    pub text: &'a str,
    /// The voice, by the engine's own name for it.
    pub voice: &'a str,
}

/// Why a line was not spoken, or not all of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mute {
    /// The engine cannot be used as it stands: no key, no voice, no encoder. Nothing was
    /// tried, and another engine may be.
    Unavailable(String),
    /// It failed. `begun` says whether audio had already been handed over. The words are for
    /// the log and hold nothing of what was to be said.
    Failed { why: String, begun: bool },
}

impl Mute {
    /// Whether another engine may say the line from its start.
    pub fn may_be_said_by_another(&self) -> bool {
        match self {
            Self::Unavailable(_) => true,
            Self::Failed { begun, .. } => !begun,
        }
    }
}

/// An engine that speaks.
pub trait Speaker: Send + Sync {
    /// Speaks a line. The audio is handed to `sink` piece by piece as it comes; the answer
    /// is how many bytes it was. It takes as long as the line takes to make, so it is never
    /// called where nothing may wait.
    fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute>;
    /// Gemini interprets emotion tags; ordinary synthesis strips them.
    fn speak_turn(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        let text = crate::markup::strip_emotion_tags(line.text);
        self.speak(
            Line {
                text: &text,
                ..line
            },
            sink,
        )
    }
}

/// Says a line with the first of these engines that can: the next is tried when the one
/// before could not begin, and never after one that had begun. Answers which engine spoke
/// (its place in the list) and how many bytes; or why none did, the last reason first.
pub fn said_by_the_first_that_can(
    engines: &[(&dyn Speaker, &str)],
    text: &str,
    sink: &mut dyn FnMut(&[u8]),
) -> Result<(usize, u64), Mute> {
    let mut last = Mute::Unavailable("no engine that speaks is configured".to_string());
    for (place, (engine, voice)) in engines.iter().enumerate() {
        match engine.speak_turn(Line { text, voice }, sink) {
            Ok(bytes) => return Ok((place, bytes)),
            Err(mute) => {
                let another = mute.may_be_said_by_another();
                last = mute;
                if !another {
                    break;
                }
            }
        }
    }
    Err(last)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An engine that hands over what it is told to and then ends as it is told to.
    struct Told(&'static [u8], Option<Mute>);

    impl Speaker for Told {
        fn speak(&self, _line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
            if !self.0.is_empty() {
                sink(self.0);
            }
            match &self.1 {
                Some(mute) => Err(mute.clone()),
                None => Ok(self.0.len() as u64),
            }
        }
    }

    fn failed(begun: bool) -> Mute {
        Mute::Failed {
            why: "the provider is down".to_string(),
            begun,
        }
    }

    #[test]
    fn the_next_engine_speaks_when_the_first_could_not_begin_and_never_after_it_had() {
        let no_key = Told(b"", Some(Mute::Unavailable("no key".to_string())));
        let down = Told(b"", Some(failed(false)));
        let cut_off = Told(b"begin", Some(failed(true)));
        let works = Told(b"audio", None);
        let say = |engines: &[(&dyn Speaker, &str)]| {
            let mut heard = Vec::new();
            let said = said_by_the_first_that_can(engines, "a line", &mut |piece| {
                heard.extend_from_slice(piece);
            });
            (said, heard)
        };
        // The first that can speaks.
        assert_eq!(
            say(&[(&works, "a"), (&down, "b")]),
            (Ok((0, 5)), b"audio".to_vec())
        );
        // One that has no key, one that is down before any audio: the next is tried.
        assert_eq!(
            say(&[(&no_key, "a"), (&down, "b"), (&works, "c")]),
            (Ok((2, 5)), b"audio".to_vec())
        );
        // One that failed after it had begun: nobody says the line again from its start.
        let (said, heard) = say(&[(&cut_off, "a"), (&works, "b")]);
        assert_eq!(said, Err(failed(true)));
        assert_eq!(heard, b"begin");
        // Nobody can: the last reason is the answer.
        assert_eq!(say(&[(&no_key, "a"), (&down, "b")]).0, Err(failed(false)));
        assert!(matches!(say(&[]).0, Err(Mute::Unavailable(_))));
    }
}
