//! Conversation and Session views of a chat, chosen per agent (feature
//! `chat_view`, Host contract 145, issue 702; docs/protocol.md
//! "Conversation and session views"). The design and words are the iOS
//! app's (`ChatView.swift`, `ChatViewRows.swift`).
//!
//! Conversation: the user's messages, the agent's deliberate posts, each
//! turn's final reply, and cards; one Running box while a turn works, and
//! Show work on a finished turn's reply, which opens the rows the view left
//! out in their places. Session: everything, as before.
//!
//! What a chat draws: its own choice on this device (its menu), else Clean
//! chat (this device's switch: Conversation), else the Host's agent
//! setting, else Session. Pure: the engine keeps the choices and the
//! window splices the rows.

use std::collections::{HashMap, HashSet};

use serde_json::Value;

use crate::json::Object;
use crate::live_present::{Entry, Kind};
use crate::protocol::Message;
use crate::tr;

// i18n-screen: chat

pub const FEATURE: &str = "chat_view";
/// The Host lets an agent answer from a fork while its own turn works.
pub const FORK_FEATURE: &str = "conversation_fork";
pub const CONVERSATION: &str = "conversation";
pub const SESSION: &str = "session";
/// `POST {session, chat_view}`; answers `{ok, session, chat_view}`.
pub const WRITE_PATH: &str = "/agent-chat-view";
/// The `agent-roster` kind carrying `session` and `chat_view`.
pub const ROSTER_KIND: &str = "chat_view";
/// The settings key of each chat's own choice on this device:
/// `{<chat key>: "conversation" | "session"}`.
pub const OWN_KEY: &str = "conversation/chatView";
/// The settings key of Clean chat (every chat in Conversation view).
pub const CLEAN_KEY: &str = "conversation/cleanChat";

pub const TITLE: &str = "Chat view";
pub const CLEAN_TITLE: &str = "Clean chat";
pub const CLEAN_DETAIL: &str = "Messages and replies only.";
pub const SHOW_WORK: &str = "Show work";
pub const HIDE_WORK: &str = "Hide work";
/// The Tool activity menu's place in Conversation view.
pub const TOOL_ACTIVITY_IN_CONVERSATION: &str = "Applies in Session view";
/// The prefix of a finished turn's Show work key: `live:work:<trace>`.
/// Under `live:`, so a click or O toggles it as it does a live row.
pub const WORK_PREFIX: &str = "live:work:";

/// The chat menu's entries: (value, English label; `menu_label` gives the
/// words in the app's language). "default" follows Settings.
pub const MENU: [(&str, &str); 3] = [("default", "Follow Settings"), (CONVERSATION, "Conversation"), (SESSION, "Session")];

/// A value from the Host or the settings file: `conversation`, `session`,
/// or None for anything else (missing, empty, unknown).
pub fn decoded(value: &str) -> Option<&'static str> {
    match value.trim().to_ascii_lowercase().as_str() {
        CONVERSATION => Some(CONVERSATION),
        SESSION => Some(SESSION),
        _ => None,
    }
}

/// What a chat draws: its own choice, else Clean chat, else the Host's
/// value, else Session.
pub fn effective(own: Option<&str>, clean: bool, host: &str) -> &'static str {
    if let Some(own) = own.and_then(decoded) {
        return own;
    }
    if clean {
        return CONVERSATION;
    }
    decoded(host).unwrap_or(SESSION)
}

/// The words for an effective view.
pub fn label(view: &str) -> String {
    match decoded(view) {
        // i18n: chat menu and profile, name of the chat view that shows messages and replies only (noun)
        Some(CONVERSATION) => tr!("chat view" => "Conversation"),
        // i18n: chat menu and profile, name of the chat view that also shows the agent's work (noun)
        _ => tr!("chat view" => "Session"),
    }
}

/// The words for a menu value ("default" reads Follow Settings).
pub fn menu_label(value: &str) -> String {
    match value {
        CONVERSATION | SESSION => label(value),
        // i18n: chat menu choice: this chat uses the choice made in Settings
        _ => tr!("Follow Settings"),
    }
}

