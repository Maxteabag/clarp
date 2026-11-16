//! The lines an agent says aloud, and what became of each.
//!
//! What an agent marks for the ear in a reply is spoken: each marked part, in pieces an
//! engine takes, is a line. A line is written into the agent's log in the same breath as its
//! reply, so a line waits to be spoken exactly when its reply is in the conversation, and a
//! Host that is started again finds what it still owes the ear.
//!
//! A line goes one way: it waits, an engine speaks it into a clip, the clip is whole or the
//! engine failed. One line of an agent is spoken at a time, in the order they were said. A
//! line an engine had begun long before a restart is not begun again: whoever was listening
//! heard its beginning then, or was not listening any more (the Python Host's rule for its
//! queue of speech, `tts_queue_records.py` and `tts_worker.recover_in_flight`).

use clarp_speech::markup::{speak_blocks, spoken_chunks_for_tts};
use serde::{Deserialize, Serialize};

/// The most signs of one line: what an engine takes of a text at once.
pub const MOST_SIGNS: usize = 1_800;

/// How long ago an engine may have begun a line for the line to be begun again after a
/// restart.
pub const BEGUN_AGAIN_WITHIN_MS: i64 = 2 * 60 * 1_000;

/// What is said of a line that was being spoken when its Host went away, too long ago.
pub const CUT_OFF: &str = "the Host was started again while this was being spoken";

/// How a line stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum LineState {
    /// It waits to be spoken.
    #[default]
    Queued,
    /// An engine is speaking it: its clip is being made.
    Speaking,
    /// Its clip is whole.
    Spoken,
    /// No engine spoke it, or not all of it. `error` says why.
    Failed,
    /// It was taken back before it was spoken: its turn was stopped.
    Dropped,
}

impl LineState {
    /// Whether nothing more happens to a line that stands so.
    pub fn is_over(self) -> bool {
        matches!(self, Self::Spoken | Self::Failed | Self::Dropped)
    }
}

fn is_false(truth: &bool) -> bool {
    !*truth
}

/// A line an agent says aloud, as a change left it.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Line {
    /// `s-`, the reply's id without its `r-`, and the line's place among the reply's lines.
    pub id: String,
    pub turn: u64,
    /// The reply it is a piece of.
    pub reply: String,
    /// What is said: the piece as the agent wrote it, as an engine is handed it.
    pub text: String,
    /// It begins a part of the reply that is for the ear. An agent the user is not looking
    /// at says who speaks there.
    #[serde(default, skip_serializing_if = "is_false")]
    pub first: bool,
    /// The trace of the turn that said it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    pub state: LineState,
    /// The clip it became: its number on this Host.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub clip_id: Option<u64>,
    /// The name of the clip's file.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub clip: Option<String>,
    /// How many bytes the clip has, once it is whole.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bytes: Option<u64>,
    /// The engine that spoke it, and the voice.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voice: Option<String>,
    /// What an app last said of playing the clip (`play-start`, `play-ok`, `play-fail`, ...).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub played: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub queued_at_ms: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claimed_at_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at_ms: Option<i64>,
}

/// The lines of a reply: each part it marks for the ear, in pieces an engine takes. A reply
/// that marks nothing has none: what is not marked is read, not heard.
pub fn lines_of(
    reply: &str,
    turn: u64,
    text: &str,
    trace_id: Option<&str>,
    now_ms: i64,
) -> Vec<Line> {
    let name = reply.strip_prefix("r-").unwrap_or(reply);
    let mut lines = Vec::new();
    for block in speak_blocks(text, false) {
        for (place, piece) in spoken_chunks_for_tts(block.trim(), MOST_SIGNS)
            .into_iter()
            .enumerate()
        {
            lines.push(Line {
                id: format!("s-{name}-{}", lines.len() + 1),
                turn,
                reply: reply.to_string(),
                text: piece,
                first: place == 0,
                trace_id: trace_id.map(str::to_string),
                queued_at_ms: now_ms,
                ..Line::default()
            });
        }
    }
    lines
}

/// Why a line cannot go the way it was asked to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refused {
    /// The agent has no such line.
    Unknown,
    /// The line does not stand where this begins: it was spoken, dropped, or taken already.
    NotNow,
}

