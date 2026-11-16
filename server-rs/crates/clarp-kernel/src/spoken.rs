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

/// How long a line may have waited when its Host is started again for it still to be
/// spoken. A Host that was away for longer says nothing of what was said before it went:
/// by then it is no news to the ear.
pub const WAITS_AT_MOST_MS: i64 = 30 * 60 * 1_000;

/// What is said of a line that waited for a Host that was away too long.
pub const WAITED_TOO_LONG: &str = "the Host was away for too long for this still to be spoken";

/// How long a line that is over is kept in mind, and its clip kept to be played again: the
/// Python Host's week of retained replies (`REPLAY_RETAIN_SEC`). The log keeps every line.
pub const KEPT_FOR_MS: i64 = 7 * 24 * 60 * 60 * 1_000;

/// How long ago a clip may have been announced to be offered again to an app that did not
/// say it played it, and how many such clips are offered at once (`recoverable_events`).
pub const OFFERED_AGAIN_WITHIN_MS: i64 = 10 * 60 * 1_000;
pub const OFFERED_AGAIN_AT_MOST: usize = 3;
/// How many clips an app said it holds back are offered again.
pub const HELD_AT_MOST: usize = 64;

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
    /// The engine that spoke it, and the voice: while it is being spoken, the ones that
    /// were asked first.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voice: Option<String>,
    /// The number of the event its clip was announced with.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_id: Option<u64>,
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

