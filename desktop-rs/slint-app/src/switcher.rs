//! The quick switcher (QuickSwitcher.qml): one search over agents, commands
//! and settings. With a query, agents come first (best match first); with
//! none, commands do. The selection is kept by identity while results
//! rebuild, so a streaming agent cannot move Enter onto another row.

use clarp_core::protocol::display_name;
use clarp_engine::Engine;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Kind {
    Agent,
    Command,
    /// An idle contact to start (launch dialogs).
    Contact,
    /// A message found by message search (`search_view.rs`): the target is
    /// its chat and id, the detail its snippet as Markdown.
    Message,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Item {
    pub kind: Kind,
    /// The session (agent) or action (command).
    pub target: String,
    pub label: String,
    pub detail: String,
    pub key: String,
    pub group: &'static str,
    /// What it does, one line (commands and settings).
    pub description: String,
    pub aliases: &'static [&'static str],
    /// A setting's current value ("On", "Paper").
    pub value: String,
    /// The catalogue id it is described by; its place breaks ties.
    pub entry: &'static str,
}

impl Item {
    pub fn key_of(&self) -> String {
        format!("{:?}:{}", self.kind, self.target)
    }

    pub fn message(target: String, label: String, snippet: String) -> Self {
        Item {
            kind: Kind::Message,
            target,
            label,
            detail: snippet,
            key: String::new(),
            group: "message",
            description: String::new(),
            aliases: &[],
            value: String::new(),
            entry: "",
        }
    }
}

/// A command or setting `id` as the catalogue describes it.
fn described(id: &str, target: String, key: &str) -> Item {
    let entry = crate::catalogue::find(id);
    Item {
        kind: Kind::Command,
        target,
        label: entry.map_or(id, |e| e.label).into(),
        detail: String::new(),
        key: key.into(),
        group: entry.map_or("view", |e| e.group),
        description: entry.map_or("", |e| e.description).into(),
        aliases: entry.map_or(&[][..], |e| e.aliases),
        value: String::new(),
        entry: entry.map_or("", |e| e.id),
    }
}

fn command(action: &str, key: &str) -> Item {
    described(action, action.into(), key)
}

/// What the commands depend on.
#[derive(Debug, Clone, Copy, Default)]
pub struct Toggles {
    pub preview_versions: bool,
}

/// The commands; the settings come from the settings page (`settings`).
pub fn commands(toggles: Toggles) -> Vec<Item> {
    let mut rows: Vec<Item> = [
        ("edit-keymap", "Ctrl+Alt+,"),
        ("choose-font", ""),
        ("reset-font", ""),
        ("new-workspace", "Ctrl+T"),
        ("next-workspace", "Ctrl+Tab"),
        ("previous-workspace", "Ctrl+Shift+Tab"),
        ("close-workspace", "Ctrl+W"),
        ("quick-new-agent", "Ctrl+Shift+N"),
        ("rename-agent", "F2"),
        ("new", "Ctrl+N"),
        ("preview-versions", ""),
        ("new-contact", "Ctrl+Alt+N"),
        ("agent-terminal", "Ctrl+Alt+T"),
        ("split-right", "Ctrl+Alt+V"),
        ("split-down", "Ctrl+Alt+S"),
        ("close-pane", "Ctrl+Alt+X"),
        ("zoom", "Ctrl+Alt+Z"),
        ("balance", "Ctrl+Alt+="),
        ("chat-zoom-in", "Ctrl+="),
        ("chat-zoom-out", "Ctrl+-"),
        ("chat-zoom-reset", "Ctrl+0"),
        ("ui-larger", "Ctrl+Alt+PageUp"),
        ("ui-smaller", "Ctrl+Alt+PageDown"),
        ("ui-reset", "Ctrl+Alt+0"),
        ("list-all", ""),
        ("list-unread", ""),
        ("list-rooms", ""),
        ("list-archive", ""),
        ("link-hints", "F"),
        ("jump-latest", "Ctrl+End"),
        ("retry-message", "Ctrl+Alt+R"),
        ("dismiss-error", "Esc"),
        ("dismiss-layout-warning", "Esc"),
        ("keep-layout", "Ctrl+Shift+S"),
        ("change-directory", "Ctrl+Alt+D"),
        ("recent-agents", "Ctrl+R"),
        ("search-messages", "Ctrl+F"),
        ("refresh", "F5"),
        ("overview", "Ctrl+Shift+O"),
        ("chats", "Ctrl+1"),
        ("updates", "Ctrl+2"),
        ("teams", "Ctrl+3"),
        ("settings", "Ctrl+,"),
        ("next-attention", "Ctrl+J"),
        ("next-agent", ""),
        ("previous-agent", ""),
        ("release-agent", "Ctrl+Shift+R"),
        ("stop-agent", "Ctrl+."),
        ("talk", "Ctrl+Shift+Space"),
    ]
    .into_iter()
    .map(|(action, key)| command(action, key))
    .collect();
    rows.retain(|c| c.target != "preview-versions" || toggles.preview_versions);
    rows
}