impl Line {
    /// An engine takes the line: it is being spoken from now.
    pub fn claimed(&self, now_ms: i64) -> Result<Self, Refused> {
        if self.state != LineState::Queued {
            return Err(Refused::NotNow);
        }
        Ok(Self {
            state: LineState::Speaking,
            claimed_at_ms: Some(now_ms),
            ..self.clone()
        })
    }

    /// The line has a clip: its number, its file, the engine and the voice that speak it.
    pub fn begun(
        &self,
        clip_id: u64,
        clip: &str,
        provider: &str,
        voice: &str,
    ) -> Result<Self, Refused> {
        if self.state != LineState::Speaking {
            return Err(Refused::NotNow);
        }
        Ok(Self {
            clip_id: Some(clip_id),
            clip: Some(clip.to_string()),
            provider: Some(provider.to_string()),
            voice: Some(voice.to_string()),
            ..self.clone()
        })
    }

    /// The engine is done with the line: its clip is whole, or it failed and says why.
    pub fn ended(&self, outcome: Result<u64, &str>, now_ms: i64) -> Result<Self, Refused> {
        if self.state != LineState::Speaking {
            return Err(Refused::NotNow);
        }
        let mut line = self.clone();
        line.ended_at_ms = Some(now_ms);
        match outcome {
            Ok(bytes) => {
                line.state = LineState::Spoken;
                line.bytes = Some(bytes);
            }
            Err(why) => {
                line.state = LineState::Failed;
                line.error = Some(why.chars().take(300).collect());
            }
        }
        Ok(line)
    }

    /// The line is taken back before anything spoke it.
    pub fn dropped(&self, now_ms: i64) -> Result<Self, Refused> {
        if self.state != LineState::Queued {
            return Err(Refused::NotNow);
        }
        Ok(Self {
            state: LineState::Dropped,
            ended_at_ms: Some(now_ms),
            ..self.clone()
        })
    }

    /// What an app says of playing the line's clip. A clip that was played is not said to
    /// be waiting again.
    pub fn played(&self, status: &str, error: Option<&str>) -> Self {
        let mut line = self.clone();
        line.played = Some(status.to_string());
        if status == "play-fail" {
            line.error = Some(error.unwrap_or("").chars().take(300).collect());
        }
        line
    }
}

/// What an app may say of a clip, in the contract's words.
pub const PLAYED: [&str; 7] = [
    "synthesized",
    "broadcast",
    "queued",
    "held",
    "play-start",
    "play-ok",
    "play-fail",
];

/// The lines an agent says aloud, as its log has them, in the order they were said.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SpeechBook {
    lines: Vec<Line>,
}

impl SpeechBook {
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes a line as a change left it.
    pub fn apply(&mut self, line: &Line) {
        match self.lines.iter_mut().find(|known| known.id == line.id) {
            Some(known) => *known = line.clone(),
            None => self.lines.push(line.clone()),
        }
    }

    pub fn get(&self, id: &str) -> Option<&Line> {
        self.lines.iter().find(|line| line.id == id)
    }

    /// The line that became the clip with this number.
    pub fn by_clip(&self, clip_id: u64) -> Option<&Line> {
        self.lines.iter().find(|line| line.clip_id == Some(clip_id))
    }

    /// The line whose clip's file has this name.
    pub fn by_file(&self, name: &str) -> Option<&Line> {
        self.lines
            .iter()
            .find(|line| line.clip.as_deref() == Some(name))
    }

    pub fn lines(&self) -> &[Line] {
        &self.lines
    }

    /// The line that is spoken next: the oldest that waits, when none of the agent's is
    /// being spoken. One line of an agent at a time, in the order they were said.
    pub fn next(&self) -> Option<&Line> {
        if self
            .lines
            .iter()
            .any(|line| line.state == LineState::Speaking)
        {
            return None;
        }
        self.lines
            .iter()
            .find(|line| line.state == LineState::Queued)
    }

    /// The lines that still wait.
    pub fn waiting(&self) -> impl Iterator<Item = &Line> {
        self.lines
            .iter()
            .filter(|line| line.state == LineState::Queued)
    }

