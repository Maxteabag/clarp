//! Whether each backend's turns on this Host reach the chat: `backend_failures`.
//!
//! An agent's thread hands every turn it ends to the hub ([`crate::hub::Hub::turn_ended`]),
//! judged as it ended ([`judged`]). Each pass takes them into the findings book, which keeps
//! the last two hours of them, so a restart forgets none that a pass has taken. A Host that
//! starts again tells each agent's last turn once more; it is kept once.

use std::collections::{BTreeMap, BTreeSet};

use clarp_kernel::conversation::Entry;
use clarp_kernel::failures::{AUTH, ENDED_FROM_OUTSIDE, FOLDER_MISSING, INTERRUPTED, USAGE_LIMIT};
use clarp_kernel::turn::{COMPACTION, Origin, Outcome};
use jiff::tz::TimeZone;
use serde::{Deserialize, Serialize};

use super::book::Seen;

/// The finding's kind; its subject is the backend.
pub(super) const KIND: &str = "backend_failures";
const HOUR: i64 = 60 * 60 * 1_000;
/// How long the book keeps a turn's end.
const KEPT_MS: i64 = 2 * HOUR;
/// And how many at most, the newest kept.
const KEPT_MOST: usize = 20_000;
const REASON_CHARS: usize = 80;

/// How a turn ended, for whether its backend works.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "how", rename_all = "snake_case")]
pub(crate) enum How {
    /// It said something to the chat: a reply with words, or a note.
    Answered,
    /// It ended done, and nothing it said reached the chat.
    Empty,
    /// It ended done without words on another agent's message: an agent that chose not to
    /// answer (a "no reply needed", a note that only informs), not a backend that failed.
    /// Marked in the finding, never counted against the backend.
    Silent,
    /// It failed, for this reason.
    Failed { reason: String },
    /// It failed on the backend's sign-in.
    SignIn,
}

impl How {
    fn bad(&self) -> Option<String> {
        match self {
            Self::Answered | Self::Silent => None,
            Self::Empty => Some("empty".to_owned()),
            Self::Failed { reason } => Some(format!("failed: {reason}")),
            Self::SignIn => Some("sign-in".to_owned()),
        }
    }
}

/// One turn's end, as the agent's thread told it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Ended {
    pub agent_id: String,
    pub session: String,
    pub backend: String,
    pub turn: u64,
    pub at: i64,
    #[serde(flatten)]
    pub how: How,
}

/// Whether any of these rows is a reply with words, or a note, of this turn: in any audience,
/// so a coordination reply hidden from the user's view counts.
pub(crate) fn said<'a>(rows: impl IntoIterator<Item = &'a Entry>, turn: u64) -> bool {
    rows.into_iter().any(|entry| match entry {
        Entry::Reply(reply) => reply.turn == turn && !reply.text.trim().is_empty(),
        Entry::Note(note) => note.turn == turn,
        _ => false,
    })
}

/// How a settled turn counts: `None` for one that says nothing of its backend (a Stop, a
/// usage limit the Hotseat handles, an agent whose folder is gone, a turn whose process
/// something outside the Host ended, a cut-off, a turn replaced or taken back). `answered` says whether the row that closed it is a reply with words in it
/// or it left a note.
pub(crate) fn judged(outcome: &Outcome, origin: &Origin, answered: bool) -> Option<How> {
    match outcome {
        Outcome::Completed
            if answered || matches!(origin, Origin::Trigger { name } if name == COMPACTION) =>
        {
            Some(How::Answered)
        }
        // An agent's message answered with silence: the user asked nothing, and an agent may
        // be told not to reply. A turn of the user's, or one the Host started, still counts.
        Outcome::Completed if matches!(origin, Origin::Agent { .. }) => Some(How::Silent),
        Outcome::Completed => Some(How::Empty),
        Outcome::Failed { kind, .. }
            if kind == USAGE_LIMIT
                || kind == INTERRUPTED
                || kind == FOLDER_MISSING
                || kind == ENDED_FROM_OUTSIDE =>
        {
            None
        }
        Outcome::Failed { kind, .. } if kind == AUTH => Some(How::SignIn),
        Outcome::Failed { reason, kind } => Some(How::Failed {
            reason: short(reason, kind),
        }),
        Outcome::Stopped { .. }
        | Outcome::Interrupted { .. }
        | Outcome::Replaced { .. }
        | Outcome::Withdrawn { .. } => None,
    }
}