/// One row of the settings page: `(kind, id, label, detail, on)`.
pub type SettingRow = (String, String, String, String, bool);

/// Every row of the settings page that does something, as a switcher row
/// with its current value: Enter switches a toggle, opens a choice's picker
/// (`settingpicker:ID`) and runs an action.
pub fn settings(rows: &[SettingRow]) -> Vec<Item> {
    // The font rows are the commands Choose font… and Reset font.
    const COMMANDS: &[&str] = &["font", "reset-font"];
    let mut items = Vec::new();
    for (kind, id, label, detail, on) in rows {
        if id.is_empty() || COMMANDS.contains(&id.as_str()) {
            continue;
        }
        let (target, value) = match kind.as_str() {
            "toggle" => (format!("settingrow:{id}:1"), if *on { "On" } else { "Off" }.to_owned()),
            "choice" | "number" => (format!("settingpicker:{id}"), detail.clone()),
            "action" => (format!("settingrow:{id}:1"), String::new()),
            _ => continue,
        };
        let mut item = described(id, target, "");
        // The page's own label (the Host's name for its connection).
        if crate::catalogue::find(id).is_none() || kind == "action" {
            item.label = label.clone();
        }
        item.value = value;
        if kind == "action" {
            item.detail = detail.clone();
        }
        items.push(item);
    }
    items
}

/// A choice's options as rows (`settingpick:ID:VALUE`), the current marked.
pub fn picker(id: &str, options: &[(String, String, String, bool)], query: &str) -> Vec<Item> {
    let mut rows: Vec<(u32, Item)> = options
        .iter()
        .filter_map(|(value, label, description, current)| {
            let score = crate::catalogue::score(query, label, &[], description)?;
            let item = Item {
                kind: Kind::Command,
                target: format!("settingpick:{id}:{value}"),
                label: label.clone(),
                detail: String::new(),
                key: String::new(),
                group: "settings",
                description: description.clone(),
                aliases: &[],
                value: if *current { "Current".into() } else { String::new() },
                entry: "",
            };
            Some((score, item))
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0));
    rows.into_iter().map(|(_, item)| item).collect()
}

