//! Rows the Host now shows otherwise than when the apps first read them, sent again.
//!
//! An app keeps a row it holds until the row is sent again, and a row is sent again when
//! something is written that names it. A goal's check-in was shown as the words of the user
//! until the Host showed it as its own (contract 163), and an app that read one before still
//! holds those words. So an agent's newest check-ins are sent again once, where they stand: an
//! item that names one makes the row as new as that item in a chat, and a chat that asks what
//! is newer is given the row again, as it is shown now. To a reader of everything (the goal
//! keeper, a delegate's start) nothing was said.
//!
//! Only the newest are sent. The phones keep the newest 100 rows of a chat, and an older copy
//! lives only in an open chat's memory, read anew when the chat is opened again. A row the
//! phones do not hold is put at the end of their chat when a delta brings it, so sending the
//! whole history would stack old check-ins under the newest reply and fill what they keep.

use std::collections::HashSet;

use clarp_kernel::goals::WAKE_PREFIX;
use clarp_kernel::journal::kind;
use clarp_kernel::turn::names_a_goal_wake;
use clarp_ports::log::{AgentLog, NewItem, Seq};

use crate::ActorError;

/// Where an agent's log says that its goals' check-ins were sent again.
const GOAL_WAKES_RESENT: &str = "conversation/goal-wakes-resent";

/// How many of an agent's newest row items are looked at. A row is often more than one item
/// (a tool row written again when it ended, the rows a chat leaves out): Neon Koko's newest
/// 100 items were 56 rows (23 Nov 2026), so 400 reach past the 100 rows a phone keeps.
const WINDOW: usize = 400;

/// The key under which a row's sending again is found.
pub(super) fn key(id: &str) -> String {
    format!("resent-row-{id}")
}

