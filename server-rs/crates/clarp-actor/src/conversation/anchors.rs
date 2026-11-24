//! The anchors of a turn: the rows the conversation view shows in place of its work.

use std::collections::{HashMap, HashSet};

use clarp_kernel::conversation::{Commentary, Entry};
use clarp_kernel::journal::kind;
use clarp_ports::log::{AgentLog, Item, Seq};

use super::{CHUNK, corrupt, is_conversation, joined_read};
use crate::ActorError;

/// A row the conversation view shows in place of the work of a turn: the answer to one message
/// of the user's.
pub(super) struct Anchor {
    pub(super) id: String,
    /// How many of the turn's rows for that message the conversation view leaves out.
    pub(super) session_rows: usize,
}

/// The anchors of one turn. Each message the turn answers has one: the turn's own, and each
/// message read into it while it ran. The newest final reply to a message is its anchor from
/// the moment it is written, so a client never sees it first as work and then as the answer.
#[derive(Default)]
pub(super) struct TurnAnchors {
    /// Oldest first.
    anchors: Vec<Anchor>,
    /// Which of them the turn's end settled: it carries the turn's last count, and is written
    /// again with the end's revision.
    settled: Option<usize>,
    /// A reply that was the answer to its message until a later reply to the same message was
    /// written, and where that later reply is: it left the conversation view there.
    pub(super) superseded: HashMap<String, Seq>,
    /// The other way round: the reply a later one took the place of.
    pub(super) supersedes: HashMap<String, String>,
}

impl TurnAnchors {
    pub(super) fn of(&self, id: &str) -> Option<&Anchor> {
        self.anchors.iter().find(|anchor| anchor.id == id)
    }

    pub(super) fn settled(&self) -> Option<&Anchor> {
        self.settled.and_then(|at| self.anchors.get(at))
    }
}

/// A reply of a turn with words in it.
struct Said {
    id: String,
    seq: Seq,
    /// Its own message called tools: work follows it.
    calls: bool,
    /// It came right after the reply with words before it, with no work in between: it takes
    /// that one's place as the answer.
    follows: bool,
}

/// What a turn did for one message it answers: from that message to the next one read into
/// the turn, or to the turn's end.
#[derive(Default)]
struct Segment {
    /// Oldest first.
    said: Vec<Said>,
    /// The rows the conversation view leaves out.
    rows: usize,
    /// Its newest row that is conversation by itself: the turn's message or a note.
    fallback: Option<String>,
    /// Work came after the newest reply with words.
    worked: bool,
}

/// Whether this row of another turn is a message read into a running turn of the agent's own
/// session: a message the agent's conversation fork read is the fork's turn, not a part of it.
fn read_into(log: &dyn AgentLog, item: &Item) -> Result<bool, ActorError> {
    if item.kind != kind::MESSAGE || !joined_read(log, item)? {
        return Ok(false);
    }
    let Some(joined) = item.turn else {
        return Ok(false);
    };
    Ok(log
        .find(&crate::fork::turn_key(joined))?
        .is_none_or(|record| record.kind != kind::FORK))
}