/// A line that was spoken to its end: how many bytes its clip has, and who spoke it. That
/// is the engine that was asked first, or the one that spoke when the first could not begin.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Said<'a> {
    pub bytes: u64,
    pub provider: &'a str,
    pub voice: &'a str,
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

    /// The line has a clip: its number, its file, the engine and the voice that are asked
    /// first, and the number of the event the clip was announced with. A line has one clip.
    pub fn begun(
        &self,
        clip: (u64, &str),
        asked: (&str, &str),
        event_id: Option<u64>,
    ) -> Result<Self, Refused> {
        if self.state != LineState::Speaking || self.clip_id.is_some() {
            return Err(Refused::NotNow);
        }
        Ok(Self {
            clip_id: Some(clip.0),
            clip: Some(clip.1.to_string()),
            provider: Some(asked.0.to_string()),
            voice: Some(asked.1.to_string()),
            event_id,
            ..self.clone()
        })
    }

    /// The one who took the line is done with it: its clip is whole, or it was not spoken,
    /// or not all of it, and why. Whoever ends a line names the clip it made of it, or
    /// none when it made none: a line that waits again after its Host was opened anew, or
    /// that someone else took since, is not ended by the one who had it before.
    pub fn ended(
        &self,
        clip: Option<u64>,
        outcome: Result<Said<'_>, &str>,
        now_ms: i64,
    ) -> Result<Self, Refused> {
        if self.state != LineState::Speaking || self.clip_id != clip {
            return Err(Refused::NotNow);
        }
        let mut line = self.clone();
        line.ended_at_ms = Some(now_ms);
        match outcome {
            Ok(said) => {
                line.state = LineState::Spoken;
                line.bytes = Some(said.bytes);
                line.provider = Some(said.provider.to_string());
                line.voice = Some(said.voice.to_string());
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

    /// What an app says of playing the line's clip. A clip an app said it played to its
    /// end, or could not play, is not said to be waiting again: only what is said of
    /// playing it once more takes the place of that.
    pub fn played(&self, status: &str, error: Option<&str>) -> Self {
        let mut line = self.clone();
        let playing = status.starts_with("play-");
        if self.heard_or_given_up() && !playing {
            return line;
        }
        line.played = Some(status.to_string());
        if status == "play-fail" {
            line.error = Some(error.unwrap_or("").chars().take(300).collect());
        }
        line
    }

    /// Whether an app said it played the clip to its end, or could not play it.
    pub fn heard_or_given_up(&self) -> bool {
        matches!(self.played.as_deref(), Some("play-ok" | "play-fail"))
    }
}

/// Which clip an app means when it says something of playing one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Meant {
    /// It names none by number: the clip is the one its address names.
    ByAddress,
    Clip(u64),
    /// A number no clip has.
    NoClip,
}

/// What an app sent for a clip's number is no number at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NoNumber;

/// Reads the number of a clip as an app sends it, the way the Python Host does
/// (`int(clip_id)` unless it is null or empty): a number, a text of digits, a truth.
pub fn clip_meant(value: Option<&serde_json::Value>) -> Result<Meant, NoNumber> {
    use serde_json::Value;
    let number: i128 = match value {
        None | Some(Value::Null) => return Ok(Meant::ByAddress),
        Some(Value::String(text)) if text.is_empty() => return Ok(Meant::ByAddress),
        Some(Value::Bool(truth)) => i128::from(*truth),
        Some(Value::Number(number)) => match (number.as_i64(), number.as_u64(), number.as_f64()) {
            (Some(whole), _, _) => i128::from(whole),
            (_, Some(whole), _) => i128::from(whole),
            // Towards zero, as Python's `int()` cuts.
            #[allow(clippy::cast_possible_truncation)]
            (_, _, Some(float)) if float.is_finite() => float.trunc() as i128,
            _ => return Err(NoNumber),
        },
        Some(Value::String(text)) => crate::python::int(text).ok_or(NoNumber)?,
        Some(Value::Array(_) | Value::Object(_)) => return Err(NoNumber),
    };
    Ok(match u64::try_from(number) {
        // Nothing is numbered zero: the clip is looked for by its address.
        Ok(0) => Meant::ByAddress,
        Ok(clip) => Meant::Clip(clip),
        Err(_) => Meant::NoClip,
    })
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

    /// The lines of one reply, in the order they were said.
    pub fn of_reply<'a>(&'a self, reply: &'a str) -> impl Iterator<Item = &'a Line> {
        self.lines.iter().filter(move |line| line.reply == reply)
    }

    /// What a Host that was started again makes of the lines it finds unspoken. One an
    /// engine was speaking when it went waits again when it was begun a moment ago, and is
    /// over when it was begun long ago. One that has waited for longer than a Host is
    /// away for an update is not spoken any more. Answers the lines as they stand now, for
    /// the log.
    pub fn after_a_restart(&self, now_ms: i64) -> Vec<Line> {
        let mut changed = Vec::new();
        for line in &self.lines {
            let mut line = line.clone();
            match line.state {
                LineState::Speaking => {
                    let begun = line.claimed_at_ms.unwrap_or(line.queued_at_ms);
                    if now_ms - begun <= BEGUN_AGAIN_WITHIN_MS {
                        line.state = LineState::Queued;
                        line.claimed_at_ms = None;
                        (line.clip_id, line.clip, line.event_id) = (None, None, None);
                        (line.provider, line.voice) = (None, None);
                    } else {
                        line.state = LineState::Failed;
                        line.error = Some(CUT_OFF.to_string());
                        line.ended_at_ms = Some(now_ms);
                    }
                }
                LineState::Queued if now_ms - line.queued_at_ms > WAITS_AT_MOST_MS => {
                    line.state = LineState::Dropped;
                    line.error = Some(WAITED_TOO_LONG.to_string());
                    line.ended_at_ms = Some(now_ms);
                }
                _ => continue,
            }
            changed.push(line);
        }
        changed
    }

    /// Forgets the lines that were over before `before_ms`. The log keeps them.
    pub fn forget_before(&mut self, before_ms: i64) {
        self.lines.retain(|line| {
            !line.state.is_over() || line.ended_at_ms.unwrap_or(line.queued_at_ms) >= before_ms
        });
    }
}

/// Which of these lines' clips are offered again to an app that asks what it missed, in
/// the order they were announced: the ones an app said it holds back, and the newest few
/// that were announced a moment ago and that no app said it played to the end or could not
/// play. A clip no engine finished is not offered (the Python Host's `recoverable_events`).
/// Answers their places in `lines`.
pub fn offered_again(lines: &[&Line], now_ms: i64) -> Vec<usize> {
    let announced = |place: &usize| {
        let line = lines[*place];
        line.clip_id.is_some() && line.state != LineState::Failed
    };
    // The newest first: by the event that announced the clip, then by the clip's number.
    let mut newest: Vec<usize> = (0..lines.len()).filter(announced).collect();
    newest.sort_by_key(|place| std::cmp::Reverse((lines[*place].event_id, lines[*place].clip_id)));
    let held = |place: &&usize| lines[**place].played.as_deref() == Some("held");
    let recent = |place: &&usize| {
        let line = lines[**place];
        let at = line.claimed_at_ms.unwrap_or(line.queued_at_ms);
        at >= now_ms - OFFERED_AGAIN_WITHIN_MS
            && line.played.as_deref() != Some("held")
            && !line.heard_or_given_up()
    };
    let mut offered: Vec<usize> = newest
        .iter()
        .filter(held)
        .take(HELD_AT_MOST)
        .copied()
        .collect();
    offered.extend(newest.iter().filter(recent).take(OFFERED_AGAIN_AT_MOST));
    offered.sort_by_key(|place| (lines[*place].event_id, lines[*place].clip_id));
    offered
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
        let kore = |bytes| Said {
            bytes,
            provider: "gemini",
            voice: "Kore",
        };
        assert_eq!(
            line.begun((1, "c.mp3"), ("gemini", "Kore"), None),
            Err(Refused::NotNow)
        );
        assert_eq!(line.ended(None, Ok(kore(9)), 2_000), Err(Refused::NotNow));
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
            .begun((41, "1000__maximus.mp3"), ("gemini", "Kore"), Some(7))
            .unwrap();
        assert_eq!(
            (
                begun.clip_id,
                begun.clip.as_deref(),
                begun.provider.as_deref(),
                begun.voice.as_deref(),
                begun.event_id
            ),
            (
                Some(41),
                Some("1000__maximus.mp3"),
                Some("gemini"),
                Some("Kore"),
                Some(7)
            )
        );
        assert_eq!(
            begun.begun((42, "other.mp3"), ("gemini", "Kore"), None),
            Err(Refused::NotNow),
            "a line has one clip"
        );
        // Whoever ends it names the clip it made of it.
        assert_eq!(begun.ended(None, Ok(kore(1)), 1_900), Err(Refused::NotNow));
        assert_eq!(
            begun.ended(Some(40), Ok(kore(1)), 1_900),
            Err(Refused::NotNow)
        );
        let spoken = begun.ended(Some(41), Ok(kore(2_048)), 1_900).unwrap();
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
            spoken.ended(Some(41), Ok(kore(1)), 2_000),
            Err(Refused::NotNow),
            "over once"
        );
        // The engine that spoke is the one that is written: the first could not begin.
        let by_another = Said {
            bytes: 5,
            provider: "cartesia",
            voice: "c-voice",
        };
        let other = begun.ended(Some(41), Ok(by_another), 1_900).unwrap();
        assert_eq!(
            (other.provider.as_deref(), other.voice.as_deref()),
            (Some("cartesia"), Some("c-voice"))
        );
        // Or the engine failed, and says why in few words.
        let failed = begun.ended(Some(41), Err(&"x".repeat(400)), 1_900).unwrap();
        assert_eq!(failed.state, LineState::Failed);
        assert_eq!(failed.error.as_deref().map(str::len), Some(300));
        // Or nothing on this Host could speak it: it is over without a clip.
        let unspoken = taken.ended(None, Err("no voice"), 1_200).unwrap();
        assert_eq!(
            (unspoken.state, unspoken.clip_id),
            (LineState::Failed, None)
        );
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
        // A clip that was played to its end is not said to be waiting again by an app
        // that is late; playing it once more is said.
        let heard = playing.played("play-ok", None);
        assert!(heard.heard_or_given_up());
        assert_eq!(
            heard.played("queued", None).played.as_deref(),
            Some("play-ok")
        );
        assert_eq!(
            heard.played("play-start", None).played.as_deref(),
            Some("play-start")
        );
    }

    #[test]
    fn the_number_of_a_clip_is_read_as_an_app_sends_it() {
        use serde_json::json;
        let meant = |value: serde_json::Value| clip_meant(Some(&value));
        assert_eq!(clip_meant(None), Ok(Meant::ByAddress));
        assert_eq!(meant(json!(null)), Ok(Meant::ByAddress));
        assert_eq!(meant(json!("")), Ok(Meant::ByAddress));
        assert_eq!(meant(json!(0)), Ok(Meant::ByAddress));
        assert_eq!(meant(json!(false)), Ok(Meant::ByAddress));
        assert_eq!(meant(json!(true)), Ok(Meant::Clip(1)));
        assert_eq!(meant(json!(41)), Ok(Meant::Clip(41)));
        assert_eq!(meant(json!(41.9)), Ok(Meant::Clip(41)));
        assert_eq!(meant(json!("41")), Ok(Meant::Clip(41)));
        assert_eq!(meant(json!(" +4_1\n")), Ok(Meant::Clip(41)));
        assert_eq!(
            meant(json!(1_763_280_000_000_u64)),
            Ok(Meant::Clip(1_763_280_000_000))
        );
        assert_eq!(meant(json!(-3)), Ok(Meant::NoClip));
        assert_eq!(meant(json!("-3")), Ok(Meant::NoClip));
        assert_eq!(meant(json!("9".repeat(60))), Ok(Meant::NoClip));
        for no_number in [
            json!("41.5"),
            json!("x"),
            json!("  "),
            json!("4__1"),
            json!("_41"),
            json!("41_"),
            json!("-"),
            json!([41]),
            json!({"clip": 41}),
        ] {
            assert_eq!(meant(no_number.clone()), Err(NoNumber), "{no_number}");
        }
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
        let begun = taken
            .begun((41, "a.mp3"), ("gemini", "Kore"), None)
            .unwrap();
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
        let said = Said {
            bytes: 10,
            provider: "gemini",
            voice: "Kore",
        };
        book.apply(&begun.ended(Some(41), Ok(said), 1_500).unwrap());
        assert_eq!(book.next().map(|line| line.id.as_str()), Some("s-abc-2"));
        assert_eq!(book.lines().len(), 2);
        assert_eq!(
            book.get("s-abc-1").map(|line| line.state),
            Some(LineState::Spoken)
        );
        assert_eq!(book.of_reply("r-abc").count(), 2);
        assert_eq!(book.of_reply("r-other").count(), 0);
        // What was over long ago is forgotten; what still waits is not, however old.
        book.forget_before(1_500);
        assert_eq!(book.lines().len(), 2, "over at that moment: kept");
        book.forget_before(1_501);
        let left: Vec<&str> = book.lines().iter().map(|line| line.id.as_str()).collect();
        assert_eq!(left, ["s-abc-2"]);
    }

    #[test]
    fn a_line_begun_long_before_a_restart_is_not_begun_again() {
        let mut book = SpeechBook::new();
        let lines = lines_of("r-abc", 7, REPLY, None, 1_000);
        let recent = lines[0]
            .claimed(100_000)
            .unwrap()
            .begun((1, "a.mp3"), ("gemini", "Kore"), Some(3))
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
                again.clip.as_deref(),
                again.event_id
            ),
            (LineState::Queued, None, None, None, None),
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
        // Unless it has waited for a Host that was away too long: it is not spoken then.
        let late = settled.after_a_restart(1_000 + WAITS_AT_MOST_MS + 1);
        assert_eq!(late.len(), 1);
        assert_eq!(
            (late[0].state, late[0].error.as_deref()),
            (LineState::Dropped, Some(WAITED_TOO_LONG))
        );
        assert!(settled.after_a_restart(1_000 + WAITS_AT_MOST_MS).is_empty());
    }

    #[test]
    fn the_clips_an_app_missed_are_offered_again() {
        let said = Said {
            bytes: 10,
            provider: "gemini",
            voice: "Kore",
        };
        // Six clips of one reply, announced a second apart from 100 s on.
        let text = "<speak>one</speak>".repeat(6);
        let clips: Vec<Line> = lines_of("r-a", 1, &text, None, 0)
            .into_iter()
            .zip(1_u64..)
            .map(|(line, number)| {
                let at = 100_000 + i64::try_from(number).unwrap() * 1_000;
                let begun = line.claimed(at).unwrap();
                let begun = begun
                    .begun((number, "a.mp3"), ("gemini", "Kore"), Some(number + 50))
                    .unwrap();
                begun.ended(Some(number), Ok(said), at + 500).unwrap()
            })
            .collect();
        let now = 110_000;
        let offered = |lines: &[Line], now: i64| -> Vec<u64> {
            let lines: Vec<&Line> = lines.iter().collect();
            let places = offered_again(&lines, now);
            places
                .into_iter()
                .map(|place| lines[place].clip_id.unwrap())
                .collect()
        };
        // The newest three, in the order they were announced.
        assert_eq!(offered(&clips, now), [4, 5, 6]);
        // One that was played, and one that could not be, are not offered; older ones are.
        let mut some = clips.clone();
        some[5] = some[5].played("play-ok", None);
        some[4] = some[4].played("play-fail", Some("no session"));
        assert_eq!(offered(&some, now), [2, 3, 4]);
        // One an app holds back is offered however old it is, beside the newest three.
        some[0] = some[0].played("held", None);
        assert_eq!(offered(&some, now), [1, 2, 3, 4]);
        let later = now + OFFERED_AGAIN_WITHIN_MS;
        assert_eq!(offered(&some, later), [1]);
        // One that is still being spoken is offered: its beginning can be heard already.
        let mut speaking = clips.clone();
        speaking.truncate(1);
        let more = lines_of("r-b", 2, "<speak>two</speak>", None, 0).remove(0);
        let more = more.claimed(109_000).unwrap();
        speaking.push(
            more.begun((9, "b.mp3"), ("gemini", "Kore"), Some(90))
                .unwrap(),
        );
        assert_eq!(offered(&speaking, now), [1, 9]);
        // One no engine finished is not, and neither is a line that has no clip.
        let failed = speaking[1].ended(Some(9), Err("cut"), 109_500).unwrap();
        let waiting = lines_of("r-c", 3, "<speak>three</speak>", None, 0).remove(0);
        assert_eq!(offered(&[failed, waiting], now), Vec::<u64>::new());
    }
}