    /// What a Host that was started again makes of the lines an engine was speaking when it
    /// went: one begun a moment ago waits again, one begun long ago is over. Answers the
    /// lines as they stand now, for the log.
    pub fn after_a_restart(&self, now_ms: i64) -> Vec<Line> {
        let speaking = self
            .lines
            .iter()
            .filter(|line| line.state == LineState::Speaking);
        speaking
            .map(|line| {
                let begun = line.claimed_at_ms.unwrap_or(line.queued_at_ms);
                let mut line = line.clone();
                if now_ms - begun <= BEGUN_AGAIN_WITHIN_MS {
                    line.state = LineState::Queued;
                    line.claimed_at_ms = None;
                    (line.clip_id, line.clip, line.provider, line.voice) = (None, None, None, None);
                } else {
                    line.state = LineState::Failed;
                    line.error = Some(CUT_OFF.to_string());
                    line.ended_at_ms = Some(now_ms);
                }
                line
            })
            .collect()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    const REPLY: &str = "I looked.\n<speak>The build is green. The gate too.</speak>\nDetails \
                         below.\n<speak>Shall I land it?</speak>";

    #[test]
    fn what_a_reply_marks_for_the_ear_becomes_its_lines() {
        let lines = lines_of("r-abc", 7, REPLY, Some("trace-1"), 1_000);
        let said: Vec<(&str, &str, bool)> = lines
            .iter()
            .map(|line| (line.id.as_str(), line.text.as_str(), line.first))
            .collect();
        assert_eq!(
            said,
            [
                ("s-abc-1", "The build is green. The gate too.", true),
                ("s-abc-2", "Shall I land it?", true),
            ]
        );
        for line in &lines {
            assert_eq!(
                (
                    line.turn,
                    line.reply.as_str(),
                    line.state,
                    line.queued_at_ms
                ),
                (7, "r-abc", LineState::Queued, 1_000)
            );
            assert_eq!(line.trace_id.as_deref(), Some("trace-1"));
        }
        // What is not marked is read, not heard; and neither is a mark that never closes.
        assert!(lines_of("r-x", 1, "Nothing for the ear here.", None, 0).is_empty());
        assert!(lines_of("r-x", 1, "<speak>never closed", None, 0).is_empty());
        assert!(lines_of("r-x", 1, "<speak>   </speak>", None, 0).is_empty());
        // A long part is several lines, and only its first begins the part.
        let long = format!(
            "<speak>{}</speak>",
            "A sentence of some length. ".repeat(120)
        );
        let pieces = lines_of("r-long", 1, &long, None, 0);
        assert!(pieces.len() > 1, "{}", pieces.len());
        assert!(
            pieces
                .iter()
                .all(|line| line.text.chars().count() <= MOST_SIGNS)
        );
        let firsts: Vec<bool> = pieces.iter().map(|line| line.first).collect();
        assert!(firsts[0] && firsts[1..].iter().all(|first| !first));
        assert_eq!(pieces[1].id, "s-long-2");
    }

    #[test]
    fn a_line_goes_one_way() {
        let line = lines_of("r-abc", 7, REPLY, None, 1_000).remove(0);
        // It waits; an engine takes it; it has a clip; the clip is whole.
        assert_eq!(
            line.begun(1, "c.mp3", "gemini", "Kore"),
            Err(Refused::NotNow)
        );
        assert_eq!(line.ended(Ok(9), 2_000), Err(Refused::NotNow));
        let taken = line.claimed(1_100).unwrap();
        assert_eq!(
            (taken.state, taken.claimed_at_ms),
            (LineState::Speaking, Some(1_100))
        );
        assert_eq!(taken.claimed(1_200), Err(Refused::NotNow), "taken once");
        assert_eq!(
            taken.dropped(1_200),
            Err(Refused::NotNow),
            "what is being spoken is not taken back"
        );
        let begun = taken
            .begun(41, "1000__maximus.mp3", "gemini", "Kore")
            .unwrap();
        assert_eq!(
            (
                begun.clip_id,
                begun.clip.as_deref(),
                begun.provider.as_deref(),
                begun.voice.as_deref()
            ),
            (
                Some(41),
                Some("1000__maximus.mp3"),
                Some("gemini"),
                Some("Kore")
            )
        );
        let spoken = begun.ended(Ok(2_048), 1_900).unwrap();
        assert_eq!(
            (
                spoken.state,
                spoken.bytes,
                spoken.ended_at_ms,
                spoken.error.as_deref()
            ),
            (LineState::Spoken, Some(2_048), Some(1_900), None)
        );
        assert!(spoken.state.is_over());
        assert_eq!(
            spoken.ended(Ok(1), 2_000),
            Err(Refused::NotNow),
            "over once"
        );
        // Or the engine failed, and says why in few words.
        let failed = begun.ended(Err(&"x".repeat(400)), 1_900).unwrap();
        assert_eq!(failed.state, LineState::Failed);
        assert_eq!(failed.error.as_deref().map(str::len), Some(300));
        // Or it is taken back before anything spoke it.
        let back = line.dropped(1_050).unwrap();
        assert_eq!(
            (back.state, back.ended_at_ms),
            (LineState::Dropped, Some(1_050))
        );
        assert_eq!(back.claimed(1_100), Err(Refused::NotNow));
        // What an app says of playing the clip is kept, with why when it could not.
        let playing = spoken.played("play-start", None);
        assert_eq!(
            (playing.played.as_deref(), playing.error.as_deref()),
            (Some("play-start"), None)
        );
        let unplayed = spoken.played("play-fail", Some("no audio session"));
        assert_eq!(unplayed.error.as_deref(), Some("no audio session"));
        assert!(PLAYED.contains(&"play-ok") && !PLAYED.contains(&"played"));
    }

    #[test]
    fn one_line_of_an_agent_is_spoken_at_a_time_in_the_order_they_were_said() {
        let mut book = SpeechBook::new();
        assert!(book.next().is_none());
        let lines = lines_of("r-abc", 7, REPLY, None, 1_000);
        for line in &lines {
            book.apply(line);
        }
        assert_eq!(book.next().map(|line| line.id.as_str()), Some("s-abc-1"));
        assert_eq!(book.waiting().count(), 2);
        let taken = lines[0].claimed(1_100).unwrap();
        book.apply(&taken);
        assert!(book.next().is_none(), "one is being spoken");
        let begun = taken.begun(41, "a.mp3", "gemini", "Kore").unwrap();
        book.apply(&begun);
        assert_eq!(
            book.by_clip(41).map(|line| line.id.as_str()),
            Some("s-abc-1")
        );
        assert_eq!(
            book.by_file("a.mp3").map(|line| line.id.as_str()),
            Some("s-abc-1")
        );
        assert!(book.by_clip(42).is_none() && book.by_file("b.mp3").is_none());
        book.apply(&begun.ended(Ok(10), 1_500).unwrap());
        assert_eq!(book.next().map(|line| line.id.as_str()), Some("s-abc-2"));
        assert_eq!(book.lines().len(), 2);
        assert_eq!(
            book.get("s-abc-1").map(|line| line.state),
            Some(LineState::Spoken)
        );
    }

    #[test]
    fn a_line_begun_long_before_a_restart_is_not_begun_again() {
        let mut book = SpeechBook::new();
        let lines = lines_of("r-abc", 7, REPLY, None, 1_000);
        let recent = lines[0]
            .claimed(100_000)
            .unwrap()
            .begun(1, "a.mp3", "gemini", "Kore")
            .unwrap();
        let old = lines[1].claimed(10_000).unwrap();
        book.apply(&recent);
        book.apply(&old);
        // The Host comes back a little over two minutes after the older one was begun.
        let now = 10_000 + BEGUN_AGAIN_WITHIN_MS + 1;
        let after = book.after_a_restart(now);
        assert_eq!(after.len(), 2);
        let again = &after[0];
        assert_eq!(
            (
                again.state,
                again.claimed_at_ms,
                again.clip_id,
                again.clip.as_deref()
            ),
            (LineState::Queued, None, None, None),
            "begun a moment ago: it waits again, without the clip that was cut off"
        );
        let over = &after[1];
        assert_eq!(
            (over.state, over.error.as_deref(), over.ended_at_ms),
            (LineState::Failed, Some(CUT_OFF), Some(now))
        );
        // What waits and what is over is left as it is.
        let mut settled = SpeechBook::new();
        settled.apply(&lines[0]);
        settled.apply(&lines[1].dropped(5).unwrap());
        assert!(settled.after_a_restart(now).is_empty());
    }
}