/// The items that send an agent's check-ins again: those among its newest rows, above the
/// `floor` the conversation begins after, in the order they stand, and the mark that this was
/// done. Nothing when it was done before, or when the agent has no check-in at all.
pub(crate) fn goal_wakes(
    log: &dyn AgentLog,
    floor: Seq,
    at: i64,
) -> Result<Vec<NewItem>, ActorError> {
    if log.find(GOAL_WAKES_RESENT)?.is_some() {
        return Ok(Vec::new());
    }
    // Most agents have none: the key index says so at a look.
    let prefix = format!("u-{WAKE_PREFIX}");
    if log.with_key_prefix(&prefix, kind::ROWS)?.is_empty() {
        return Ok(Vec::new());
    }
    let mut seen = HashSet::new();
    let mut items: Vec<NewItem> = log
        .read_before(Seq::MAX, kind::ROWS, WINDOW)?
        .into_iter()
        // At or below the floor is a conversation that ended: the agent was started again.
        .filter(|row| row.seq > floor)
        .filter_map(|row| row.key)
        .filter(|id| id.starts_with(&prefix) && names_a_goal_wake(id) && seen.insert(id.clone()))
        .map(|id| NewItem::new(kind::RESENT_ROW, at, id.as_bytes().to_vec()).keyed(key(&id)))
        .collect();
    items.push(NewItem::new(kind::RESENT_ROW, at, Vec::new()).keyed(GOAL_WAKES_RESENT));
    Ok(items)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::path::Path;

    use clarp_kernel::conversation::{Entry, Imported, Message, Reply};
    use clarp_kernel::turn::Origin;
    use clarp_ports::log::LogEngine;
    use serde_json::json;

    use super::*;
    use crate::conversation::{REVISION_STEP, Span, Which, after, tail};

    fn log() -> Box<dyn AgentLog> {
        clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/resend"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap()
    }

    fn automation() -> Origin {
        Origin::Trigger {
            name: "automation".into(),
        }
    }

    fn message(id: &str, origin: Origin) -> NewItem {
        Entry::Message(Message {
            id: id.into(),
            text: "Continue your durable outcome commitment.".into(),
            origin,
            ..Message::default()
        })
        .to_item(10)
        .unwrap()
    }

    fn span(log: &dyn AgentLog, floor: Seq) -> Span<'static> {
        Span {
            floor,
            head_revision: log.last_seq().unwrap() * REVISION_STEP,
            imported_anchors: None,
        }
    }

    fn ids(page: &crate::conversation::Page) -> Vec<&str> {
        page.entries.iter().map(|row| row.entry.id()).collect()
    }

    /// The check-ins an app read before are given to a chat again, after everything it holds
    /// and in the order they stand; nothing else is, and the pass is made once.
    #[test]
    fn a_goals_check_ins_are_sent_again_once_where_they_stand() {
        let mut log = log();
        let imported = Entry::Imported(Imported {
            id: "u-task-goal-older".into(),
            row: json!({"id": "u-task-goal-older", "role": "user", "origin": "automation",
                "text": "Continue your durable outcome commitment."}),
        })
        .to_item(5)
        .unwrap();
        let reply = Entry::Reply(Reply {
            id: "r-task-goal-newer".into(),
            text: "Checked.".into(),
            ..Reply::default()
        })
        .to_item(12)
        .unwrap();
        log.append(&[
            imported,
            message("u-task-goal-newer", automation()),
            reply,
            message("u-plain", Origin::User),
        ])
        .unwrap();
        let held = span(log.as_ref(), 0).head_revision;

        let writes = goal_wakes(log.as_ref(), 0, 20).unwrap();
        assert_eq!(writes.len(), 3, "two check-ins and the mark");
        log.append(&writes).unwrap();
        let chat = Which::chat(false);
        let page = after(log.as_ref(), held, &[], span(log.as_ref(), 0), 100, chat).unwrap();
        assert_eq!(ids(&page), ["u-task-goal-older", "u-task-goal-newer"]);
        assert!(page.entries.iter().all(|row| row.revision > held));
        assert_eq!(page.latest_revision, span(log.as_ref(), 0).head_revision);

        // A chat read a page at a time gets each once.
        let mut cursor = held;
        let mut paged = Vec::new();
        loop {
            let page = after(log.as_ref(), cursor, &[], span(log.as_ref(), 0), 1, chat).unwrap();
            paged.extend(ids(&page).into_iter().map(str::to_owned));
            cursor = page.latest_revision;
            if !page.has_more {
                break;
            }
        }
        assert_eq!(paged, ["u-task-goal-older", "u-task-goal-newer"]);

        // A cursor before a check-in gets it once, as new as its sending again.
        let page = after(
            log.as_ref(),
            REVISION_STEP,
            &[],
            span(log.as_ref(), 0),
            100,
            chat,
        )
        .unwrap();
        assert_eq!(
            ids(&page),
            [
                "r-task-goal-newer",
                "u-plain",
                "u-task-goal-older",
                "u-task-goal-newer"
            ]
        );
        assert_eq!(page.entries[3].revision, 6 * REVISION_STEP);

        // A page at a time from there: a check-in read on an earlier page than its sending
        // again waits for the page that reaches it, and comes once (issue 732).
        let mut cursor = REVISION_STEP;
        let mut paged = Vec::new();
        loop {
            let page = after(log.as_ref(), cursor, &[], span(log.as_ref(), 0), 1, chat).unwrap();
            paged.extend(ids(&page).into_iter().map(str::to_owned));
            cursor = page.latest_revision;
            if !page.has_more {
                break;
            }
        }
        assert_eq!(
            paged,
            [
                "r-task-goal-newer",
                "u-plain",
                "u-task-goal-older",
                "u-task-goal-newer"
            ]
        );

        // Read where they stand, they carry in a chat the revision they were sent again with.
        let newest = tail(log.as_ref(), &[], span(log.as_ref(), 0), 100, chat).unwrap();
        assert_eq!(
            ids(&newest),
            [
                "u-task-goal-older",
                "u-task-goal-newer",
                "r-task-goal-newer",
                "u-plain"
            ]
        );
        assert_eq!(newest.entries[0].revision, 5 * REVISION_STEP);
        assert_eq!(newest.entries[1].revision, 6 * REVISION_STEP);
        assert_eq!(newest.entries[3].revision, 4 * REVISION_STEP);

        assert!(goal_wakes(log.as_ref(), 0, 30).unwrap().is_empty(), "once");
    }

    /// To a reader of everything (the goal keeper, a delegate's start) a check-in sent again
    /// is not news, and it keeps the revision it was written with.
    #[test]
    fn a_reader_of_everything_is_not_told_a_check_in_again() {
        let mut log = log();
        log.append(&[message("u-task-goal-a", automation())])
            .unwrap();
        let held = span(log.as_ref(), 0).head_revision;
        log.append(&goal_wakes(log.as_ref(), 0, 20).unwrap())
            .unwrap();
        let all = after(
            log.as_ref(),
            held,
            &[],
            span(log.as_ref(), 0),
            100,
            Which::All,
        )
        .unwrap();
        assert!(all.entries.is_empty(), "{:?}", ids(&all));
        let rows = tail(log.as_ref(), &[], span(log.as_ref(), 0), 100, Which::All).unwrap();
        assert_eq!(rows.entries[0].revision, REVISION_STEP);
    }

    /// A check-in of a conversation that ended (at or below the floor), or one older than the
    /// newest rows, is not sent again.
    #[test]
    fn only_the_newest_check_ins_of_the_conversation_are_sent_again() {
        let mut long = log();
        long.append(&[
            message("u-task-goal-before-the-start", automation()),
            message("u-task-goal-long-ago", automation()),
        ])
        .unwrap();
        let floor = 1;
        let between: Vec<NewItem> = (0..WINDOW)
            .map(|n| message(&format!("u-{n}"), Origin::User))
            .collect();
        long.append(&between).unwrap();
        long.append(&[message("u-task-goal-lately", automation())])
            .unwrap();
        let writes = goal_wakes(long.as_ref(), floor, 20).unwrap();
        let named: Vec<_> = writes.iter().map(|item| item.body.clone()).collect();
        assert_eq!(named, [b"u-task-goal-lately".to_vec(), Vec::new()]);

        // Without the newer rows, the one below the floor is still left out.
        let mut short = log();
        short
            .append(&[
                message("u-task-goal-before-the-start", automation()),
                message("u-task-goal-since", automation()),
            ])
            .unwrap();
        let writes = goal_wakes(short.as_ref(), floor, 20).unwrap();
        let named: Vec<_> = writes.iter().map(|item| item.body.clone()).collect();
        assert_eq!(named, [b"u-task-goal-since".to_vec(), Vec::new()]);
    }

    /// An agent with no check-in writes nothing, not even the mark.
    #[test]
    fn an_agent_without_check_ins_writes_nothing() {
        let mut log = log();
        log.append(&[message("u-plain", Origin::User)]).unwrap();
        assert!(goal_wakes(log.as_ref(), 0, 20).unwrap().is_empty());
    }
}
