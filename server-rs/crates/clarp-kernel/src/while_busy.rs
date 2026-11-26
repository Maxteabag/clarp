//! What the owner's Send does while the agent works (feature `send_while_busy`, issue 920): one
//! setting of the Host's, the same in every app, and what the Host did with a send that came
//! with it. Only the user's own sends act on it; another agent's or an automation's send waits
//! as it always has.

/// The key the owner's setting is kept under, among the Host's settings.
pub const KEY: &str = "send.while_busy";

/// What a send of the owner's does while a turn runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum WhileBusy {
    /// Read into the running turn where its tool takes words; where it cannot take words during
    /// a turn at all, the turn is stopped and the send runs next.
    #[default]
    Steer,
    /// Waits for the running turn and runs next, also where it could be read in.
    AfterTurn,
    /// Stops the running turn now and runs next.
    TakeOver,
}

impl WhileBusy {
    /// The value as the apps name it, or `None` for a word that is no such value.
    pub fn named(word: &str) -> Option<Self> {
        match word {
            "steer" => Some(Self::Steer),
            "after_turn" => Some(Self::AfterTurn),
            "take_over" => Some(Self::TakeOver),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Steer => "steer",
            Self::AfterTurn => "after_turn",
            Self::TakeOver => "take_over",
        }
    }

    /// The owner's setting as it is kept: the default where nothing, or nothing readable, is.
    pub fn stored(kept: Option<&str>) -> Self {
        kept.and_then(Self::named).unwrap_or_default()
    }
}

/// The words a refusal of a value that is none of the three says.
pub const NOT_A_VALUE: &str = "while_busy must be \"steer\", \"after_turn\" or \"take_over\"";

/// Why a send did something other than what it asked for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    /// The running turn's tool cannot take words during a turn at all: no steering for its
    /// backend, a terminal session, a turn on its own server, a turn whose input has ended.
    NotSteerable,
    /// The send is no words to read in: it names a file, or begins with `/`.
    NotWords,
    /// The running turn can neither take it now nor be stopped for it (the tool's compaction,
    /// a native goal's own turn; a take-over is refused `turn_not_cuttable`): the send waits.
    NotCuttable,
}

impl Reason {
    pub fn name(self) -> &'static str {
        match self {
            Self::NotSteerable => "not_steerable",
            Self::NotWords => "not_words",
            Self::NotCuttable => "turn_not_cuttable",
        }
    }
}

/// What the Host did with a send of the owner's that came while a turn ran, and, where that is
/// not what it asked for, what it asked for and why.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Done {
    pub done: WhileBusy,
    pub fallback: Option<(WhileBusy, Reason)>,
}

impl Done {
    pub fn as_asked(done: WhileBusy) -> Self {
        Self {
            done,
            fallback: None,
        }
    }

    pub fn fell_back(asked: WhileBusy, done: WhileBusy, reason: Reason) -> Self {
        Self {
            done,
            fallback: Some((asked, reason)),
        }
    }

    /// The short line the app's chip shows for a fallback.
    pub fn text(self) -> Option<&'static str> {
        let (_, reason) = self.fallback?;
        Some(match (reason, self.done) {
            (Reason::NotSteerable, WhileBusy::TakeOver) => {
                "This agent can't take a message mid-turn: stopped and sent"
            }
            (Reason::NotWords, _) => "Files and / commands wait for the turn to end",
            (Reason::NotCuttable, _) => "This turn can't be stopped: your message waits for it",
            (Reason::NotSteerable, _) => "This agent can't take a message mid-turn: sent after it",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_setting_is_steer_unless_the_owner_kept_another_of_the_three() {
        assert_eq!(WhileBusy::stored(None), WhileBusy::Steer);
        assert_eq!(WhileBusy::stored(Some("junk")), WhileBusy::Steer);
        for mode in [WhileBusy::Steer, WhileBusy::AfterTurn, WhileBusy::TakeOver] {
            assert_eq!(WhileBusy::stored(Some(mode.name())), mode);
        }
    }

    #[test]
    fn only_a_fallback_has_a_line_for_the_chip() {
        assert_eq!(Done::as_asked(WhileBusy::Steer).text(), None);
        let stopped = Done::fell_back(WhileBusy::Steer, WhileBusy::TakeOver, Reason::NotSteerable);
        assert_eq!(
            stopped.text(),
            Some("This agent can't take a message mid-turn: stopped and sent")
        );
        let waits = Done::fell_back(WhileBusy::Steer, WhileBusy::AfterTurn, Reason::NotWords);
        assert_eq!(
            waits.text(),
            Some("Files and / commands wait for the turn to end")
        );
        let held = Done::fell_back(WhileBusy::Steer, WhileBusy::AfterTurn, Reason::NotCuttable);
        assert_eq!(
            held.fallback.map(|(_, why)| why.name()),
            Some("turn_not_cuttable")
        );
        assert_eq!(
            held.text(),
            Some("This turn can't be stopped: your message waits for it")
        );
    }
}