/// Clean chat's title and description (Settings), in the app's language.
pub fn clean_title() -> String {
    // i18n: Settings, row title: every chat shows only messages and replies (noun phrase, a setting's name)
    tr!("Clean chat")
}

pub fn clean_detail() -> String {
    // i18n: Settings, description under the Clean chat row
    tr!("Messages and replies only.")
}

/// The quiet line after a write the Host refused.
pub fn refused(host: &str) -> String {
    if host.trim().is_empty() {
        // i18n: chat, quiet note after the Host refused to save the chat's view choice
        tr!("Saved on this device only.")
    } else {
        // i18n: chat, quiet note after the Host refused to save the chat's view choice; {} is the Host's name
        tr!("Not saved on {}. Kept locally.", host.trim())
    }
}

/// A chat's own choice on this device, by its chat key.
pub fn own(settings: &crate::settings::Settings, key: &str) -> Option<&'static str> {
    if key.is_empty() {
        return None;
    }
    settings.get(OWN_KEY)?.get(key)?.as_str().and_then(decoded)
}

/// Sets a chat's own choice, or (None) lets it follow Settings.
pub fn set_own(settings: &mut crate::settings::Settings, key: &str, view: Option<&str>) {
    if key.is_empty() {
        return;
    }
    let mut map = settings.get(OWN_KEY).and_then(Value::as_object).cloned().unwrap_or_default();
    match view.and_then(decoded) {
        Some(view) => {
            map.insert(key.to_owned(), Value::String(view.to_owned()));
        }
        None => {
            map.remove(key);
        }
    }
    if map.is_empty() {
        settings.remove(OWN_KEY);
    } else {
        settings.set(OWN_KEY, Value::Object(map));
    }
}

/// Whether a row belongs to the conversation view. A row the Host did not
/// tag reads as Session (a row it meant to hide is never shown), except a
/// message of one's own not yet read back, which is always conversation:
/// pending, failed, or delivered but not yet sent back by the Host
/// (`sent_here`). A message steered into a running turn is answered 2xx at
/// once but comes back tagged only when the turn reads it; hiding it
/// meanwhile made Peter's own message vanish (23 Nov 2026).
pub fn in_conversation(row: &Message) -> bool {
    if row.view.is_empty() {
        return row.role == "user" && (row.pending || row.delivery_failed || row.sent_here);
    }
    row.view == CONVERSATION
}

/// Whether the held rows carry the Host's tags at all: a chat drawn from
/// a cache written before tags existed is drawn as Session until the
/// Host's read brings them, so it never opens as an empty chat.
pub fn tagged(rows: &[Message]) -> bool {
    rows.iter().any(|r| !r.view.is_empty())
}

pub fn work_key(trace: &str) -> String {
    format!("{WORK_PREFIX}{trace}")
}

/// A finished turn's Show work, on its anchor row.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Work {
    /// `live:work:<trace>`: what the reader toggles.
    pub key: String,
    /// How many rows the view leaves out (the anchor's `session_rows`).
    pub rows: i64,
    pub open: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Projection {
    /// Rows the conversation view leaves out now.
    pub hidden_rows: HashSet<String>,
    /// Turns whose work the reader opened (their session rows show).
    pub open_traces: HashSet<String>,
    /// Anchor row id → its Show work.
    pub anchors: HashMap<String, Work>,
}

/// The conversation view of `rows`: every session row leaves, except a
/// row of a turn the reader opened with Show work (`expanded` holds its
/// key), which shows in its place as the session view draws it.
pub fn project(rows: &[Message], expanded: &HashSet<String>) -> Projection {
    let mut projection = Projection::default();
    for row in rows {
        if let Some(count) = row.session_rows.filter(|n| *n > 0)
            && !row.trace_id.is_empty()
            && in_conversation(row)
        {
            let key = work_key(&row.trace_id);
            let open = expanded.contains(&key);
            if open {
                projection.open_traces.insert(row.trace_id.clone());
            }
            projection.anchors.insert(row.id.clone(), Work { key, rows: count, open });
        }
    }
    for row in rows {
        if in_conversation(row) || (!row.trace_id.is_empty() && projection.open_traces.contains(&row.trace_id)) {
            continue;
        }
        projection.hidden_rows.insert(row.id.clone());
    }
    projection
}

