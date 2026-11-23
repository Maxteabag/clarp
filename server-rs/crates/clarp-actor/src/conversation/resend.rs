//! Rows the Host now shows otherwise than when the apps first read them, sent again.
//!
//! An app keeps a row it holds until the row is sent again, and a row is sent again when
//! something is written that names it. A goal's check-in was shown as the words of the user
//! until the Host showed it as its own (contract 163), and an app that read one before still
//! holds those words. So an agent's check-ins are sent again once, where they stand: an item
//! that names one makes the row as new as that item, and a reader that asks what is newer is
//! given the row again, as it is shown now. A row that no app holds yet comes to it in its
//! place, as every row does.

use std::collections::HashSet;

use clarp_kernel::goals::WAKE_PREFIX;
use clarp_kernel::journal::kind;
use clarp_kernel::turn::names_a_goal_wake;
use clarp_ports::log::{AgentLog, NewItem};

use crate::ActorError;

/// Where an agent's log says that its goals' check-ins were sent again.
const GOAL_WAKES_RESENT: &str = "conversation/goal-wakes-resent";

/// The key under which a row's sending again is found.
pub(super) fn key(id: &str) -> String {
    format!("resent-row-{id}")
}

/// The items that send an agent's goals' check-ins again, in the order the rows stand, and the
/// mark that this was done. Nothing when it was done before, or when the agent has none.
pub(crate) fn goal_wakes(log: &dyn AgentLog, at: i64) -> Result<Vec<NewItem>, ActorError> {
    if log.find(GOAL_WAKES_RESENT)?.is_some() {
        return Ok(Vec::new());
    }
    let mut seen = HashSet::new();
    let mut items: Vec<NewItem> = log
        .with_key_prefix(&format!("u-{WAKE_PREFIX}"), kind::ROWS)?
        .into_iter()
        .filter_map(|row| row.key)
        .filter(|id| names_a_goal_wake(id) && seen.insert(id.clone()))
        .map(|id| NewItem::new(kind::RESENT_ROW, at, id.as_bytes().to_vec()).keyed(key(&id)))
        .collect();
    if !items.is_empty() {
        items.push(NewItem::new(kind::RESENT_ROW, at, Vec::new()).keyed(GOAL_WAKES_RESENT));
    }
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

    fn span(log: &dyn AgentLog) -> Span<'static> {
        Span {
            floor: 0,
            head_revision: log.last_seq().unwrap() * REVISION_STEP,
            imported_anchors: None,
        }
    }

    /// The check-ins an app read before are given to it again, after everything it holds and
    /// in the order they stand; nothing else is, and the pass is made once.
    #[test]
    fn a_goals_check_ins_are_sent_again_once_where_they_stand() {
        let automation = || Origin::Trigger {
            name: "automation".into(),
        };
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
        let held = span(log.as_ref()).head_revision;

        let writes = goal_wakes(log.as_ref(), 20).unwrap();
        assert_eq!(writes.len(), 3, "two check-ins and the mark");
        log.append(&writes).unwrap();
        let chat = Which::chat(false);
        let page = after(log.as_ref(), held, &[], span(log.as_ref()), 100, chat).unwrap();
        let sent: Vec<_> = page.entries.iter().map(|row| row.entry.id()).collect();
        assert_eq!(sent, ["u-task-goal-older", "u-task-goal-newer"]);
        assert!(page.entries.iter().all(|row| row.revision > held));
        assert_eq!(page.latest_revision, span(log.as_ref()).head_revision);

        // Read where they stand, they carry the revision they were sent again with.
        let newest = tail(log.as_ref(), &[], span(log.as_ref()), 100, chat).unwrap();
        let stand: Vec<_> = newest.entries.iter().map(|row| row.entry.id()).collect();
        assert_eq!(
            stand,
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

        assert!(goal_wakes(log.as_ref(), 30).unwrap().is_empty(), "once");
    }

    /// An agent with no check-in writes nothing, not even the mark.
    #[test]
    fn an_agent_without_check_ins_writes_nothing() {
        let mut log = log();
        log.append(&[message("u-plain", Origin::User)]).unwrap();
        assert!(goal_wakes(log.as_ref(), 20).unwrap().is_empty());
    }
}
