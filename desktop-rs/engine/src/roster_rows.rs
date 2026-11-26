//! One agent's change without the whole agent list (issue 841). An
//! `agent-roster` event once had the desktop read `/agents/snapshot`, the
//! whole list, for every change of one agent. A Host that lists
//! `roster_fields` (contract 182) puts the fields of the row its change moved
//! in the event (and, for a helper, its maker's): the rows are patched and
//! nothing is read. One that lists `agent_row` (contract 183) answers one row
//! at `GET /agents/{session}`, read for the kinds whose change stays in that
//! row (docs/protocol.md, "One agent's change in its roster event").
//!
//! The snapshot is still read for every other kind, for the reconnect nudge,
//! on a Host without the features, and whenever a patch cannot stand on its
//! own: a row the desktop does not hold, a row put away or taken back, a
//! limited device refused the row, or a snapshot already on its way that may
//! have been made before the change (it is read once more after it lands).
//! Peers are whole engines of their own, so each Host's rows follow its own
//! features; the merged list of all Hosts is rebuilt from theirs.

use std::collections::HashSet;

use clarp_core::json::{self, Object};
use clarp_core::roster::RowChange;
use serde_json::Value;

use crate::{Change, Engine};

pub(crate) const FIELDS_FEATURE: &str = "roster_fields";
pub(crate) const ROW_FEATURE: &str = "agent_row";

/// The kinds whose events carry `fields` on a Host with `roster_fields`.
/// `availability` (feature `agent_availability`) always carries them.
const CARRIED: [&str; 5] = ["helper-state", "mute-changed", "chat_view", "agent-transit", "availability"];
/// The kinds whose change stays in the agent's own row: with `agent_row`,
/// that row is read. `coordination-changed` is one only with a `session`
/// (without, the Host's own value changed), and `agent-transit` only
/// without `fields` (its end changed the backend, the model and what
/// follows from them).
const ONE_ROW: [&str; 3] = ["portrait-selected", "agent-moved", "stale-work"];

const TAG: &str = "agent-row:";

#[derive(Default)]
pub(crate) struct RosterRows {
    /// `roster_fields` was in the last `/server-info`.
    pub fields: bool,
    /// `agent_row` was in it, and this device may read a row.
    pub row: bool,
    /// Rows being read; one read per row at a time.
    in_flight: HashSet<String>,
    /// Rows to read again once their read lands: a change came meanwhile.
    again: HashSet<String>,
}

/// What an `agent-roster` event asks of this desktop.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Step {
    /// Patch `session`'s row with `fields`, and the maker's with its own.
    Patch { session: String, fields: Object, parent: Option<(String, Object)> },
    /// Read `session`'s row.
    Row(String),
    /// Read the whole list.
    Snapshot,
}

/// What the event asks, by its kind and what the Host offers.
pub(crate) fn step(event: &Object, fields: bool, row: bool) -> Step {
    let kind = json::string(event, "kind");
    let session = json::string(event, "session");
    if kind.is_empty() || session.is_empty() {
        return Step::Snapshot;
    }
    if (fields || kind == clarp_core::availability::ROSTER_KIND) && CARRIED.contains(&kind.as_str()) {
        if let Some(changed) = event.get("fields").and_then(Value::as_object) {
            let parent = event.get("parent").and_then(Value::as_object).and_then(|parent| {
                let maker = json::string(parent, "session");
                let changed = parent.get("fields").and_then(Value::as_object)?;
                (!maker.is_empty()).then(|| (maker, changed.clone()))
            });
            return Step::Patch { session, fields: changed.clone(), parent };
        }
    }
    if row && (ONE_ROW.contains(&kind.as_str()) || kind == "coordination-changed" || kind == "agent-transit") {
        return Step::Row(session);
    }
    Step::Snapshot
}

fn has(info: &Object, name: &str) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(name)))
}