/// A turn's anchors and counts come from its own rows, even if the page omits all its work.
/// `end`: where the turn's end is written, once it has ended.
///
/// Each message the turn answers has its own anchor: the newest reply to it with words, when no
/// work came after it (a final reply, from the moment it is written). A message read into the
/// running turn closes what was said before it. The rows of a message with no such answer are
/// counted on the next anchor. When the turn has ended, what came after its last anchor is
/// settled as before there were anchors per message: its newest reply with words, else its
/// newest note or its message, carries the count.
pub(super) fn find_anchors(
    log: &dyn AgentLog,
    turn: u64,
    end: Option<Seq>,
) -> Result<TurnAnchors, ActorError> {
    // Back to the turn's own message: its rows, and where a message was read into it (`None`).
    let mut found: Vec<(Seq, Option<Entry>)> = Vec::new();
    let mut before = end.unwrap_or(Seq::MAX);
    'reading: loop {
        let items = log.read_before(before, kind::ROWS, CHUNK)?;
        let Some(oldest) = items.first() else { break };
        before = oldest.seq;
        for item in items.into_iter().rev() {
            if item.turn != Some(turn) {
                if read_into(log, &item)? {
                    found.push((item.seq, None));
                }
                continue;
            }
            let Some(entry) = Entry::from_item(&item).map_err(|why| corrupt(&item, why))? else {
                continue;
            };
            let prompt = matches!(entry, Entry::Message(_));
            found.push((item.seq, Some(entry)));
            if prompt {
                break 'reading;
            }
        }
    }
    found.reverse();

    let mut segments = vec![Segment::default()];
    let mut seen = HashSet::new();
    for (seq, entry) in found {
        let Some(entry) = entry else {
            segments.push(Segment::default());
            continue;
        };
        let id = entry.id();
        if clarp_kernel::artifacts::forms::is_answer_row(id) || !seen.insert(id.to_owned()) {
            continue;
        }
        let Some(segment) = segments.last_mut() else {
            continue;
        };
        if is_conversation(&entry, None) {
            if matches!(entry, Entry::Message(_) | Entry::Note(_)) {
                segment.fallback = Some(id.to_owned());
            }
            continue;
        }
        segment.rows += 1;
        match &entry {
            Entry::Reply(said) if !said.text.trim().is_empty() => {
                let follows =
                    !segment.worked && segment.said.last().is_some_and(|before| !before.calls);
                segment.said.push(Said {
                    id: said.id.clone(),
                    seq,
                    calls: !said.tools.is_empty(),
                    follows,
                });
                segment.worked = !said.tools.is_empty();
            }
            Entry::Reply(said) if !said.tools.is_empty() => segment.worked = true,
            Entry::Tool(_) => segment.worked = true,
            _ => {}
        }
    }

    let mut anchors = TurnAnchors::default();
    // The rows of the messages since the last anchor: the next anchor counts them.
    let mut pending = 0;
    let mut since = 0;
    let last = segments.len() - 1;
    for (at, segment) in segments.iter().enumerate() {
        for pair in segment.said.windows(2) {
            if pair[1].follows && !pair[1].calls {
                anchors.superseded.insert(pair[0].id.clone(), pair[1].seq);
                anchors
                    .supersedes
                    .insert(pair[1].id.clone(), pair[0].id.clone());
            }
        }
        pending += segment.rows;
        if end.is_some() && at == last {
            break;
        }
        // Words with no work after them are the answer, also while the turn still runs.
        if let Some(newest) = segment.said.last()
            && !newest.calls
            && log.find(&Commentary::id_of(&newest.id))?.is_none()
        {
            anchors.anchors.push(Anchor {
                id: newest.id.clone(),
                session_rows: pending - 1,
            });
            pending = 0;
            since = at + 1;
        }
    }
    if end.is_some() {
        let open = &segments[since..];
        let settled = match open.iter().rev().find_map(|segment| segment.said.last()) {
            Some(newest) => Some(Anchor {
                id: newest.id.clone(),
                session_rows: pending.saturating_sub(1),
            }),
            None => open
                .iter()
                .rev()
                .find_map(|segment| segment.fallback.clone())
                .map(|id| Anchor {
                    id,
                    session_rows: pending,
                }),
        };
        if let Some(anchor) = settled {
            anchors.settled = Some(anchors.anchors.len());
            anchors.anchors.push(anchor);
        }
    }
    Ok(anchors)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::path::Path;

    use clarp_kernel::agent::Command;
    use clarp_kernel::conversation::{Commentary, Entry, Message, Reply, TurnEnd};
    use clarp_kernel::journal::{Incoming, Recorder};
    use clarp_kernel::steering::Join;
    use clarp_kernel::turn::{Admission, Origin, RunOutcome, TurnId};
    use clarp_ports::log::LogEngine;

    use super::super::{REVISION_STEP, Span, Versioned, Which, after, tail};

    /// A row as a chat is given it: its id, its count when it is an anchor, and its revision.
    type Shown = (String, Option<usize>, u64);

    fn shown(rows: &[Versioned]) -> Vec<Shown> {
        rows.iter()
            .map(|row| (row.entry.id().to_owned(), row.session_rows, row.revision))
            .collect()
    }

    fn open() -> Recorder {
        let log = clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/anchors"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        Recorder::open(log).unwrap()
    }

    fn accept(recorder: &mut Recorder, id: &str, now: i64) -> TurnId {
        let message = Message {
            id: id.into(),
            text: id.into(),
            origin: Origin::User,
            ..Message::default()
        };
        recorder
            .accept_message(
                Incoming {
                    key: Some(message.id.clone()),
                    body: serde_json::to_vec(&message).unwrap(),
                    origin: Origin::User,
                    admission: Admission::Queue,
                    coordination: None,
                    user_word: false,
                },
                now,
            )
            .unwrap()
            .turn
            .unwrap()
    }

    /// A turn of the user's that runs: its message is `u-ask`.
    fn start(recorder: &mut Recorder) -> TurnId {
        let turn = accept(recorder, "u-ask", 100);
        recorder.handle(Command::TurnStarted { turn }, 110).unwrap();
        turn
    }

    /// The agent says something whole in the turn; `tools`: the calls its message makes. Rows
    /// it said before and that the calls now make words along the way are named in `along`.
    fn say(recorder: &mut Recorder, turn: TurnId, id: &str, tools: &[&str], along: &[&str]) {
        let mut batch = recorder.batch();
        for earlier in along {
            let part = Entry::Commentary(Commentary::of(earlier, turn.0));
            batch.item(part.to_item(200).unwrap());
        }
        let reply = Entry::Reply(Reply {
            id: id.into(),
            turn: turn.0,
            text: id.into(),
            tools: tools.iter().map(|tool| (*tool).to_owned()).collect(),
            ..Reply::default()
        });
        batch.item(reply.to_item(200).unwrap());
        batch.commit().unwrap();
    }

    /// A message of the user's read into the running turn (steering).
    fn read_in(recorder: &mut Recorder, into: TurnId, id: &str) {
        let followup = accept(recorder, id, 300);
        let mut batch = recorder.batch();
        let join = Join {
            into,
            uuid: id.into(),
            recovery: None,
            together: false,
        };
        batch
            .command(
                Command::Join {
                    turn: followup,
                    join,
                },
                310,
            )
            .unwrap();
        batch
            .command(Command::JoinRead { turn: followup }, 320)
            .unwrap();
        batch.commit().unwrap();
    }

    fn end(recorder: &mut Recorder, turn: TurnId, closes: &str) {
        let mut batch = recorder.batch();
        batch
            .command(
                Command::TurnEnded {
                    turn,
                    outcome: RunOutcome::Completed,
                },
                400,
            )
            .unwrap();
        let end = TurnEnd::of(batch.agent().last().unwrap(), closes).unwrap();
        batch.item(Entry::TurnEnd(end).to_item(400).unwrap());
        batch.commit().unwrap();
    }

    /// What a chat holds now, in the order it shows it.
    fn now(recorder: &Recorder, which: Which) -> Vec<Shown> {
        shown(
            &tail(recorder.log(), &[], Span::default(), 50, which)
                .unwrap()
                .entries,
        )
    }

    /// What a chat that held everything up to the row with this id is given.
    fn since(recorder: &Recorder, id: &str) -> Vec<Shown> {
        let seq = recorder.log().find(id).unwrap().unwrap().seq;
        shown(
            &after(
                recorder.log(),
                seq * REVISION_STEP,
                &[],
                Span::default(),
                50,
                Which::chat(true),
            )
            .unwrap()
            .entries,
        )
    }

    fn counts(rows: &[Shown]) -> Vec<(&str, Option<usize>)> {
        rows.iter()
            .map(|(id, count, _)| (id.as_str(), *count))
            .collect()
    }

    fn conversation(recorder: &Recorder) -> Vec<String> {
        now(recorder, Which::chat(true).conversation_view(false))
            .into_iter()
            .map(|(id, _, _)| id)
            .collect()
    }

    #[test]
    fn a_final_reply_is_the_answer_from_its_first_write_and_the_end_adds_only_its_summary() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-answer", &[], &[]);
        // The turn still runs: the answer is in the conversation already, with its count.
        assert_eq!(
            counts(&now(&recorder, Which::chat(true))),
            [("u-ask", None), ("r-answer", Some(0))]
        );
        assert_eq!(conversation(&recorder), ["u-ask", "r-answer"]);
        assert_eq!(counts(&since(&recorder, "u-ask")), [("r-answer", Some(0))]);
        end(&mut recorder, turn, "r-answer");
        // The end gives it again, as it was, now with how the turn ended.
        let settled = tail(recorder.log(), &[], Span::default(), 50, Which::chat(true)).unwrap();
        let answer = settled
            .entries
            .iter()
            .find(|row| row.entry.id() == "r-answer")
            .unwrap();
        assert_eq!(answer.session_rows, Some(0));
        assert!(answer.turn.is_some());
        assert_eq!(
            counts(&since(&recorder, "r-answer")),
            [("r-answer", Some(0))]
        );
        assert_eq!(conversation(&recorder), ["u-ask", "r-answer"]);
    }

    #[test]
    fn a_later_reply_to_the_same_message_takes_the_answer_s_place() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-one", &[], &[]);
        assert_eq!(conversation(&recorder), ["u-ask", "r-one"]);
        say(&mut recorder, turn, "r-two", &[], &[]);
        assert_eq!(conversation(&recorder), ["u-ask", "r-two"]);
        // A chat that held the first as the answer is given it again, as work now, and as new
        // as the reply that took its place.
        let given = since(&recorder, "r-one");
        assert_eq!(counts(&given), [("r-one", None), ("r-two", Some(1))]);
        assert_eq!(given[0].2, given[1].2);
        end(&mut recorder, turn, "r-two");
        assert_eq!(conversation(&recorder), ["u-ask", "r-two"]);
    }

    #[test]
    fn words_that_work_follows_leave_the_answer_s_place_when_the_work_begins() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-look", &[], &[]);
        assert_eq!(conversation(&recorder), ["u-ask", "r-look"]);
        say(&mut recorder, turn, "r-call", &["t-1"], &["r-look"]);
        assert_eq!(conversation(&recorder), ["u-ask"]);
        assert_eq!(
            counts(&since(&recorder, "r-look")),
            [("r-look", None), ("r-call", None)]
        );
        say(&mut recorder, turn, "r-done", &[], &[]);
        assert_eq!(conversation(&recorder), ["u-ask", "r-done"]);
        assert_eq!(
            counts(&now(&recorder, Which::chat(true))),
            [
                ("u-ask", None),
                ("r-look", None),
                ("r-call", None),
                ("r-done", Some(2))
            ]
        );
    }

    #[test]
    fn each_message_read_into_a_turn_keeps_its_own_answer() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-first", &[], &[]);
        read_in(&mut recorder, turn, "u-more");
        say(&mut recorder, turn, "r-call", &["t-1"], &[]);
        assert_eq!(conversation(&recorder), ["u-ask", "r-first", "u-more"]);
        say(&mut recorder, turn, "r-second", &[], &[]);
        let running = now(&recorder, Which::chat(true));
        assert_eq!(
            counts(&running),
            [
                ("u-ask", None),
                ("r-first", Some(0)),
                ("u-more", None),
                ("r-call", None),
                ("r-second", Some(1))
            ]
        );
        end(&mut recorder, turn, "r-second");
        let settled = now(&recorder, Which::chat(true));
        assert_eq!(counts(&settled), counts(&running));
        assert_eq!(
            conversation(&recorder),
            ["u-ask", "r-first", "u-more", "r-second"]
        );
        // The end changes the answer of the last message only: the first is as it was.
        assert_eq!(settled[1], running[1]);
        assert_eq!(
            counts(&since(&recorder, "r-second")),
            [("r-second", Some(1))]
        );
    }

    #[test]
    fn a_turn_whose_last_words_were_work_settles_as_it_always_did() {
        // Nothing that answered a message by itself: at the end its newest words carry the
        // count of the whole turn, a message read into it or not.
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-look", &["t-1"], &[]);
        read_in(&mut recorder, turn, "u-more");
        say(&mut recorder, turn, "r-call", &["t-2"], &[]);
        assert_eq!(conversation(&recorder), ["u-ask", "u-more"]);
        end(&mut recorder, turn, "r-call");
        assert_eq!(
            counts(&now(&recorder, Which::chat(true))),
            [
                ("u-ask", None),
                ("r-look", None),
                ("u-more", None),
                ("r-call", Some(1))
            ]
        );
        // A turn that said nothing: its message carries the count.
        let mut recorder = open();
        let turn = start(&mut recorder);
        let quiet = Entry::Reply(Reply {
            id: "r-quiet".into(),
            turn: turn.0,
            text: String::new(),
            tools: vec!["t-1".into()],
            ..Reply::default()
        });
        let mut batch = recorder.batch();
        batch.item(quiet.to_item(250).unwrap());
        batch.commit().unwrap();
        end(&mut recorder, turn, "r-quiet");
        assert_eq!(
            counts(&now(&recorder, Which::chat(true))),
            [("u-ask", Some(1)), ("r-quiet", None)]
        );
    }
}
