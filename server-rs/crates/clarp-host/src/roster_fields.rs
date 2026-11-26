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

use crate::fleet::{AboveWork, Directory, Member};

/// The roster events are told from, with the walk of who works below whom (issue 936): made
/// once, at the first event that needs it, and shared by an event and the events about the
/// makers above it that follow it, so that they say the same and the walk is made once.
pub struct Roster<'a> {
    directory: &'a Directory,
    above: std::cell::OnceCell<AboveWork>,
}

impl<'a> Roster<'a> {
    pub fn new(directory: &'a Directory) -> Self {
        Self {
            directory,
            above: std::cell::OnceCell::new(),
        }
    }

    pub(crate) fn above(&self) -> &AboveWork {
        self.above.get_or_init(|| AboveWork::of(self.directory))
    }
}

/// The kinds whose events carry `fields`.
pub const CARRIED: [&str; 4] = ["helper-state", "mute-changed", "chat_view", "agent-transit"];

/// Adds what the event's change moved to an `agent-roster` event of a carried kind about one
/// agent the Host has, from the roster as it stands. Any other event is left as it is.
pub fn add_to(event: &mut Value, roster: &Roster<'_>) {
    let directory = roster.directory;
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
    let Some(fields) = fields(&kind, roster, member) else {
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
            "fields": crate::edge::family_fields(directory, parent, roster.above()),
        });
    }
}

/// The fields of the agent's row that a change of this kind moves, as the row has them now;
/// `None` where this change moved more than them.
fn fields(kind: &str, roster: &Roster<'_>, member: &Member) -> Option<Value> {
    let (directory, record) = (roster.directory, &member.record);
    match kind {
        "helper-state" => Some(json!({
            "helper_state": roster.above().helper_state(member),
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
/// change may move (issue 936). A helper is shown `running` while a helper below it, at any
/// depth, works and is not put away; so a helper that starts, stops, or is put away or taken
/// back can move every helper above it, up the same line the walk of [`AboveWork`] takes,
/// through makers that are no helpers and helpers put away alike. The walk ends at the first
/// maker whose own work lifts the makers above it ([`AboveWork::seeds`]), that maker included:
/// what it lifts does not change, but its own row may. Each helper passed that does not read
/// `running` by its own state gets a `helper-state` event of its own, carrying its row's fields
/// and its own maker's.
pub fn makers_moved(event: &Value, roster: &Roster<'_>) -> Vec<String> {
    let kind = &event["kind"];
    if event["type"] != "agent-roster" || (kind != "helper-state" && kind != "archive-changed") {
        return Vec::new();
    }
    let directory = roster.directory;
    let Some(mut below) = event["session"]
        .as_str()
        .and_then(|session| member(directory, |member| member.record.session == session))
    else {
        return Vec::new();
    };
    let mut passed: Vec<&str> = vec![&below.record.agent_id];
    let mut makers: Vec<String> = Vec::new();
    while let Some(maker) = below.record.parent_agent_id.as_deref().and_then(|parent| {
        directory
            .members
            .iter()
            .map(|member| &**member)
            .find(|member| member.record.agent_id == parent)
    }) {
        if passed.contains(&maker.record.agent_id.as_str()) {
            break;
        }
        passed.push(&maker.record.agent_id);
        if maker.record.is_helper() && maker.shown_helper_state() != Some("running") {
            makers.push(maker.record.session.clone());
        }
        // A maker at work by itself lifts the makers above it whatever happens below it; its
        // own row, lifted for what worked below it, is told above.
        if AboveWork::seeds(maker) {
            break;
        }
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