/// The turn a history fold or a live fold stands for, from its key
/// (`live:fold:<turn>`, `live:handoff:<turn>`, or a part of its work that
/// ran before a message it read, `live:fold:<turn>@<row>`).
pub fn fold_turn(key: &str) -> &str {
    let turn = key.strip_prefix("live:fold:").or_else(|| key.strip_prefix("live:handoff:")).unwrap_or_default();
    turn.split(crate::live_present::PART).next().unwrap_or_default()
}

/// Whether an entry of a finished turn stays in the conversation view:
/// its reply (a message not of commentary), or a durable row that is.
pub fn finished_entry_stays(entry: &Entry, conversation_row: impl Fn(&str) -> bool) -> bool {
    if !entry.row.is_empty() {
        return conversation_row(&entry.row);
    }
    entry.kind == Kind::Message && entry.phase != "commentary"
}

/// The Running box's words, as the live status says: a tool's
/// "Running swift build" as "Running swift build…", another step as
/// "Thinking…", nothing known as "Running…".
pub fn running(status: &str) -> String {
    let mut status = status.trim().to_owned();
    while status.ends_with('…') || status.ends_with('.') {
        status.pop();
    }
    let status = status.trim_end();
    if status.is_empty() { "Running…".to_owned() } else { format!("{status}…") }
}

/// What the live activity (`GET /live`'s `activity`) says runs now, in
/// plain words: the Host's headline, else the tool's label, else the step.
pub fn status_words(activity: &Object) -> String {
    let text = |o: &Object, key: &str| o.get(key).and_then(Value::as_str).unwrap_or_default().trim().to_owned();
    let headline = text(activity, "headline");
    let state = text(activity, "state");
    let tool = activity.get("tool").and_then(Value::as_object);
    match state.as_str() {
        "tool" => {
            if !headline.is_empty() {
                return headline;
            }
            let label = tool.map(|t| text(t, "label")).unwrap_or_default();
            if !label.is_empty() {
                // i18n: chat list and Running box, what the agent runs now; {} is the command or tool's label
                return tr!("Running {}", label.lines().next().unwrap_or_default());
            }
            tool_words(&tool.map(|t| text(t, "name")).unwrap_or_default())
        }
        // A turn held for the machine looks like any working turn (the
        // owner, 25 Nov 2026): no words of its own, whatever the Host sends.
        // i18n: chat list and Running box, the agent is working (verb, ongoing)
        "limited" if held_for_the_machine(activity) => tr!("Working"),
        "waiting" | "limited" | "background" if !headline.is_empty() => headline,
        "limited" => limited_words(activity),
        "thinking" if !headline.is_empty() => headline,
        _ => step_words(&state, "").unwrap_or_default(),
    }
}

/// A `limited` turn the Host holds for the machine's load (`waits_for`
/// `machine`): drawn exactly as a working turn, with no banner, words, icon
/// or figure of its own (the owner, 25 Nov 2026). Network and quota holds
/// keep their words.
pub fn held_for_the_machine(activity: &Object) -> bool {
    activity.get("state").and_then(Value::as_str) == Some("limited") && activity.get("waits_for").and_then(Value::as_str) == Some("machine")
}

/// What a `limited` turn waits for, in plain words, when the Host sends no
/// headline: `waits_for` (Rust Host contract 188, feature `waits_for`) tells
/// the machine's pressure and the network from a usage limit; without it
/// (an older Host) `limited` is a usage limit, as before.
pub fn limited_words(activity: &Object) -> String {
    match activity.get("waits_for").and_then(Value::as_str).unwrap_or_default() {
        // i18n: chat list and Running box, the agent is working (verb, ongoing)
        "machine" => tr!("Working"),
        // i18n: chat list and Running box, the turn waits until the network is back
        "network" => tr!("Waiting for the network"),
        // i18n: chat list and Running box, the turn waits until the account's usage limit resets
        _ => tr!("Waiting for the usage limit"),
    }
}