impl Engine {
    /// `/server-info` answered: which of the two this Host offers.
    pub(crate) fn roster_rows_server_info(&mut self, info: &Object) {
        self.roster_rows.fields = has(info, FIELDS_FEATURE);
        self.roster_rows.row = has(info, ROW_FEATURE);
    }

    /// A new connection: the features are read again, and no row is on its way.
    pub(crate) fn reset_roster_rows(&mut self) {
        self.roster_rows = RosterRows::default();
    }

    /// An `agent-roster` event.
    pub(crate) fn roster_event(&mut self, event: &Object) {
        let step = step(event, self.roster_rows.fields, self.roster_rows.row);
        if step != Step::Snapshot && self.snapshot_in_flight {
            // The list on its way may have been made before this change.
            self.snapshot_dirty = true;
        }
        match step {
            Step::Snapshot => self.request_snapshot(),
            Step::Row(session) => self.read_row(&session),
            Step::Patch { session, fields, parent } => {
                self.patch_one(&session, &fields);
                if let Some((maker, fields)) = parent {
                    self.patch_one(&maker, &fields);
                }
            }
        }
    }

    /// Patches one row: in the list of agents, else among those put away.
    fn patch_one(&mut self, session: &str, fields: &Object) {
        if self.roster_rows.in_flight.contains(session) {
            // The row on its way may have been made before this change.
            self.roster_rows.again.insert(session.to_owned());
        }
        let mut change = self.mutate_roster(|r| r.patch_row(session, fields));
        if change == RowChange::NotHeld {
            change = self.archived.patch_row(session, fields);
            if !self.archived.take_ops().is_empty() {
                self.changes.push(Change::Archive);
            }
        }
        match change {
            RowChange::Applied => self.row_changed(session, fields.contains_key("transit")),
            RowChange::NotHeld => self.read_row(session),
            RowChange::Moved => self.request_snapshot(),
        }
    }

    /// Reads one row, or the whole list where the Host answers no row.
    fn read_row(&mut self, session: &str) {
        if !self.roster_rows.row {
            self.request_snapshot();
            return;
        }
        if !self.roster_rows.in_flight.insert(session.to_owned()) {
            self.roster_rows.again.insert(session.to_owned());
            return;
        }
        let path = format!("/agents/{}", clarp_core::endpoint::percent_encode_segment(session));
        self.api.get(&format!("{TAG}{session}"), &path, &[]);
    }

    /// A read row landed (`GET /agents/{session}`); true when the tag was one.
    pub(crate) fn roster_rows_json(&mut self, tag: &str, object: &Object) -> bool {
        let Some(session) = tag.strip_prefix(TAG) else { return false };
        let session = session.to_owned();
        if !self.roster_rows.in_flight.remove(&session) {
            // From before a reconnect.
            return true;
        }
        let mut change = self.mutate_roster(|r| r.replace_row(object));
        if change == RowChange::NotHeld {
            change = self.archived.replace_row(object);
            if !self.archived.take_ops().is_empty() {
                self.changes.push(Change::Archive);
            }
        }
        match change {
            RowChange::Applied => self.row_changed(&session, true),
            // An agent this desktop never heard of, or one put away or taken
            // back: the list and its counts changed.
            RowChange::NotHeld | RowChange::Moved => self.request_snapshot(),
        }
        if self.roster_rows.again.remove(&session) {
            self.read_row(&session);
        }
        true
    }

    /// A row read failed; true when the tag was one. A session the Host
    /// lost (404), a device refused rows (403) or any other failure: the
    /// whole list tells.
    pub(crate) fn roster_rows_failure(&mut self, tag: &str, status: u16) -> bool {
        let Some(session) = tag.strip_prefix(TAG) else { return false };
        if !self.roster_rows.in_flight.remove(session) {
            return true;
        }
        self.roster_rows.again.remove(session);
        if status == 403 {
            // A limited device reads the snapshot (docs/protocol.md).
            self.roster_rows.row = false;
        }
        self.request_snapshot();
        true
    }

