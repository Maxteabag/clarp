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
    keywords: &'static str,
}

impl Item {
    pub fn key_of(&self) -> String {
        format!("{:?}:{}", self.kind, self.target)
    }
}

fn command(label: &str, action: &str, key: &str, group: &'static str, keywords: &'static str) -> Item {
    Item { kind: Kind::Command, target: action.into(), label: label.into(), detail: String::new(), key: key.into(), group, keywords }
}

/// What the commands' labels depend on.
#[derive(Debug, Clone, Copy, Default)]
pub struct Toggles {
    pub sidebar_visible: bool,
    pub muted: bool,
    pub show_when_ready: bool,
    pub timestamps_visible: bool,
    pub workspace_bar: bool,
    pub shared_filesystem: bool,
    pub activity_mode: i32,
    pub narration: bool,
    pub preview_versions: bool,
    pub detail_level: i32,
}

fn toggle(on: bool, label: &str, action: &str, key: &str, keywords: &'static str) -> Item {
    command(&format!("{} · {label}", if on { "On → Off" } else { "Off → On" }), action, key, "settings", keywords)
}

pub fn commands(toggles: Toggles, reading_theme: &str) -> Vec<Item> {
    let mut rows: Vec<Item> = vec![
        command("Customize key bindings", "edit-keymap", "Ctrl+Alt+,", "view", ""),
        command("Next workspace", "next-workspace", "Ctrl+Alt+W", "view", ""),
        command("New contact & chat", "quick-new-agent", "Ctrl+Shift+N", "agent", "new session hub create"),
        command("Rename contact", "rename-agent", "F2", "agent", "rename name title relabel persona"),
        command("New session", "new", "Ctrl+N", "agent", "new agent start chat provider contact hub"),
        command("Preview versions · update or roll back", "preview-versions", "", "settings", "previous installs rollback downgrade"),
        command("Start an idle contact", "new-contact", "Ctrl+Alt+N", "agent", "new session hub"),
        command("Open agent in terminal", "agent-terminal", "Ctrl+Alt+T", "agent", ""),
        command(
            if toggles.narration { "Disable plain-English tools" } else { "Enable plain-English tools (Spark · extra usage)" },
            "tool-narration",
            "",
            "experiment",
            "",
        ),
        command("Split right", "split-right", "Ctrl+Alt+V", "layout", ""),
        command("Split down", "split-down", "Ctrl+Alt+S", "layout", ""),
        command("Close pane", "close-pane", "Ctrl+Alt+X", "layout", ""),
        command("Zoom pane", "zoom", "Ctrl+Alt+Z", "layout", ""),
        command("Balance panes", "balance", "Ctrl+Alt+=", "layout", ""),
        command(if toggles.sidebar_visible { "Hide sidebar" } else { "Show sidebar" }, "sidebar", "Ctrl+B", "view", ""),
        command("Show/hide keybindings", "shortcut-bar", "Ctrl+Shift+K", "view", ""),
        command("Larger interface", "ui-larger", "Ctrl+=", "view", ""),
        command("Smaller interface", "ui-smaller", "Ctrl+-", "view", ""),
        command("Reset interface size", "ui-reset", "Ctrl+0", "view", ""),
        command("Agents: all chats", "list-all", "", "view", "sidebar list scope everything back"),
        command("Agents: unread only", "list-unread", "", "view", "sidebar list scope filter new"),
        command("Agent conversations (agent to agent)", "list-rooms", "", "view", "pairs rooms sidebar"),
        command("Archived agents", "list-archive", "", "view", "archive restore old sidebar"),
        command("Jump to latest", "jump-latest", "Ctrl+End", "view", "bottom newest scroll follow"),
        command("Retry latest failed message", "retry-message", "Ctrl+Alt+R", "view", "resend send delivery not delivered"),
        command("Dismiss conversation error", "dismiss-error", "Esc", "view", "clear close error warning banner voice synthesis failed"),
        command("Change directory", "change-directory", "Ctrl+Alt+D", "agent", "folder workspace cwd new chat"),
        command("Recent agents", "recent-agents", "Ctrl+R", "agent", "last previous switch back mru history"),
        command("Refresh conversation", "refresh", "F5", "view", ""),
        command("Agent overview", "overview", "Ctrl+Shift+O", "view", ""),
        command("Chats", "chats", "Ctrl+1", "destination", ""),
        command("Updates", "updates", "Ctrl+2", "destination", ""),
        command("Teams", "teams", "Ctrl+3", "destination", ""),
        command("Settings", "settings", "Ctrl+,", "destination", ""),
        command("Host connection", "connection", "", "settings", ""),
        command("Orchestrator settings", "orchestrator", "", "view", ""),
        command("Next agent needing attention", "next-attention", "Ctrl+J", "agent", ""),
        command("Release agent", "release-agent", "Ctrl+Shift+R", "agent", ""),
        command("Stop agent", "stop-agent", "Ctrl+.", "agent", ""),
        command(if toggles.muted { "Enable voice replies" } else { "Mute voice replies" }, "mute", "Ctrl+M", "settings", ""),
        command("Talk", "talk", "Ctrl+Shift+Space", "audio", ""),
        toggle(toggles.workspace_bar, "Workspace bar", "setting:workspaceBarVisible", "", "hide show workspaces tabs top strip"),
        toggle(toggles.timestamps_visible, "Timestamps", "setting:timestampsVisible", "", "date time messages"),
        toggle(toggles.show_when_ready, "Show when ready", "setting:showWhenReady", "", "stream streaming answers typing"),
        toggle(toggles.shared_filesystem, "Shared filesystem access (trusted Host)", "setting:sharedFilesystem", "", "files local folders"),
    ];
    for (mode, label) in ["Grouped", "Always visible", "Group old"].iter().enumerate() {
        let current = if toggles.activity_mode == mode as i32 { " (current)" } else { "" };
        rows.push(command(&format!("Tool activity: {label}{current}"), &format!("setting:activity:{mode}"), if mode == 1 { "Ctrl+Shift+T" } else { "" }, "settings", "tool calls collapse expand grouping"));
    }
    rows.retain(|c| c.target != "preview-versions" || toggles.preview_versions);
    for (level, name) in clarp_engine::Engine::narrator_detail_levels().iter().enumerate() {
        let level = level as i32;
        let note = if toggles.detail_level == level { " (current)" } else if level > 0 { " (uses AI)" } else { " (no AI)" };
        rows.push(command(&format!("Tool detail: {name}{note}"), &format!("setting:detail:{level}"), "", "settings", "explanation explanations narration audience"));
    }
    for theme in clarp_core::reading_theme::themes() {
        let text = |key: &str| theme.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_owned();
        let current = if text("id") == reading_theme { " (current)" } else { "" };
        rows.push(Item {
            detail: text("detail"),
            ..command(
                &format!("Reading theme: {} · {}{current}", text("label"), crate::view::theme_font(theme)),
                &format!("setting:reading:{}", text("id")),
                "",
                "settings",
                "font typeface text contrast readable legible eyes appearance colours",
            )
        });
    }
    rows
}