/// The kind of a step in plain words; None for none.
pub fn step_words(state: &str, tool: &str) -> Option<String> {
    match state {
        // i18n: chat list and Running box, the agent is thinking (verb, ongoing)
        "thinking" => Some(tr!("Thinking")),
        // i18n: chat list and Running box, the agent is writing its reply (verb, ongoing)
        "responding" => Some(tr!("Writing")),
        // i18n: chat list and Running box, the agent is shortening its context (verb, ongoing)
        "compacting" => Some(tr!("Compacting")),
        // i18n: chat list and Running box, the agent is waiting (verb, ongoing)
        "waiting" => Some(tr!("Waiting")),
        // i18n: chat list and Running box, the turn waits until the account's usage limit resets
        "limited" => Some(tr!("Waiting for the usage limit")),
        "tool" => Some(tool_words(tool)),
        _ => None,
    }
}

/// A tool's kind in plain words, never its text.
pub fn tool_words(name: &str) -> String {
    let name = name.trim().to_ascii_lowercase();
    match name.as_str() {
        // i18n: chat list and Running box, the kind of step running now
        "bash" | "shell" | "exec_command" => tr!("Running a command"),
        // i18n: chat list and Running box, the kind of step running now
        "read" | "glob" | "grep" | "ls" | "view" => tr!("Reading files"),
        // i18n: chat list and Running box, the kind of step running now
        "edit" | "write" | "multiedit" | "notebookedit" | "apply_patch" => tr!("Editing files"),
        // i18n: chat list and Running box, the kind of step running now
        "websearch" | "webfetch" | "web_search" => tr!("Searching the web"),
        // i18n: chat list and Running box, the kind of step running now: a helper agent does part of the work
        "task" | "agent" => tr!("Working with a helper"),
        // i18n: chat list and Running box, the kind of step running now
        _ if name.contains("clarp-admin") || name.contains("prompt") => tr!("Messaging another agent"),
        // i18n: chat list and Running box, the kind of step running now
        _ => tr!("Using a tool"),
    }
}

/// The agent's own turn beside a fork's (`main_turn`), while it runs:
/// "Also working with Bella · Running a command" (iOS
/// `secondaryLine`): who it works with (`name`, when the turn is between
/// agents; else "another agent") and the kind of its step, never its
/// text. None when it is not running.
pub fn main_turn_line(main: &Object, name: &str) -> Option<String> {
    let text = |o: &Object, key: &str| o.get(key).and_then(Value::as_str).unwrap_or_default().trim().to_ascii_lowercase();
    if text(main, "status") != "running" {
        return None;
    }
    let mut line = if text(main, "audience") != "agents" {
        // i18n: Running box, second line: the agent's own turn also runs while a fork answers
        tr!("Also working")
    } else if name.trim().is_empty() {
        // i18n: Running box, second line: the agent's own turn also runs, with an unnamed agent
        tr!("Also working with another agent")
    } else {
        // i18n: Running box, second line: the agent's own turn also runs; {} is the other agent's name
        tr!("Also working with {}", name.trim())
    };
    let activity = main.get("activity").and_then(Value::as_object);
    let state = activity.map(|a| text(a, "state")).unwrap_or_default();
    let tool = activity.and_then(|a| a.get("tool")).and_then(Value::as_object).map(|t| text(t, "name")).unwrap_or_default();
    if let Some(step) = step_words(&state, &tool) {
        line.push_str(" · ");
        line.push_str(&step);
    }
    Some(line)
}

/// The box's second line: its failed steps ("1 step failed") and the
/// fork's main-turn line, as one quiet line; empty with neither.
pub fn second_line(failed: usize, other: Option<&str>) -> String {
    let failure = match failed {
        0 => None,
        // i18n: Running box, second line: how many steps of the turn failed; {n} is the count
        n => Some(tr!("{n} step failed" | "{n} steps failed" % n)),
    };
    [failure, other.map(str::to_owned)].into_iter().flatten().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" · ")
}

