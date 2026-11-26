//! What an `agent-roster` event about one agent carries of its change (feature
//! `roster_fields`, issue 809).
//!
//! Such an event once said only `{type, kind, session}`, and every app read the whole
//! snapshot again for it: hundreds of rows for a change of one or two fields, most often a
//! helper that began or ended its work. An event of a kind in [`CARRIED`] now carries
//! `fields`: the fields of the agent's snapshot row its change moved, with their values as
//! that row has them at the moment the event is numbered. An event about a helper also
//! carries `parent`: its maker's `session` and `agent_id` and the `fields` of the maker's
//! row that its helpers decide. An app patches its rows with them and reads nothing.
//!
//! The values are made by the code that makes the snapshot's rows (`edge::busy_with`,
//! `wire::own_state`, `chat_view::preference`, `agent_transit::running_of`), so they cannot
//! differ from them. An event of another kind, or one whose change is more than its fields
//! (the end of a change of backend), carries none: an app reads the one row
//! (`GET /agents/{session}`) or the snapshot, as `docs/protocol.md` says of each kind.
//!
//! Nothing here holds words a reader of the event may not have: the guard of bound privacy
//! judges every frame as it is sent (`bound_privacy::Withheld::walk`). The event and its
//! `parent` each name their agent by `session`, so each is blanked as that agent's row in
//! the snapshot is blanked for that reader.

use serde_json::{Value, json};

use crate::fleet::{Directory, Member};

/// The kinds whose events carry `fields`.
pub const CARRIED: [&str; 4] = ["helper-state", "mute-changed", "chat_view", "agent-transit"];

/// Adds what the event's change moved to an `agent-roster` event of a carried kind about one
/// agent the Host has, from the roster as it stands. Any other event is left as it is.
pub fn add_to(event: &mut Value, directory: &Directory) {
    let kind = event["kind"].as_str().unwrap_or("").to_owned();
    if event["type"] != "agent-roster"
        || !CARRIED.contains(&kind.as_str())
        || event.get("fields").is_some()
    {
        return;
    }
    let Some(member) = event["session"]
        .as_str()
        .and_then(|session| member(directory, |member| member.record.session == session))
    else {
        return;
    };
    let Some(fields) = fields(&kind, directory, member) else {
        return;
    };
    event["agent_id"] = json!(member.record.agent_id);
    event["fields"] = fields;
    if kind == "helper-state"
        && let Some(parent) =
            member.record.parent_agent_id.as_deref().and_then(|parent| {
                self::member(directory, |member| member.record.agent_id == parent)
            })
    {
        event["parent"] = json!({
            "session": parent.record.session,
            "agent_id": parent.record.agent_id,
            "fields": crate::edge::family_fields(directory, parent),
        });
    }
}

/// The fields of the agent's row that a change of this kind moves, as the row has them now;
/// `None` where this change moved more than them.
fn fields(kind: &str, directory: &Directory, member: &Member) -> Option<Value> {
    let record = &member.record;
    match kind {
        "helper-state" => Some(json!({
            "helper_state": crate::fleet::AboveWork::of(directory).helper_state(member),
            "archived_at": record.archived_at_ms,
        })),
        "mute-changed" => Some(json!({ "muted": record.muted })),
        "chat_view" => Some(json!({
            "chat_view": crate::chat_view::preference(directory, &record.agent_id),
        })),
        // A change of backend that begins: its row says so. One that ended changed the
        // agent's backend and model, and what follows from them: the row is read.
        "agent-transit" => {
            let transit = crate::agent_transit::running_of(&record.agent_id);
            (!transit.is_null()).then(|| json!({ "transit": transit }))
        }
        _ => None,
    }
}

/// The helpers above the agent of a `helper-state` or `archive-changed` event whose rows its
/// change may move: a helper is shown `running` while a helper below it works, one that is put
/// away aside (issue 936), so a helper that starts, stops, or is put away or taken back can move
/// each maker above it that is not at work itself. Each gets a `helper-state` event of its own,
/// which carries its row's fields and its own maker's; the walk stops at the first maker that
/// is no helper or works itself, whose row the helper below it no longer moves.
pub fn makers_moved(event: &Value, directory: &Directory) -> Vec<String> {
    let kind = &event["kind"];
    if event["type"] != "agent-roster" || (kind != "helper-state" && kind != "archive-changed") {
        return Vec::new();
    }
    let Some(mut below) = event["session"]
        .as_str()
        .and_then(|session| member(directory, |member| member.record.session == session))
    else {
        return Vec::new();
    };
    let mut makers: Vec<String> = Vec::new();
    while let Some(maker) = below.record.parent_agent_id.as_deref().and_then(|parent| {
        directory
            .members
            .iter()
            .map(|member| &**member)
            .find(|member| member.record.agent_id == parent)
    }) {
        let at_work = maker.shown_helper_state() == Some("running");
        if !maker.record.is_helper() || at_work || makers.contains(&maker.record.session) {
            break;
        }
        makers.push(maker.record.session.clone());
        below = maker;
    }
    makers
}

fn member(directory: &Directory, found: impl Fn(&Member) -> bool) -> Option<&Member> {
    directory
        .members
        .iter()
        .chain(&directory.janitors)
        .map(|member| &**member)
        .find(|&member| found(member))
}