/// Every row of the settings page that does something, as commands, so
/// Ctrl+K reaches all settings (`(kind, id, label, detail, on)` per row).
/// Rows the list above already covers by hand are skipped.
pub fn settings(rows: &[(String, String, String, String, bool)]) -> Vec<Item> {
    const COVERED: &[&str] =
        &["timestamps", "show-when-ready", "workspace-bar", "shared-filesystem", "activity", "tool-detail", "reading-theme", "spoken-replies", "narration", "connection", "orchestrator"];
    let keywords = "setting settings preference option";
    let mut items = Vec::new();
    for (kind, id, label, detail, on) in rows {
        if id.is_empty() || COVERED.contains(&id.as_str()) {
            continue;
        }
        let target = |delta: i32| format!("settingrow:{id}:{delta}");
        match kind.as_str() {
            "toggle" => items.push(Item { keywords, ..toggle(*on, label, &target(1), "", "") }),
            "choice" => {
                items.push(Item { detail: detail.clone(), keywords, ..command(&format!("{label}: next (now {detail})"), &target(1), "", "settings", "") });
                items.push(Item { detail: detail.clone(), keywords, ..command(&format!("{label}: previous"), &target(-1), "", "settings", "") });
            }
            "action" => items.push(Item { detail: detail.clone(), keywords, ..command(label, &target(1), "", "settings", "") }),
            _ => {}
        }
    }
    items
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
                keywords: "",
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
    let activity = |session: &str| engine.roster().rows().iter().find(|r| r.session == session).map_or(0, |r| r.last_activity);
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
            keywords: "",
        })
        .collect()
}