/// The status of a stalled turn's Running box (#249): it reads Seems stuck
/// and holds Stop.
pub const STALLED: &str = "stalled";

/// The prefix of the key a stalled box's Stop sends: `live:stop:<box key>`.
pub const STOP_PREFIX: &str = "live:stop:";

/// A turn another agent's message started, in the Running box: "Working
/// with Bella" (iOS `CoordinationLiveRule.workingText`), never the turn's
/// activity.
pub fn working_with(name: &str) -> String {
    let name = name.trim();
    if name.is_empty() {
        // i18n: Running box, a turn another agent's message started, when that agent has no name
        tr!("Working with another agent")
    } else {
        // i18n: Running box, a turn another agent's message started; {} is that agent's name
        tr!("Working with {}", name)
    }
}

/// Whether a turn is one between agents (`audience` `agents`).
pub fn between_agents(audience: &str) -> bool {
    audience.trim().eq_ignore_ascii_case("agents")
}

/// Who started the turn `trace`: the newest agent prompt of that trace
/// among the rows (its `sender_name`), else `fallback` (the agent row's
/// `working_with`).
pub fn sender_of(rows: &[Message], trace: &str, fallback: &str) -> String {
    let named = (!trace.is_empty())
        .then(|| rows.iter().rev().find(|r| r.role == "user" && r.trace_id == trace && !r.sender_name.trim().is_empty()))
        .flatten();
    named.map_or_else(|| fallback.trim().to_owned(), |r| r.sender_name.trim().to_owned())
}

/// What the Running box says besides the turn's steps.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BoxLook {
    /// What runs ("Running swift build…", `running`).
    pub title: String,
    /// The turn's clock.
    pub meta: String,
    /// The fork's main-turn line (`main_turn_line`).
    pub other: Option<String>,
    /// A stalled turn (#249): `stall::indicator`'s words, which take the
    /// title's place; the box then holds Stop.
    pub stall: Option<String>,
    /// The key that stops the agent, on that Stop.
    pub stop_key: String,
    /// A turn between agents: "Working with <Name>" in the title's place.
    /// It never opens, and its second line says only the kind of step.
    pub with: Option<String>,
    /// That turn's kind of step (`step_words`), never its text.
    pub step: Option<String>,
}

/// The running turn's entries as the conversation view draws them: one
/// Running box (`look`). It stands in the place of the turn's work row
/// (live items folded as Grouped folds them) under its key, so it opens in
/// place as that row does and then lists the turn's steps under it; failed
/// steps go inside it, counted on its second line. Nothing else of the
/// running turn shows: its text in between is not the reply. A turn
/// between agents shows only who it works with and the kind of step; a
/// stalled one says so and holds Stop, so a stuck agent is never hidden.
pub fn running_box(entries: &[Entry], turn_id: &str, look: &BoxLook) -> Vec<Entry> {
    let fold_key = format!("live:fold:{turn_id}");
    let fold = entries.iter().find(|e| e.kind == Kind::Fold && e.key == fold_key);
    let work: Vec<&Entry> = entries.iter().filter(|e| e.kind != Kind::Message && e.kind != Kind::Fold).collect();
    let failed = work.iter().filter(|e| matches!(e.status.as_str(), "failed" | "interrupted")).count();
    let expandable = look.with.is_none() && fold.is_some_and(|f| f.expandable);
    let open = expandable && fold.is_some_and(|f| f.expanded);
    let stalled = look.stall.as_deref().filter(|s| !s.is_empty());
    let title = stalled.or(look.with.as_deref()).unwrap_or(&look.title).to_owned();
    let mut second = second_line(failed, look.other.as_deref());
    if look.with.is_some()
        && let Some(step) = look.step.as_deref().filter(|s| !s.is_empty())
    {
        second = if second.is_empty() { step.to_owned() } else { format!("{step} · {second}") };
    }
    let boxed = Entry {
        key: fold_key,
        kind: Kind::Fold,
        items: fold.map(|f| f.items.clone()).unwrap_or_default(),
        status: if stalled.is_some() { STALLED } else { "running" }.to_owned(),
        title,
        meta: look.meta.clone(),
        secondary: second,
        expandable,
        expanded: open,
        rev: fold.map_or(0, |f| f.rev),
        started_at_ms: entries.iter().filter_map(|e| e.started_at_ms).min(),
        stop: if stalled.is_some() { if look.stop_key.is_empty() { "■".to_owned() } else { look.stop_key.clone() } } else { String::new() },
        ..Entry::default()
    };
    let mut out = vec![boxed];
    if open {
        out.extend(work.into_iter().cloned());
    }
    out
}