/// A failure's reason as a finding names it: its first line, cut short.
fn short(reason: &str, kind: &str) -> String {
    let line = reason.lines().map(str::trim).find(|line| !line.is_empty());
    let Some(line) = line else {
        return if kind.is_empty() { "unknown" } else { kind }.to_owned();
    };
    if line.chars().count() <= REASON_CHARS {
        return line.to_owned();
    }
    let mut cut: String = line.chars().take(REASON_CHARS - 1).collect();
    cut.push('…');
    cut
}

/// Takes the newly told ends into what the book keeps: each turn once, the last two hours.
pub(super) fn fold(kept: &mut Vec<Ended>, told: Vec<Ended>, now: i64) {
    let mut known: BTreeSet<(String, u64)> = kept
        .iter()
        .map(|ended| (ended.agent_id.clone(), ended.turn))
        .collect();
    for ended in told {
        if known.insert((ended.agent_id.clone(), ended.turn)) {
            kept.push(ended);
        }
    }
    let since = now.saturating_sub(KEPT_MS);
    kept.retain(|ended| ended.at >= since);
    kept.sort_by_key(|ended| ended.at);
    let over = kept.len().saturating_sub(KEPT_MOST);
    kept.drain(..over);
}

/// A backend's finding opens with two or more turns of the last hour that went wrong, or when
/// every turn of the hour (one at least) did. Once open it stays open while the hour holds one.
pub(super) fn findings(
    seen: &mut Seen,
    kept: &[Ended],
    open: &BTreeSet<String>,
    host: &str,
    zone: &TimeZone,
    now: i64,
) {
    #[derive(Default)]
    struct Tally<'a> {
        turns: usize,
        bad: usize,
        silent: usize,
        reasons: BTreeMap<String, usize>,
        newest: Option<&'a Ended>,
    }
    let since = now.saturating_sub(HOUR);
    let mut backends: BTreeMap<&str, Tally> = BTreeMap::new();
    for ended in kept
        .iter()
        .filter(|ended| (since..=now).contains(&ended.at))
    {
        let tally = backends.entry(&ended.backend).or_default();
        tally.turns += 1;
        if ended.how == How::Silent {
            tally.silent += 1;
        }
        if let Some(reason) = ended.how.bad() {
            tally.bad += 1;
            *tally.reasons.entry(reason).or_default() += 1;
            if tally.newest.is_none_or(|newest| ended.at >= newest.at) {
                tally.newest = Some(ended);
            }
        }
    }
    for (backend, tally) in backends {
        let opens = tally.bad >= 2 || tally.bad == tally.turns;
        let Some(newest) = tally.newest.filter(|_| opens || open.contains(backend)) else {
            continue;
        };
        let reasons = tally
            .reasons
            .iter()
            .map(|(reason, count)| {
                if *count == 1 {
                    reason.clone()
                } else {
                    format!("{reason} ({count}x)")
                }
            })
            .collect::<Vec<_>>()
            .join("; ");
        let at = jiff::Timestamp::from_millisecond(newest.at)
            .map(|at| at.to_zoned(zone.clone()).strftime("%H:%M").to_string())
            .unwrap_or_default();
        let place = if host.is_empty() {
            String::new()
        } else {
            format!(" on {host}")
        };
        let silent = match tally.silent {
            0 => String::new(),
            1 => " 1 more ended without words on another agent's message; it is not counted."
                .to_owned(),
            n => format!(
                " {n} more ended without words on another agent's message; they are not counted."
            ),
        };
        seen.add(
            KIND,
            backend,
            format!(
                "{} of {} {backend} turns{place} in the last hour did not reach the chat: {reasons}. The newest at {at}, agent {}.{silent}",
                tally.bad, tally.turns, newest.session,
            ),
        );
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::super::book::{Book, Notice};
    use super::super::settings::Settings;
    use super::*;
    use crate::fleet::Directory;

    const START: i64 = 1_800_000_000_000;
    const MINUTE: i64 = 60_000;

    fn failed(reason: &str, kind: &str) -> Outcome {
        Outcome::Failed {
            reason: reason.into(),
            kind: kind.into(),
        }
    }

    fn ended(turn: u64, at: i64, how: How) -> Ended {
        Ended {
            agent_id: format!("agent-{turn}"),
            session: format!("kai-{turn}"),
            backend: "deepseek".into(),
            turn,
            at,
            how,
        }
    }

    fn crash(reason: &str) -> How {
        How::Failed {
            reason: reason.into(),
        }
    }

    fn seen(kept: &[Ended], open: &[&str], now: i64) -> BTreeMap<(String, String), String> {
        let mut seen = Seen::default();
        let open = open.iter().map(|s| (*s).to_owned()).collect();
        findings(&mut seen, kept, &open, "laptopstudio", &TimeZone::UTC, now);
        seen.found
    }

    #[test]
    fn a_turn_counts_when_it_failed_said_nothing_or_failed_to_sign_in_and_not_for_a_stop_or_a_limit()
     {
        let user = Origin::User;
        assert_eq!(
            judged(&Outcome::Completed, &user, true),
            Some(How::Answered)
        );
        assert_eq!(
            judged(&Outcome::Completed, &user, false),
            Some(How::Empty),
            "done with no reply is a turn that went wrong"
        );
        let compaction = Origin::Trigger {
            name: COMPACTION.into(),
        };
        assert_eq!(
            judged(&Outcome::Completed, &compaction, false),
            Some(How::Answered),
            "a compaction says nothing by design"
        );
        let agent = Origin::Agent {
            agent_id: "front-desk".into(),
        };
        assert_eq!(
            judged(&Outcome::Completed, &agent, false),
            Some(How::Silent),
            "an agent's message answered with silence is a choice, not a backend that failed"
        );
        assert_eq!(
            judged(&Outcome::Completed, &agent, true),
            Some(How::Answered)
        );
        assert_eq!(
            judged(
                &Outcome::Completed,
                &Origin::Trigger {
                    name: "automation".into()
                },
                false
            ),
            Some(How::Empty),
            "a goal taken up again that says nothing still counts"
        );
        assert_eq!(
            judged(
                &failed("process exited 1\nmore", "runner_exit"),
                &user,
                false
            ),
            Some(crash("process exited 1"))
        );
        assert_eq!(
            judged(&failed("", ""), &user, false),
            Some(crash("unknown"))
        );
        assert_eq!(
            judged(&failed("Not logged in", AUTH), &user, false),
            Some(How::SignIn)
        );
        assert_eq!(
            judged(&failed("out of credits", USAGE_LIMIT), &user, false),
            None
        );
        assert_eq!(judged(&failed("stopped", INTERRUPTED), &user, false), None);
        // Ended by a signal from outside the Host: nothing of its backend (issue 829).
        let outside = Outcome::failed(clarp_kernel::failures::ended_from_outside("SIGTERM"));
        assert!(matches!(&outside, Outcome::Failed { kind, .. } if kind == ENDED_FROM_OUTSIDE));
        assert_eq!(judged(&outside, &user, false), None);
        assert_eq!(
            judged(
                &Outcome::stopped(clarp_kernel::control::Control::by("user")),
                &user,
                false
            ),
            None,
            "a user's Stop"
        );
        let long = "x".repeat(200);
        let How::Failed { reason } = judged(&failed(&long, "unknown"), &user, false).unwrap()
        else {
            panic!("failed");
        };
        assert_eq!(reason.chars().count(), REASON_CHARS);
    }

    #[test]
    fn a_hidden_coordination_reply_is_an_answer_and_only_a_turn_with_no_reply_or_note_is_empty() {
        use clarp_kernel::conversation::{Message, Note, Reply};
        let reply = |turn, text: &str, origin| {
            Entry::Reply(Reply {
                id: format!("r-{turn}"),
                turn,
                text: text.into(),
                origin,
                ..Reply::default()
            })
        };
        let from_an_agent = Origin::Agent {
            agent_id: "maximus".into(),
        };
        // A reply to another agent, hidden from the user's view, was still said.
        assert!(said([&reply(7, "done", from_an_agent.clone())], 7));
        assert!(said(
            [
                &reply(6, "earlier turn", Origin::User),
                &Entry::Note(Note {
                    id: "n".into(),
                    turn: 7,
                    text: "note".into(),
                    trace_id: None,
                }),
            ],
            7
        ));
        let message = Entry::Message(Message::default());
        assert!(!said(
            [&message, &reply(6, "earlier turn", Origin::User)],
            7
        ));
        assert!(
            !said([&reply(7, "  \n", from_an_agent)], 7),
            "blank is no reply"
        );
    }

    #[test]
    fn two_failed_turns_open_one_among_good_ones_does_not_and_a_lone_failed_turn_does() {
        let now = START;
        let good = |turn, at| ended(turn, at, How::Answered);
        let two = [
            good(1, now - 50 * MINUTE),
            ended(2, now - 40 * MINUTE, crash("process exited 1")),
            ended(3, now - 10 * MINUTE, How::Empty),
            good(4, now - 5 * MINUTE),
        ];
        assert_eq!(
            seen(&two, &[], now)[&(KIND.into(), "deepseek".into())],
            "2 of 4 deepseek turns on laptopstudio in the last hour did not reach the chat: empty; failed: process exited 1. The newest at 07:50, agent kai-3."
        );
        let quiet = [
            good(1, now - 50 * MINUTE),
            ended(2, now - 45 * MINUTE, How::Silent),
            ended(3, now - 40 * MINUTE, How::Silent),
            ended(4, now - 30 * MINUTE, How::Silent),
            good(5, now - 5 * MINUTE),
        ];
        assert!(
            seen(&quiet, &[], now).is_empty(),
            "silence on agents' messages opens nothing"
        );
        let mixed = [
            ended(1, now - 50 * MINUTE, How::Silent),
            ended(2, now - 40 * MINUTE, crash("process exited 1")),
            ended(3, now - 10 * MINUTE, How::Empty),
            good(4, now - 5 * MINUTE),
        ];
        assert_eq!(
            seen(&mixed, &[], now)[&(KIND.into(), "deepseek".into())],
            "2 of 4 deepseek turns on laptopstudio in the last hour did not reach the chat: empty; failed: process exited 1. The newest at 07:50, agent kai-3. 1 more ended without words on another agent's message; it is not counted."
        );
        let one = [
            good(1, now - 50 * MINUTE),
            ended(2, now - 40 * MINUTE, crash("process exited 1")),
            good(3, now - 5 * MINUTE),
        ];
        assert!(
            seen(&one, &[], now).is_empty(),
            "one of three is no finding"
        );
        // Once open, the one that is left keeps it open.
        assert!(seen(&one, &["deepseek"], now).contains_key(&(KIND.into(), "deepseek".into())));
        let lone = [ended(1, now - 30 * MINUTE, How::SignIn)];
        assert_eq!(
            seen(&lone, &[], now)[&(KIND.into(), "deepseek".into())],
            "1 of 1 deepseek turns on laptopstudio in the last hour did not reach the chat: sign-in. The newest at 07:30, agent kai-1."
        );
        let lone_empty = [ended(1, now - 30 * MINUTE, How::Empty)];
        assert!(seen(&lone_empty, &[], now).contains_key(&(KIND.into(), "deepseek".into())));
        // An hour on, the window is quiet.
        assert!(seen(&lone, &["deepseek"], now + 31 * MINUTE).is_empty());
        assert!(seen(&[good(1, now)], &["deepseek"], now).is_empty());
        let same = [
            ended(1, now - 9 * MINUTE, crash("exit 1")),
            ended(2, now - 8 * MINUTE, crash("exit 1")),
        ];
        assert!(
            seen(&same, &[], now)[&(KIND.into(), "deepseek".into())]
                .contains("failed: exit 1 (2x)")
        );
    }

    #[test]
    fn the_book_keeps_each_turn_once_for_two_hours() {
        let mut kept = Vec::new();
        fold(&mut kept, vec![ended(1, START, How::Empty)], START);
        // A Host that starts again tells the last turn once more.
        fold(
            &mut kept,
            vec![
                ended(1, START, How::Empty),
                ended(2, START + MINUTE, How::Answered),
            ],
            START + MINUTE,
        );
        assert_eq!(kept.len(), 2);
        fold(&mut kept, Vec::new(), START + 2 * 60 * MINUTE + 30_000);
        assert_eq!(kept.iter().map(|e| e.turn).collect::<Vec<_>>(), [2]);
        // It is kept with the book, and an older book without it still reads.
        let mut book = Book::default();
        book.turns.clone_from(&kept);
        let bytes = serde_json::to_vec(&book).unwrap();
        let read: Book = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(read.turns, kept);
        let old: Book =
            serde_json::from_str(r#"{"rows":[],"owed":[],"next":0,"summary_date":""}"#).unwrap();
        assert!(old.turns.is_empty());
    }

    #[test]
    fn told_at_once_again_only_after_an_hour_with_changed_text_and_cleared_after_a_quiet_hour() {
        let settings = Settings::from_section(Some(
            &toml::from_str(
                "front_desk = 'hazel-74f6@elitebook'\n[owners]\nbackend_failures = ['maximus-6c37@elitebook', 'nori-5aac']\n",
            )
            .unwrap(),
        ));
        let directory = Directory::default();
        let zone = TimeZone::UTC;
        let mut book = Book::default();
        let mut pass = |at: i64, told: Vec<Ended>| {
            fold(&mut book.turns, told, at);
            let open = book
                .rows
                .iter()
                .filter(|row| row.kind == KIND && row.cleared_at.is_none())
                .map(|row| row.subject.clone())
                .collect();
            let mut seen = Seen::default();
            findings(&mut seen, &book.turns, &open, "laptopstudio", &zone, at);
            book.owed.clear();
            book.reconcile(seen, &settings, &directory, &zone, at);
            book.owed
                .iter()
                .filter(|owed| !matches!(owed.notice, Notice::Summary(_)))
                .map(|owed| (owed.to.clone(), owed.notice.clone()))
                .collect::<Vec<_>>()
        };
        assert_eq!(
            pass(START, vec![ended(1, START, crash("exit 1"))]).len(),
            3,
            "a lone failed turn opens"
        );
        let opened = pass(START + MINUTE, Vec::new());
        assert!(opened.is_empty(), "told once");
        // A second failure changes the text: not told before the hour is out.
        assert!(
            pass(
                START + 20 * MINUTE,
                vec![ended(2, START + 20 * MINUTE, How::Empty)]
            )
            .is_empty()
        );
        let again = pass(
            START + 61 * MINUTE,
            vec![ended(3, START + 61 * MINUTE, How::Answered)],
        );
        let to: Vec<_> = again.iter().map(|(to, _)| to.as_str()).collect();
        assert_eq!(
            to,
            [
                "maximus-6c37@elitebook",
                "nori-5aac",
                "hazel-74f6@elitebook"
            ]
        );
        assert!(
            again
                .iter()
                .all(|(_, notice)| matches!(notice, Notice::Update(..)))
        );
        // Nothing new: not told again.
        assert!(pass(START + 75 * MINUTE, Vec::new()).is_empty());
        // The hour after the last failure holds none: it clears.
        let cleared = pass(START + 81 * MINUTE, Vec::new());
        assert_eq!(cleared.len(), 3);
        assert!(
            cleared
                .iter()
                .all(|(_, notice)| matches!(notice, Notice::Clear(_)))
        );
    }
}