/// Commands and settings matching `query`, best first; ties keep the
/// catalogue's order. An empty query keeps them all in their own order.
pub fn rank(items: Vec<Item>, query: &str) -> Vec<Item> {
    if query.trim().is_empty() {
        return items;
    }
    let mut scored: Vec<(u32, usize, Item)> = items
        .into_iter()
        .filter_map(|item| {
            let score = crate::catalogue::score(query, &item.label, item.aliases, &format!("{} {}", item.description, item.group))?;
            Some((score, crate::catalogue::order(item.entry), item))
        })
        .collect();
    // A typo is forgiven while nothing matches as typed.
    let typed = crate::catalogue::as_typed(query);
    if scored.iter().any(|s| s.0 >= typed) {
        scored.retain(|s| s.0 >= typed);
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    scored.into_iter().map(|(_, _, item)| item).collect()
}


/// Agents matching `query`, best first (C++ `matchingAgents`).
pub fn agents(engine: &Engine, query: &str) -> Vec<Item> {
    let needle = query.trim().to_lowercase();
    let mut rows: Vec<(u8, Item)> = engine
        .roster()
        .agents()
        .iter()
        .filter(|agent| {
            needle.is_empty()
                || [display_name(agent), agent.session.as_str(), agent.working_directory.as_str()]
                    .iter()
                    .any(|field| field.to_lowercase().contains(&needle))
        })
        .map(|agent| {
            let rank = if needle.is_empty() { 0 } else { clarp_core::roster::switcher_rank(display_name(agent), &agent.session, &needle) };
            let state = if agent.unread { format!("{} · unread", agent.latest_state) } else { agent.latest_state.clone() };
            let item = Item {
                kind: Kind::Agent,
                target: agent.session.clone(),
                label: display_name(agent).to_owned(),
                detail: format!("{} · {state}", agent.backend),
                key: String::new(),
                group: "agent",
                description: String::new(),
                aliases: &[],
                value: String::new(),
                entry: "",
            };
            (rank, item)
        })
        .collect();
    rows.sort_by_key(|(rank, _)| *rank);
    rows.into_iter().map(|(_, item)| item).collect()
}

/// Ctrl+R: agents by recency, the open chat left out so the first row is
/// the one before it. Chats opened in this window come first (most recent
/// first), then the rest by their latest activity.
pub fn recent(engine: &Engine, opened: &[String], query: &str) -> Vec<Item> {
    let selected = engine.selected_session();
    let mut all = agents(engine, query);
    let activity = |session: &str| engine.roster().find(session).map_or(0, |a| a.last_activity);
    let rank = |item: &Item| opened.iter().position(|s| *s == item.target);
    all.retain(|item| item.target != selected);
    all.sort_by(|a, b| match (rank(a), rank(b)) {
        (Some(x), Some(y)) => x.cmp(&y),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => activity(&b.target).cmp(&activity(&a.target)),
    });
    all
}

// ---- launch dialogs
/// Idle contacts matching `query`, each started with the quick-start backend.
pub fn contacts(engine: &Engine, query: &str) -> Vec<Item> {
    let backend = engine.quick_start_backend();
    engine
        .matching_contacts(query)
        .iter()
        .filter_map(|c| c.get("name").and_then(|n| n.as_str()))
        .map(|name| Item {
            kind: Kind::Contact,
            target: name.to_owned(),
            label: format!("Start {name}"),
            detail: format!("New session · {backend} · ~"),
            key: String::new(),
            group: "contact",
            description: String::new(),
            aliases: &[],
            value: String::new(),
            entry: "",
        })
        .collect()
}

/// The switcher's rows for `query`; `contacts_only` lists idle contacts only;
/// `settings` are the settings page's rows as commands.
pub fn results(engine: &Engine, query: &str, toggles: Toggles, contacts_only: bool, settings: Vec<Item>) -> Vec<Item> {
    if contacts_only {
        return contacts(engine, query);
    }
    let commands = rank(commands(toggles).into_iter().chain(settings).collect(), query);
    let agents = agents(engine, query);
    let contacts = contacts(engine, query);
    if query.trim().is_empty() {
        commands.into_iter().chain(agents).chain(contacts).collect()
    } else {
        agents.into_iter().chain(contacts).chain(commands).collect()
    }
}

/// The row to select after a rebuild: the one selected before, else the first.
pub fn keep_selection(results: &[Item], selected_key: &str) -> i32 {
    match results.iter().position(|item| item.key_of() == selected_key) {
        Some(index) => index as i32,
        None if results.is_empty() => -1,
        None => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(kind: &str, id: &str, label: &str, detail: &str, on: bool) -> SettingRow {
        (kind.to_owned(), id.to_owned(), label.to_owned(), detail.to_owned(), on)
    }

    #[test]
    fn commands_filter_by_every_term() {
        let rows = commands(Toggles::default());
        let split = rank(rows.clone(), "split right");
        assert_eq!(split.len(), 1);
        assert_eq!(split[0].target, "split-right");
        assert!(rows.iter().all(|c| c.target != "preview-versions"), "only where previews can install");
        assert!(commands(Toggles { preview_versions: true }).iter().any(|c| c.target == "preview-versions"));
    }

    #[test]
    fn the_font_commands_are_in_the_palette() {
        let rows = commands(Toggles::default());
        let find = |target: &str| rows.iter().find(|c| c.target == target).map(|c| c.label.clone());
        assert_eq!(find("choose-font").as_deref(), Some("Choose font…"));
        assert_eq!(find("reset-font").as_deref(), Some("Reset font to theme default"));
    }

    #[test]
    fn every_command_is_described_with_aliases() {
        for item in commands(Toggles { preview_versions: true }) {
            let entry = crate::catalogue::find(&item.target);
            assert!(entry.is_some_and(|e| !e.description.is_empty() && e.aliases.len() >= 2), "{} ({}) needs a catalogue entry", item.label, item.target);
            assert_eq!(item.description, entry.map(|e| e.description).unwrap_or_default());
        }
    }

    #[test]
    fn every_settings_row_becomes_a_described_row_with_its_value() {
        let items = settings(&[
            row("section", "", "APPEARANCE", "", false),
            row("toggle", "nav-rail", "Activity bar", "", true),
            row("toggle", "timestamps", "Timestamps", "", false),
            row("choice", "voice-provider", "Voice provider", "Kokoro", false),
            row("action", "connection", "studio", "http://host  ·  connected", false),
            row("info", "", "Host version", "1.0", false),
        ]);
        let shown: Vec<(&str, &str, &str)> = items.iter().map(|i| (i.target.as_str(), i.label.as_str(), i.value.as_str())).collect();
        assert_eq!(
            shown,
            [
                ("settingrow:nav-rail:1", "Activity bar", "On"),
                ("settingrow:timestamps:1", "Timestamps", "Off"),
                ("settingpicker:voice-provider", "Voice provider", "Kokoro"),
                ("settingrow:connection:1", "studio", ""),
            ],
            "sections and information are left out; a choice opens its picker"
        );
        assert!(items[0].description.contains("far left"));
        assert_eq!(items[3].detail, "http://host  ·  connected", "an action keeps its detail");
    }

    #[test]
    fn hide_ranks_the_activity_bar_among_the_commands() {
        let rows: Vec<Item> = commands(Toggles::default())
            .into_iter()
            .chain(settings(&[
                row("toggle", "timestamps", "Timestamps", "", false),
                row("toggle", "workspace-bar", "Workspace bar", "", true),
                row("toggle", "explorer", "Explorer", "", true),
                row("toggle", "nav-rail", "Activity bar", "", true),
                row("choice", "activity", "Tool activity", "Grouped", false),
            ]))
            .collect();
        for query in ["hide", "collapse", "toggle", "colapse", "left bar", "icons", "rail"] {
            let top: Vec<String> = rank(rows.clone(), query).into_iter().take(3).map(|i| i.label).collect();
            assert!(top.iter().any(|l| l == "Activity bar"), "{query:?}: {top:?}");
        }
    }

    #[test]
    fn a_picker_lists_the_options_and_marks_the_current() {
        let options = [("paper".into(), "Paper".into(), "Warm light".into(), true), ("night".into(), "Night".into(), "Dark blue".into(), false)];
        let all = picker("reading-theme", &options, "");
        assert_eq!(all.iter().map(|i| i.target.as_str()).collect::<Vec<_>>(), ["settingpick:reading-theme:paper", "settingpick:reading-theme:night"]);
        assert_eq!(all[0].value, "Current");
        assert_eq!(picker("reading-theme", &options, "dark")[0].label, "Night", "the options' descriptions are searched too");
    }

    #[test]
    fn the_selection_survives_a_rebuild() {
        let a = Item { kind: Kind::Agent, target: "rachel".into(), label: "Rachel".into(), ..command("new", "") };
        let b = Item { target: "mike".into(), label: "Mike".into(), ..a.clone() };
        assert_eq!(keep_selection(&[a.clone(), b.clone()], &b.key_of()), 1);
        assert_eq!(keep_selection(&[b.clone(), a.clone()], &b.key_of()), 0, "the row moved; the selection follows it");
        assert_eq!(keep_selection(&[a], "Agent:gone"), 0);
        assert_eq!(keep_selection(&[], "x"), -1);
    }
}