/// The newest prompt's turn, while the agent works and no live turn says
/// what runs: its trace, unless its anchor says it ended (None then).
/// Empty when that prompt has no trace.
pub fn busy_turn(rows: &[Message]) -> Option<String> {
    let prompt = rows.iter().rev().find(|r| r.role == "user")?;
    let trace = prompt.trace_id.clone();
    if !trace.is_empty() && rows.iter().any(|r| r.trace_id == trace && r.session_rows.is_some()) {
        return None;
    }
    Some(trace)
}

/// Where a row sits in an opened work box (#727, iOS `WorkBoxContent`): the
/// rows of a turn's opened work are drawn inside one bordered box, each
/// row one band of it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Edge {
    #[default]
    None,
    /// The box's first band (its top border).
    Top,
    Middle,
    /// Its last band (its bottom border).
    Bottom,
    /// A box of one row.
    Only,
}

impl Edge {
    pub fn name(self) -> &'static str {
        match self {
            Edge::None => "",
            Edge::Top => "top",
            Edge::Middle => "middle",
            Edge::Bottom => "bottom",
            Edge::Only => "only",
        }
    }
}

/// One opened work box: the row it goes under (`anchor`, a finished turn's
/// reply) or begins with (`inside`: the Running box, whose steps list under
/// it), and the rows it holds, by id.
#[derive(Debug, Clone, Default)]
pub struct WorkBox {
    pub anchor: String,
    pub inside: bool,
    pub members: HashSet<String>,
}

/// The rows (their ids, in drawing order) with each opened work box
/// gathered: its rows move, in their order, to just under its anchor (an
/// opened turn's work sits under its reply, as on iOS), and each row of a
/// box gets its band. Returns the new order (indexes into `ids`) and each
/// row's band in that order. A box whose anchor is not drawn leaves its
/// rows where they are, unboxed.
pub fn gather(ids: &[&str], boxes: &[WorkBox]) -> (Vec<usize>, Vec<Edge>) {
    let anchored: Vec<&WorkBox> = boxes.iter().filter(|b| ids.contains(&b.anchor.as_str())).collect();
    let moved = |i: usize| anchored.iter().any(|b| b.anchor != ids[i] && b.members.contains(ids[i]));
    let mut order: Vec<usize> = (0..ids.len()).filter(|&i| !moved(i)).collect();
    let mut edges = vec![Edge::None; order.len()];
    for boxed in anchored {
        let Some(at) = order.iter().position(|&i| ids[i] == boxed.anchor) else { continue };
        let members: Vec<usize> = (0..ids.len()).filter(|&i| ids[i] != boxed.anchor && boxed.members.contains(ids[i]) && !order.contains(&i)).collect();
        let first = if boxed.inside { at } else { at + 1 };
        let count = members.len() + usize::from(boxed.inside);
        for (offset, member) in members.into_iter().enumerate() {
            order.insert(at + 1 + offset, member);
            edges.insert(at + 1 + offset, Edge::None);
        }
        for (offset, edge) in edges[first..first + count].iter_mut().enumerate() {
            *edge = match (count, offset) {
                (1, _) => Edge::Only,
                (_, 0) => Edge::Top,
                (n, o) if o + 1 == n => Edge::Bottom,
                _ => Edge::Middle,
            };
        }
    }
    (order, edges)
}