/// The switcher's rows for `query`; `contacts_only` lists idle contacts only;
/// `settings` are the settings page's rows as commands.
pub fn results(engine: &Engine, query: &str, toggles: Toggles, contacts_only: bool, settings: Vec<Item>) -> Vec<Item> {
    if contacts_only {
        return contacts(engine, query);
    }
    let terms: Vec<String> = query.trim().to_lowercase().split_whitespace().map(str::to_owned).collect();
    let commands: Vec<Item> = commands(toggles, &engine.reading_theme())
        .into_iter()
        .chain(settings)
        .filter(|c| {
            let searchable = format!("{} {} {} {}", c.label, c.group, c.key, c.keywords).to_lowercase();
            terms.iter().all(|term| searchable.contains(term))
        })
        .collect();
    let agents = agents(engine, query);
    let contacts = contacts(engine, query);
    if terms.is_empty() {
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

    #[test]
    fn commands_filter_by_every_term_and_label_their_state() {
        let rows = commands(Toggles { sidebar_visible: true, muted: true, activity_mode: 1, ..Toggles::default() }, "paper");
        assert!(rows.iter().any(|c| c.label == "Hide sidebar"));
        assert!(rows.iter().any(|c| c.label == "Enable voice replies"));
        assert!(rows.iter().any(|c| c.label == "Tool activity: Always visible (current)"));
        assert!(rows.iter().any(|c| c.target == "setting:reading:paper" && c.label.ends_with("(current)")));
        let keep = |item: &Item, terms: &[&str]| {
            let searchable = format!("{} {} {} {}", item.label, item.group, item.key, item.keywords).to_lowercase();
            terms.iter().all(|t| searchable.contains(t))
        };
        let split: Vec<_> = rows.iter().filter(|c| keep(c, &["split", "right"])).collect();
        assert_eq!(split.len(), 1);
        assert_eq!(split[0].target, "split-right");
    }

    #[test]
    fn every_settings_row_becomes_a_command() {
        let row = |kind: &str, id: &str, label: &str, on: bool| (kind.to_owned(), id.to_owned(), label.to_owned(), "Kokoro".to_owned(), on);
        let items = settings(&[
            row("section", "", "APPEARANCE", false),
            row("toggle", "minimal-ui", "Minimal UI", false),
            row("toggle", "timestamps", "Timestamps", true),
            row("choice", "voice-provider", "Voice provider", false),
            row("info", "", "Host version", false),
        ]);
        let targets: Vec<&str> = items.iter().map(|i| i.target.as_str()).collect();
        assert_eq!(targets, ["settingrow:minimal-ui:1", "settingrow:voice-provider:1", "settingrow:voice-provider:-1"], "sections, info and hand-covered rows are left out");
        assert_eq!(items[0].label, "Off → On · Minimal UI");
    }

    #[test]
    fn the_selection_survives_a_rebuild() {
        let a = Item { kind: Kind::Agent, target: "rachel".into(), label: "Rachel".into(), detail: String::new(), key: String::new(), group: "agent", keywords: "" };
        let b = Item { target: "mike".into(), label: "Mike".into(), ..a.clone() };
        assert_eq!(keep_selection(&[a.clone(), b.clone()], &b.key_of()), 1);
        assert_eq!(keep_selection(&[b.clone(), a.clone()], &b.key_of()), 0, "the row moved; the selection follows it");
        assert_eq!(keep_selection(&[a], "Agent:gone"), 0);
        assert_eq!(keep_selection(&[], "x"), -1);
    }
}