    /// What follows a row's change that a whole list would also do for it:
    /// its chat catches up with the Host's head, coordination rows are read
    /// again where its setting changed, and a move of backend is shown.
    fn row_changed(&mut self, session: &str, transit: bool) {
        let behind = match (self.roster.find(session), self.conversations.get(session)) {
            (Some(agent), Some(conversation)) => {
                let other = !conversation.conversation_id().is_empty()
                    && !agent.conversation_id.is_empty()
                    && conversation.conversation_id() != agent.conversation_id;
                Some(other).filter(|other| *other || conversation.latest_revision() < agent.head_revision)
            }
            _ => None,
        };
        match behind {
            Some(true) => self.request_tail(session, false),
            Some(false) => self.request_delta(session),
            None => {}
        }
        self.coordination_reloads();
        if transit {
            self.transit_snapshot();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn event(value: Value) -> Object {
        value.as_object().cloned().expect("an object")
    }

    #[test]
    fn a_carried_kind_with_fields_is_patched_with_its_makers_fields() {
        let helper = event(json!({"type": "agent-roster", "kind": "helper-state", "session": "h", "agent_id": "h1",
            "fields": {"helper_state": "reported", "archived_at": null},
            "parent": {"session": "p", "agent_id": "p1", "fields": {"running_children": 0, "status_text": null}}}));
        let Step::Patch { session, fields, parent } = step(&helper, true, true) else { panic!("not a patch") };
        assert_eq!(session, "h");
        assert_eq!(fields.get("helper_state"), Some(&json!("reported")));
        let (maker, changed) = parent.expect("the maker's fields");
        assert_eq!(maker, "p");
        assert_eq!(changed.get("running_children"), Some(&json!(0)));
        let muted = event(json!({"kind": "mute-changed", "session": "a", "fields": {"muted": true}}));
        assert!(matches!(step(&muted, true, false), Step::Patch { parent: None, .. }));
    }

    #[test]
    fn without_the_feature_or_its_fields_the_list_is_read() {
        let muted = event(json!({"kind": "mute-changed", "session": "a", "fields": {"muted": true}}));
        assert_eq!(step(&muted, false, false), Step::Snapshot, "an older Host");
        let bare = event(json!({"kind": "mute-changed", "session": "a"}));
        assert_eq!(step(&bare, true, true), Step::Snapshot, "an agent the Host no longer has");
    }

    #[test]
    fn a_change_that_stays_in_one_row_reads_that_row() {
        for kind in ["portrait-selected", "agent-moved", "stale-work", "coordination-changed"] {
            let e = event(json!({"kind": kind, "session": "a"}));
            assert_eq!(step(&e, true, true), Step::Row("a".into()), "{kind}");
            assert_eq!(step(&e, true, false), Step::Snapshot, "{kind} without agent_row");
        }
        let ended = event(json!({"kind": "agent-transit", "session": "a"}));
        assert_eq!(step(&ended, true, true), Step::Row("a".into()), "a move of backend that ended");
        let begun = event(json!({"kind": "agent-transit", "session": "a", "fields": {"transit": {"phase": "starting"}}}));
        assert!(matches!(step(&begun, true, true), Step::Patch { .. }), "one that begins carries its row's transit");
    }

    #[test]
    fn the_nudge_and_kinds_that_reach_other_rows_read_the_list() {
        assert_eq!(step(&event(json!({"type": "agent-roster"})), true, true), Step::Snapshot, "the reconnect nudge");
        for kind in ["created", "forked", "relaunched", "deleted", "archive-changed", "agent-renamed", "contact-assigned",
                     "voice-changed", "voice-profile", "backend-quota", "persona-updated", "janitor-changed", "updated"] {
            assert_eq!(step(&event(json!({"kind": kind, "session": "a"})), true, true), Step::Snapshot, "{kind}");
        }
        let host_wide = event(json!({"kind": "coordination-changed", "coordination_visibility": "show"}));
        assert_eq!(step(&host_wide, true, true), Step::Snapshot, "the Host's own coordination value");
    }
}
