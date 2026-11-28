//! The app's settings, registered once: the settings page lists them in
//! this order within their sections, and Ctrl+K reaches every one. A new
//! group of settings can live in a module of its own, added to `register`.

use std::rc::Rc;

use clarp_core::reading_theme::{self, FontOverride};
use clarp_engine::Change;
use serde_json::Value;

use clarp_core::tr;

use super::{Choice, Entry, Setting, e, en, keyed, shown};
use crate::{App, AppWindow};

/// Tool activity's options, in the app's language: (label, the line about it).
fn activity_options() -> [(String, String); 4] {
    [
        (
            // i18n: Settings > Tool activity, option (adjective): every turn's tool calls folded into one row
            tr!("Grouped"),
            // i18n: Settings > Tool activity, the line under the option "Grouped"
            tr!("One collapsible work row per turn."),
        ),
        (
            // i18n: Settings > Tool activity, option: every tool call is always shown
            tr!("Always visible"),
            // i18n: Settings > Tool activity, the line under the option "Always visible"
            tr!("Every tool call as its own row."),
        ),
        (
            // i18n: Settings > Tool activity, option (imperative-like name): older tool calls are grouped, recent ones shown
            tr!("Group old"),
            // i18n: Settings > Tool activity, the line under the option "Group old"
            tr!("Recent calls shown; older ones grouped."),
        ),
        (
            // i18n: Settings > Tool activity, option: tool calls are hidden once the turn is finished
            tr!("Hide when done"),
            // i18n: Settings > Tool activity, the line under the option "Hide when done"
            tr!("Visible only while the turn runs."),
        ),
    ]
}
/// This device shows the quiet line where agent messages were (#653).
pub const COORDINATION_LINE: &str = "conversation/coordinationLine";
/// The double-press windows offered, in milliseconds.
const DOUBLE_PRESS: [u64; 8] = [150, 200, 250, 300, 400, 500, 700, 1000];
/// The chat's font sizes offered, in pixels.
const FONT_SIZE_MIN: f64 = 11.0;
const FONT_SIZE_MAX: f64 = 28.0;

pub(super) fn register() -> Vec<Setting> {
    let mut all = Vec::new();
    all.extend(chats());
    all.extend(experiments());
    all.extend(startup());
    all.extend(super::language::register());
    all.extend(appearance());
    all.extend(voice());
    all.extend(notifications());
    all.extend(host());
    all.extend(super::lanes::register());
    all.extend(keyboard());
    // Every other preference (clarp_core::prefs) and the settings file.
    all.extend(super::prefs::register());
    all
}

fn flag(app: &App, key: &str, default: bool) -> bool {
    app.engine.borrow().settings().boolean(key, default)
}

fn store(app: &App, key: &str, value: impl Into<Value>) {
    app.engine.borrow_mut().settings_mut().set(key, value);
}

/// Timestamps and the workspace bar live in the app's preferences too.
fn preference(app: &Rc<App>, key: &str, on: bool, timestamps: bool) {
    {
        let mut prefs = app.prefs.borrow_mut();
        if timestamps {
            prefs.timestamps = on;
        } else {
            prefs.workspace_bar = on;
        }
    }
    store(app, key, on);
    app.rebuild_transcripts();
    app.refresh(&[Change::Panes, Change::Preferences]);
}

fn chats() -> Vec<Setting> {
    vec![
        Setting::toggle(
            keyed(e(
                "timestamps",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Time beside each message.
                en::tr!("Timestamps"),
                "chats",
                // i18n: Settings page and Ctrl+K, the line under the setting “Timestamps” saying what it does
                en::tr!("Time beside each message."),
                shown!["time", "date", "clock", "message times", "stamps"],
            ), &["setting:timestampsVisible"]),
            "CHATS",
            |app| app.prefs.borrow().timestamps,
            |app, _, on| preference(app, "conversation/timestampsVisible", on, true),
        )
        .default("off"),
        Setting::toggle(
            e(
                "show-when-ready",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Show replies once complete.
                en::tr!("Show when ready"),
                "chats",
                // i18n: Settings page and Ctrl+K, the line under the setting “Show when ready” saying what it does
                en::tr!("Show replies once complete."),
                &["stream", "streaming", "typing", "live text", "wait for answer", "toggle"],
            ),
            "CHATS",
            |app| app.engine.borrow().show_when_ready(),
            |app, _, on| app.engine.borrow_mut().set_show_when_ready(on),
        ),
        Setting::stored_toggle(
            e(
                "reduced-motion",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Disable animations and shimmer.
                en::tr!("Reduce Motion"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Reduce Motion” saying what it does
                en::tr!("Disable animations and shimmer."),
                &["animations", "animation", "motion", "accessibility", "reduce animations", "toggle", "still", "shimmer"],
            ),
            "CHATS",
            "appearance/reducedMotion",
            false,
        ),
        Setting::choice(
            keyed(
                e(
                    "activity",
                    // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: How tool calls appear in chat.
                    en::tr!("Tool activity"),
                    "chats",
                    // i18n: Settings page and Ctrl+K, the line under the setting “Tool activity” saying what it does
                    en::tr!("How tool calls appear in chat."),
                    &["tool calls", "tools", "collapse", "expand", "grouping", "group tools", "activity", "hide tools", "hide when done", "quiet chat", "work row"],
                ),
                &["tools"],
            ),
            "CHATS",
            |_| activity_options().into_iter().enumerate().map(|(i, (label, about))| Choice::new(i.to_string(), label).about(about)).collect(),
            |app| app.engine.borrow().activity_mode().clamp(0, clarp_core::presentation::HIDE_WHEN_DONE).to_string(),
            |app, _, value| {
                if let Ok(mode) = value.parse() {
                    app.engine.borrow_mut().set_activity_mode(mode);
                }
            },
        )
        .default("0"),
        // Send while working (#920): the connected Host's setting, the same
        // in every app; listed only while that Host offers it. The words are
        // the apps' own; the footer is its description.
        Setting::choice(
            e("send-while-working", clarp_core::send_while_working::TITLE, "chats", clarp_core::send_while_working::FOOTER, &["steer", "steer now", "after the turn", "stop and send", "while working", "busy", "interrupt"]),
            "CHATS",
            |_| clarp_core::send_while_working::Mode::ALL.iter().map(|m| Choice::new(m.wire(), m.label()).about(m.about())).collect(),
            |app| app.engine.borrow().send_while_working().wire().to_owned(),
            |app, _, value| {
                if let Some(mode) = clarp_core::send_while_working::Mode::from_wire(value) {
                    app.engine.borrow_mut().set_send_while_working(mode);
                }
            },
        )
        .default(clarp_core::send_while_working::Mode::Steer.wire())
        .when(|app| app.engine.borrow().send_while_working_offered()),
        // Conversation view for every chat (#702), this device's switch:
        // a chat's own choice in its menu still wins. Listed while a Host
        // this desktop reads offers the views.
        Setting::toggle(
            e("clean-chat", clarp_core::chat_view::CLEAN_TITLE, "chats", clarp_core::chat_view::CLEAN_DETAIL, &["conversation view", "session view", "chat view", "only replies", "hide work", "running box", "toggle"]),
            "CHATS",
            |app| app.engine.borrow().clean_chat(),
            |app, _, on| {
                app.engine.borrow_mut().set_clean_chat(on);
                app.refresh(&[Change::Preferences]);
            },
        )
        .default("off")
        .when(|app| app.engine.borrow().chat_view_anywhere()),
        // Coordination between agents (`hidden_coordination`, #653): the
        // Host's own setting, written only when the reader flips it.
        Setting::toggle(
            e("agent-messages", clarp_core::coordination::SETTING, "chats", "Show inter-agent messages (Host setting).", &["coordination", "between agents", "agent to agent", "a2a", "hide agent messages", "quiet chat", "toggle"]),
            "CHATS",
            |app| app.engine.borrow().coordination_shown(),
            |app, _, on| app.engine.borrow_mut().set_coordination_shown(on),
        )
        .when(|app| app.engine.borrow().hidden_coordination()),
        // This device's choice, next to it: the quiet "N messages between
        // agents" line, or nothing at all.
        Setting::toggle(
            e("agent-messages-line", clarp_core::coordination::LINE_SETTING, "chats", "Show marker where agent messages are hidden.", &["agent messages line", "messages between agents", "hidden messages", "count line", "quiet chat", "toggle"]),
            "CHATS",
            |app| app.engine.borrow().settings().boolean(COORDINATION_LINE, true),
            |app, _, on| {
                store(app, COORDINATION_LINE, on);
                app.refresh(&[Change::Preferences]);
            },
        )
        .default("on")
        // Listed whenever the Host offers it: a chat's own hide under a
        // Host set to show still draws the line.
        .when(|app| app.engine.borrow().hidden_coordination()),
        // Off, the working row shimmers in the chat and the shortcut bar
        // names the stop key.
        Setting::stored_toggle(
            e(
                "live-status-line",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Show current step and Stop above the composer.
                en::tr!("Live status line"),
                "chats",
                // i18n: Settings page and Ctrl+K, the line under the setting “Live status line” saying what it does
                en::tr!("Show current step and Stop above the composer."),
                &["status line", "stop hint", "activity bar under chat", "working indicator", "live status", "current tool", "progress line", "elapsed time"],
            ),
            "CHATS",
            crate::profile_view::LIVE_STATUS_LINE,
            false,
        ),
        Setting::toggle(
            keyed(
                e(
                    "show-queue",
                    // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Show waiting messages strip above composer.
                    en::tr!("Show the queue"),
                    "chats",
                    // i18n: Settings page and Ctrl+K, the line under the setting “Show the queue” saying what it does
                    en::tr!("Show waiting messages strip above composer."),
                    &["queue", "queue strip", "waiting messages", "messages waiting", "send now", "hide queue", "show queue", "toggle"],
                ),
                &["toggle-queue"],
            ),
            "CHATS",
            |app| crate::queue_strip::shown(&app.engine.borrow()),
            |app, _, on| crate::queue_strip::set_shown(app, on),
        )
        .default("on"),
        Setting::stored_toggle(
            e(
                "send-animation",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Animate sent messages into the chat.
                en::tr!("Send animation"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Send animation” saying what it does
                en::tr!("Animate sent messages into the chat."),
                &["send", "sending", "message animation", "fly", "flight", "telegram", "bubble", "composer", "toggle"],
            ),
            "CHATS",
            crate::profile_view::SEND_ANIMATION,
            true,
        ),
        // Device preferences (Maxteabag/clarp#651), never a Host's: chats
        // from every paired Host, and the badge naming each chat's Host.
        Setting::toggle(
            e(
                "all-hosts",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: List chats from all paired Hosts.
                en::tr!("Show chats from all Hosts"),
                "chats",
                // i18n: Settings page and Ctrl+K, the line under the setting “Show chats from all Hosts” saying what it does
                en::tr!("List chats from all paired Hosts."),
                &["hosts", "all hosts", "paired hosts", "other hosts", "every host", "merge hosts", "servers", "multiple hosts", "combined list"],
            ),
            "CHATS",
            |app| app.engine.borrow().all_hosts(),
            |app, _, on| {
                app.engine.borrow_mut().set_all_hosts(on);
                crate::pump_now(app);
            },
        )
        .default("off"),
        Setting::toggle(
            e(
                "host-badges",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Show Host badge on rows and headers.
                en::tr!("Show the Host on each chat"),
                "chats",
                // i18n: Settings page and Ctrl+K, the line under the setting “Show the Host on each chat” saying what it does
                en::tr!("Show Host badge on rows and headers."),
                &["host badge", "host name", "which host", "badges", "host label", "server name", "where it lives"],
            ),
            "CHATS",
            |app| app.engine.borrow().host_badges_setting(),
            |app, _, on| {
                app.engine.borrow_mut().set_host_badges_setting(on);
                crate::pump_now(app);
            },
        )
        .default("on"),
        // The Host's setting, on Hosts that send live items (§6).
        Setting::toggle(
            keyed(
                e(
                    "tool-explanations",
                    // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: The Host's plain-language explanation of each tool row.
                    en::tr!("Tool explanations (Host)"),
                    "chats",
                    // i18n: Settings page and Ctrl+K, the line under the setting “Tool explanations (Host)” saying what it does
                    en::tr!("The Host's plain-language explanation of each tool row."),
                    &["explain", "explanations", "plain english", "narration", "tool labels", "toggle"],
                ),
                &["toggle-explanations"],
            ),
            "CHATS",
            |app| {
                let engine = app.engine.borrow();
                engine.tool_explanations_enabled(engine.selected_session())
            },
            |app, _, on| {
                let now = {
                    let engine = app.engine.borrow();
                    engine.tool_explanations_enabled(engine.selected_session())
                };
                if now != on {
                    crate::commands::toggle_explanations(app);
                }
            },
        )
        .when(|app| app.engine.borrow().tool_explanation_setting()),
    ]
}

fn experiments() -> Vec<Setting> {
    vec![
        Setting::stored_toggle(
            e(
                "scroll-dashcam",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Record recent scroll positions to report jump bugs.
                en::tr!("Scroll dashcam"),
                "diagnostics",
                // i18n: Settings page and Ctrl+K, the line under the setting “Scroll dashcam” saying what it does
                en::tr!("Record recent scroll positions to report jump bugs."),
                &["dashcam", "scroll", "flash", "jump", "diagnostics", "debug", "recorder", "jitter"],
            ),
            "DIAGNOSTICS",
            crate::scroll_journal::SETTING,
            false,
        ),
        Setting::toggle(
            keyed(e(
                "narration",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Explain tool calls in plain English (uses AI).
                en::tr!("Plain-English tools"),
                "experiment",
                // i18n: Settings page and Ctrl+K, the line under the setting “Plain-English tools” saying what it does
                en::tr!("Explain tool calls in plain English (uses AI)."),
                &["narration", "narrator", "spark", "explain tools", "plain english", "toggle"],
            ), &["tool-narration"]),
            "EXPERIMENTS",
            |app| app.engine.borrow().narrator_enabled(),
            |app, _, on| app.engine.borrow_mut().set_narrator_enabled(on),
        ),
        Setting::choice(
            e(
                "tool-detail",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: Explanation detail level.
                en::tr!("Tool detail"),
                "experiment",
                // i18n: Settings page and Ctrl+K, the line under the setting “Tool detail” saying what it does
                en::tr!("Explanation detail level."),
                &["detail level", "explanation", "verbosity", "narration", "audience", "explanations"],
            ),
            "EXPERIMENTS",
            |_| {
                clarp_engine::Engine::narrator_detail_levels()
                    .iter()
                    .enumerate()
                    .map(|(i, name)| {
                        let about = if i == 0 {
                            // i18n: Settings > Tool detail, the line beside the first level: it explains tools without an AI model
                            tr!("No AI")
                        } else {
                            // i18n: Settings > Tool detail, the line beside the other levels: an AI model writes the explanations
                            tr!("Uses AI")
                        };
                        Choice::new(i.to_string(), detail_level_name(i, name)).about(about)
                    })
                    .collect()
            },
            |app| app.engine.borrow().narrator_detail_level().clamp(0, 4).to_string(),
            |app, _, value| {
                if let Ok(level) = value.parse() {
                    app.engine.borrow_mut().set_narrator_detail_level(level);
                }
            },
        ),
    ]
}

/// A tool-detail level's name in the app's language (`clarp_core::narrator`
/// keeps them in English).
fn detail_level_name(level: usize, english: &str) -> String {
    match level {
        // i18n: Settings > Tool detail, level 1 of 5 (noun): explanations written for developers, the original tool calls
        0 => tr!("Developer"),
        // i18n: Settings > Tool detail, level 2 of 5 (adjective): actions with commands and paths
        1 => tr!("Technical"),
        // i18n: Settings > Tool detail, level 3 of 5 (adjective): actions with key technical details
        2 => tr!("Balanced"),
        // i18n: Settings > Tool detail, level 4 of 5: everyday language, no code or paths
        3 => tr!("Plain English"),
        // i18n: Settings > Tool detail, level 5 of 5 (a playful name): explained so simply a grandmother would follow, no technical terms
        4 => tr!("Grandma"),
        _ => super::local(english),
    }
}

/// An agent picture size's name in the app's language (`avatar_view` keeps
/// them in English).
fn avatar_size_name(id: &str, english: &str) -> String {
    match id {
        // i18n: Settings > Agent picture size, option (adjective): the small portrait size
        "small" => tr!("size" => "Small"),
        // i18n: Settings > Agent picture size, option (adjective): the medium portrait size
        "medium" => tr!("size" => "Medium"),
        // i18n: Settings > Agent picture size, option (adjective): the large portrait size
        "large" => tr!("size" => "Large"),
        _ => super::local(english),
    }
}

fn startup() -> Vec<Setting> {
    vec![
        Setting::stored_toggle(
            e(
                "new-agent-on-startup",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Opens the new-agent hub each time Clarp starts.
                en::tr!("Start a new agent when opening Clarp"),
                "startup",
                // i18n: Settings page and Ctrl+K, the line under the setting “Start a new agent when opening Clarp” saying what it does
                en::tr!("Opens the new-agent hub each time Clarp starts."),
                &["startup", "launch", "on open", "new session", "boot", "toggle"],
            ),
            "STARTUP",
            "launch/newAgentOnStartup",
            false,
        ),
        Setting::stored_toggle(
            e(
                "anonymous-agents",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: New agents start without a contact's name and persona.
                en::tr!("Anonymous agents by default"),
                "agent",
                // i18n: Settings page and Ctrl+K, the line under the setting “Anonymous agents by default” saying what it does
                en::tr!("New agents start without a contact's name and persona."),
                &["anonymous", "nameless", "persona", "identity", "contact", "toggle"],
            ),
            "AGENT IDENTITY",
            "launch/anonymousAgents",
            true,
        ),
    ]
}

/// The reading theme's font: the reader's override and what it resolves to.
fn font(app: &App) -> (String, Option<FontOverride>, reading_theme::ResolvedFont, f64) {
    let engine = app.engine.borrow();
    let theme_id = engine.reading_theme();
    let theme = reading_theme::theme(&theme_id);
    let chosen = engine.font_override(&theme_id);
    let resolved = crate::view::resolved_font(theme, chosen.as_ref());
    let theme_size = theme.get("fontPixelSize").and_then(Value::as_f64).unwrap_or(15.0);
    (theme_id, chosen, resolved, theme_size)
}

/// The reading theme's font size: `None` is the theme's own (the family
/// the reader chose stays).
fn set_font_size(app: &App, size: Option<f64>) {
    let (theme_id, chosen, ..) = font(app);
    let family = chosen.map(|c| c.family).unwrap_or_default();
    let next = (size.is_some() || !family.is_empty()).then_some(FontOverride { family, size });
    app.engine.borrow_mut().set_font_override(&theme_id, next);
}

fn appearance() -> Vec<Setting> {
    vec![
        Setting::stored_toggle(
            e(
                "nav-rail",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: The icon strip on the far left: Chats, Updates, Teams and Settings.
                en::tr!("Activity bar"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Activity bar” saying what it does
                en::tr!("The icon strip on the far left: Chats, Updates, Teams and Settings."),
                shown!["navigation rail", "nav rail", "rail", "left bar", "left side", "icons", "icon bar", "activity rail", "sidebar icons", "destinations"],
            ),
            "APPEARANCE",
            "appearance/navRail",
            true,
        )
        .then(|app, window| window.set_nav_rail_visible(flag(app, "appearance/navRail", true))),
        Setting::toggle(
            keyed(e(
                "explorer",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: The list of agents and chats beside the conversation.
                en::tr!("Explorer"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Explorer” saying what it does
                en::tr!("The list of agents and chats beside the conversation."),
                shown!["sidebar", "side bar", "side panel", "panel", "agents list", "chat list", "left panel"],
            ), &["sidebar"]),
            "APPEARANCE",
            |app| app.window.upgrade().is_some_and(|w| w.get_sidebar_visible()),
            |app, window, on| {
                if window.get_sidebar_visible() != on && !crate::commands::run(app, window, "sidebar") {
                    eprintln!("clarp-slint: the explorer could not change");
                }
            },
        )
        .default("on"),
        Setting::stored_toggle(
            keyed(e(
                "compact-explorer",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Shows only each chat's avatar and name in the explorer.
                en::tr!("Compact explorer"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Compact explorer” saying what it does
                en::tr!("Shows only each chat's avatar and name in the explorer."),
                &["dense", "condensed", "small rows", "avatar only", "sidebar", "toggle", "density"],
            ), &["toggle-compact"]),
            "APPEARANCE",
            "explorer/compact",
            false,
        )
        .then(|app, window| {
            window.set_explorer_compact(flag(app, "explorer/compact", false));
            crate::avatar_view::remake();
        }),
        Setting::choice(
            e(
                "avatar-size",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: How large agents' portraits are in the explorer; opens a list of sizes.
                en::tr!("Agent picture size"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Agent picture size” saying what it does
                en::tr!("How large agents' portraits are in the explorer; opens a list of sizes."),
                &["avatar", "avatars", "portrait", "portraits", "profile picture", "photo", "face", "picture size", "bigger", "smaller"],
            ),
            "APPEARANCE",
            |_| {
                crate::avatar_view::CHOICES
                    .iter()
                    .map(|(id, label, sizes)| {
                        // i18n: Settings > Agent picture size, an option; {0} is the size's name (Small, Medium, Large), {1} its size in pixels
                        let shown = tr!("{0} · {1} px", avatar_size_name(id, label), sizes.row);
                        // i18n: Settings > Agent picture size, the line under an option; {0} and {1} are sizes in pixels in the normal and the compact explorer
                        Choice::new(*id, shown).about(tr!("{0} px in the explorer, {1} px compact", sizes.row, sizes.compact))
                    })
                    .collect()
            },
            |app| crate::avatar_view::current(app).to_owned(),
            |app, window, value| crate::avatar_view::set(app, window, Some(value), 0),
        )
        .default("medium"),
        Setting::stored_toggle(
            keyed(e(
                "live-preview",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Opens the chat under the explorer's cursor while you move through the list.
                en::tr!("Explorer live preview"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Explorer live preview” saying what it does
                en::tr!("Opens the chat under the explorer's cursor while you move through the list."),
                &["preview", "peek", "follow cursor", "sidebar", "toggle", "browse"],
            ), &["toggle-preview"]),
            "APPEARANCE",
            "explorer/livePreview",
            false,
        ),
        Setting::toggle(
            keyed(e(
                "workspace-bar",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: The strip of workspace tabs along the top.
                en::tr!("Workspace bar"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Workspace bar” saying what it does
                en::tr!("The strip of workspace tabs along the top."),
                shown!["workspaces", "tabs", "tab bar", "top bar", "strip"],
            ), &["setting:workspaceBarVisible"]),
            "APPEARANCE",
            |app| app.prefs.borrow().workspace_bar,
            |app, _, on| preference(app, "appearance/workspaceBar", on, false),
        )
        .default("on"),
        Setting::stored_toggle(
            keyed(e(
                "shortcut-bar",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: The key hints along the bottom of the window.
                en::tr!("Shortcut bar"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Shortcut bar” saying what it does
                en::tr!("The key hints along the bottom of the window."),
                shown!["keybindings", "key bindings", "hints", "status bar", "bottom bar", "shortcuts", "footer"],
            ), &["shortcut-bar"]),
            "APPEARANCE",
            "appearance/shortcutsVisible",
            true,
        )
        .then(|app, window| window.set_shortcuts_visible(flag(app, "appearance/shortcutsVisible", true))),
        Setting::stored_toggle(
            e(
                "minimal-ui",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Hides chips, switches and other chrome for a quieter window.
                en::tr!("Minimal UI"),
                "layout",
                // i18n: Settings page and Ctrl+K, the line under the setting “Minimal UI” saying what it does
                en::tr!("Hides chips, switches and other chrome for a quieter window."),
                shown!["zen", "focus mode", "distraction free", "clean", "simple", "chrome", "declutter"],
            ),
            "APPEARANCE",
            "appearance/minimalUi",
            false,
        )
        .then(|app, window| window.set_minimal_ui(flag(app, "appearance/minimalUi", false))),
        Setting::choice(
            e(
                "reading-theme",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: The colours and typeface of the whole window; opens a list to pick one.
                en::tr!("Reading theme"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Reading theme” saying what it does
                en::tr!("The colours and typeface of the whole window; opens a list to pick one."),
                &["theme", "colours", "colors", "color scheme", "colour scheme", "dark mode", "light mode", "font", "typeface", "appearance", "skin", "contrast"],
            ),
            "APPEARANCE",
            |_| {
                reading_theme::themes()
                    .iter()
                    .map(|theme| {
                        let text = |key: &str| theme.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
                        Choice::new(text("id"), format!("{} · {}", reading_theme::local(&text("label")), crate::view::theme_font(theme))).about(reading_theme::local(&text("detail")))
                    })
                    .collect()
            },
            |app| app.engine.borrow().reading_theme(),
            |app, _, value| app.engine.borrow_mut().set_reading_theme(value),
        )
        .default(reading_theme::default_theme_id()),
        // The picker shows the font in the chat, so it opens over the chats.
        Setting::action(
            e(
                "font",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (an action row that opens or does something). What it does: The chat's typeface and size for this reading theme; opens the font picker.
                en::tr!("Font"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Font” saying what it does
                en::tr!("The chat's typeface and size for this reading theme; opens the font picker."),
                &["typeface", "font family", "monospace", "serif", "sans", "choose font"],
            ),
            "APPEARANCE",
            |app, window| crate::font_view::open(app, window),
        )
        .detail(|app| {
            let (_, chosen, font, _) = font(app);
            match (&chosen, &font.missing) {
                // i18n: Settings > Font, the row's value when the chosen font is missing; {0} the missing font, {1} the font used instead, {2} the size in pixels
                (_, Some(missing)) => tr!("{0} is not installed · using {1} · {2} px", missing, font.family, font.size),
                // i18n: Settings > Font, the row's value for a font the user chose; {0} the font, {1} its size in pixels
                (Some(_), None) => tr!("{0} · {1} px · your choice", font.family, font.size),
                // i18n: Settings > Font, the row's value for the reading theme's own font; {0} the font, {1} its size in pixels
                (None, None) => tr!("{0} · {1} px · theme default", font.family, font.size),
            }
        }),
        // From the theme's own size, + is one pixel larger; Delete goes back to it.
        Setting::number(
            e(
                "font-size",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a number setting). What it does: How large the chat's text is for this reading theme; opens a list of sizes.
                en::tr!("Font size"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Font size” saying what it does
                en::tr!("How large the chat's text is for this reading theme; opens a list of sizes."),
                &["text size", "font size", "bigger text", "smaller text", "larger text", "type size", "letters", "reading size", "zoom text"],
            ),
            "APPEARANCE",
            |app| {
                let (_, chosen, _, theme_size) = font(app);
                chosen.and_then(|c| c.size).unwrap_or(theme_size)
            },
            |app, _, size| set_font_size(app, Some(size)),
            FONT_SIZE_MIN,
            FONT_SIZE_MAX,
            1.0,
            "px",
        )
        .detail(|app| {
            let (_, chosen, _, theme_size) = font(app);
            match chosen.and_then(|c| c.size) {
                Some(size) => super::shown_number(size, 1.0, "px"),
                // i18n: Settings > Font size, the row's value when it is the reading theme's own size; {} is the size in pixels
                None => tr!("{} px · theme default", theme_size),
            }
        })
        .reset_with(|app, _| set_font_size(app, None)),
        Setting::action(
            e(
                "reset-font",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (an action row that opens or does something). What it does: Goes back to the reading theme's own typeface and size.
                en::tr!("Reset font to theme default"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Reset font to theme default” saying what it does
                en::tr!("Goes back to the reading theme's own typeface and size."),
                &["font", "typeface", "revert", "default font", "undo font", "restore"],
            ),
            "APPEARANCE",
            |app, _| {
                let theme = app.engine.borrow().reading_theme();
                app.engine.borrow_mut().set_font_override(&theme, None);
            },
        )
        .detail(|app| {
            let (theme_id, chosen, _, theme_size) = font(app);
            let theme = reading_theme::theme(&theme_id);
            if chosen.is_some() {
                // i18n: Settings > Reset font to theme default, the row's value: what the reset goes back to; {0} the theme's font, {1} its size in pixels
                tr!("Back to {0} · {1} px", crate::view::theme_font(theme), theme_size)
            } else {
                // i18n: Settings > Reset font to theme default, the row's value when nothing is changed
                tr!("Already the theme default")
            }
        }),
        // The whole window's scale (the chats with it): a number from where
        // it is, Ctrl+Alt+PageUp / PageDown a step, kept as a preference.
        Setting::number(
            e(
                "ui-scale",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a number setting). What it does: How large the whole interface is drawn; opens a list of sizes.
                en::tr!("Interface size"),
                "appearance",
                // i18n: Settings page and Ctrl+K, the line under the setting “Interface size” saying what it does
                en::tr!("How large the whole interface is drawn; opens a list of sizes."),
                &["zoom", "scale", "size", "bigger", "smaller", "larger", "dpi", "everything bigger"],
            ),
            "APPEARANCE",
            |app| clarp_core::prefs::number_of(app.engine.borrow().settings(), "uiscale"),
            |app, window, scale| crate::look::set_pref(app, window, "uiscale", &super::number_text(scale, 0.05)),
            0.5,
            3.0,
            0.05,
            "",
        )
        .detail(|app| super::shown_number((clarp_core::prefs::number_of(app.engine.borrow().settings(), "uiscale") * 100.0).round(), 1.0, "%"))
        .default("1.15"),
    ]
}

/// The voice providers the Host offers, as (id, label); `with_none` adds
/// "No fallback" first.
fn providers(tts: &clarp_core::json::Object, with_none: bool) -> Vec<Choice> {
    // i18n: Settings > Voice fallback, the first option: no second voice when the main one fails
    let mut out = if with_none { vec![Choice::new("none", tr!("No fallback"))] } else { Vec::new() };
    for provider in tts.get("providers").and_then(Value::as_array).into_iter().flatten() {
        let text = |key: &str| provider.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
        let id = text("id");
        if id.is_empty() {
            continue;
        }
        let label = [text("name"), text("label")].into_iter().find(|l| !l.is_empty()).unwrap_or_else(|| id.clone());
        out.push(Choice::new(id, label));
    }
    out
}

fn tts(app: &App, key: &str) -> String {
    app.engine.borrow().tts_provider_status().get(key).and_then(Value::as_str).unwrap_or_default().to_owned()
}

fn voice() -> Vec<Setting> {
    vec![
        Setting::toggle(
            keyed(e(
                "spoken-replies",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Reads agents' replies aloud.
                en::tr!("Spoken replies"),
                "audio",
                // i18n: Settings page and Ctrl+K, the line under the setting “Spoken replies” saying what it does
                en::tr!("Reads agents' replies aloud."),
                &["voice", "mute", "unmute", "speech", "tts", "audio", "sound", "read aloud", "voice replies", "toggle"],
            ), &["mute"]),
            "VOICE & AUDIO",
            |app| !app.engine.borrow().muted(),
            |app, _, on| app.engine.borrow_mut().set_muted(!on),
        ),
        Setting::choice(
            e(
                "voice-provider",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: The Host's text-to-speech service; opens a list to pick one.
                en::tr!("Voice provider"),
                "audio",
                // i18n: Settings page and Ctrl+K, the line under the setting “Voice provider” saying what it does
                en::tr!("The Host's text-to-speech service; opens a list to pick one."),
                &["tts", "speech", "voice", "elevenlabs", "kokoro", "speaker"],
            ),
            "VOICE & AUDIO",
            |app| providers(app.engine.borrow().tts_provider_status(), false),
            |app| tts(app, "provider"),
            |app, _, value| {
                let fallback = tts(app, "fallback");
                app.engine.borrow_mut().set_tts_providers(value, &fallback, "");
            },
        )
        .detail(|app| {
            let now = tts(app, "provider");
            let label = providers(app.engine.borrow().tts_provider_status(), false).into_iter().find(|c| c.value == now).map(|c| c.label);
            // i18n: Settings > Voice provider, the row's value when the Host has not said which provider it uses
            label.unwrap_or_else(|| if now.is_empty() { tr!("Unknown") } else { now })
        }),
        Setting::choice(
            e(
                "voice-fallback",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: The voice the Host uses when the main provider fails; opens a list.
                en::tr!("Voice fallback"),
                "audio",
                // i18n: Settings page and Ctrl+K, the line under the setting “Voice fallback” saying what it does
                en::tr!("The voice the Host uses when the main provider fails; opens a list."),
                &["backup voice", "tts fallback", "secondary", "speech", "voice"],
            ),
            "VOICE & AUDIO",
            |app| providers(app.engine.borrow().tts_provider_status(), true),
            |app| Some(tts(app, "fallback")).filter(|f| !f.is_empty()).unwrap_or_else(|| "none".into()),
            |app, _, value| {
                let provider = tts(app, "provider");
                app.engine.borrow_mut().set_tts_providers(&provider, value, "");
            },
        ),
    ]
}

/// The NOTIFICATIONS section: the Host's switch per event (Host feature
/// `notification_events`), the owner's choice for every contact kept on
/// the Host so every device agrees, listed only while the Host keeps them
/// and names the event; then this computer's popups, their sound and
/// pausing the phone while here.
fn notifications() -> Vec<Setting> {
    use clarp_core::notify_events::{self, EVENT_IDS, event_about, event_label, text};
    // The registry's words (Ctrl+K's search) are made once; the page says
    // each in the app's language when it draws it.
    let once = |words: String| -> &'static str { Box::leak(words.into_boxed_str()) };
    EVENT_IDS
        .iter()
        .map(|&id| {
            let on = move |app: &App| app.engine.borrow().notification_settings().and_then(|s| s.event(id)).is_some_and(|e| e.on);
            Setting::toggle(
                Entry { id: event_setting_id(id), label: once(event_label(id)), description: once(event_about(id)), aliases: EVENT_ALIASES, group: "notifications", actions: &[] },
                "NOTIFICATIONS",
                on,
                move |app, _, on| app.engine.borrow_mut().set_notification_event(id, on),
            )
            .when(move |app| app.engine.borrow().notification_settings().is_some_and(|s| s.event(id).is_some()))
            .label_from(move |_| event_label(id))
            .describe_from(move |_| event_about(id))
        })
        .chain([
            // This computer's own switches, after the Host's.
            Setting::stored_toggle(
                e("desktop-notifications", notify_events::DESKTOP_LABEL, "notifications", notify_events::DESKTOP_ABOUT, &["notifications", "alerts", "notify", "popups", "toasts", "reply notifications", "toggle"]),
                "NOTIFICATIONS",
                "notifications/replies",
                true,
            )
            .label_from(|_| text("notify.settings.desktop"))
            .describe_from(|_| text("notify.settings.desktop.detail")),
            Setting::stored_toggle(
                e("notification-sound", notify_events::SOUND_LABEL, "notifications", notify_events::SOUND_ABOUT, &["sounds", "chime", "ding", "audio alert", "beep", "notification sound", "notifications", "toggle"]),
                "NOTIFICATIONS",
                "notifications/sound",
                false,
            )
            .label_from(|_| text("notify.settings.sound"))
            .describe_from(|_| text("notify.settings.sound.detail")),
            Setting::stored_toggle(
                e(
                    "pause-mobile-push",
                    // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Holds phone notifications while you are using this window.
                    en::tr!("Pause phone alerts while active on desktop"),
                    "notifications",
                    // i18n: Settings page and Ctrl+K, the line under the setting “Pause phone alerts while active on desktop” saying what it does
                    en::tr!("Holds phone notifications while you are using this window."),
                    &["notifications", "notification", "push", "phone", "mobile", "alerts", "quiet", "do not disturb", "toggle"],
                ),
                "NOTIFICATIONS",
                "notifications/pauseMobileWhileDesktopActive",
                true,
            ),
        ])
        .collect()
}

/// Words every event switch answers to.
const EVENT_ALIASES: &[&str] = &["notifications", "notify", "alerts", "popups", "push", "per event"];

/// The catalogue id of an event's switch (`notify-reply`).
fn event_setting_id(id: &str) -> &'static str {
    match id {
        "reply" => "notify-reply",
        "needs_you" => "notify-needs-you",
        "failed" => "notify-failed",
        "update" => "notify-update",
        "job_done" => "notify-job-done",
        "helper_report" => "notify-helper-report",
        "call" => "notify-call",
        "account" => "notify-account",
        _ => "notify-event",
    }
}

fn run_command(action: &'static str) -> impl Fn(&Rc<App>, &AppWindow) {
    move |app, window| {
        if !crate::commands::run(app, window, action) {
            eprintln!("clarp-slint: {action} is not available yet");
        }
    }
}

fn host() -> Vec<Setting> {
    vec![
        Setting::action(
            e(
                "connection",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (an action row that opens or does something). What it does: The Clarp Host this window talks to, and its address and state.
                en::tr!("Host connection"),
                "host",
                // i18n: Settings page and Ctrl+K, the line under the setting “Host connection” saying what it does
                en::tr!("The Clarp Host this window talks to, and its address and state."),
                &["host", "server", "connect", "url", "address", "status"],
            ),
            "HOST",
            run_command("connection"),
        )
        .label_from(|app| {
            let engine = app.engine.borrow();
            if engine.server_name().is_empty() { "Clarp Host".to_owned() } else { engine.server_name().to_owned() }
        })
        .detail(|app| {
            let engine = app.engine.borrow();
            format!("{}  ·  {}", engine.base_url(), engine.connection_state())
        }),
        Setting::action(
            e(
                "orchestrator",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (an action row that opens or does something). What it does: The orchestrator that routes your messages to agents.
                en::tr!("Orchestrator settings"),
                "host",
                // i18n: Settings page and Ctrl+K, the line under the setting “Orchestrator settings” saying what it does
                en::tr!("The orchestrator that routes your messages to agents."),
                &["orchestrator", "routing", "router", "dispatch", "hands-free"],
            ),
            "HOST",
            run_command("orchestrator"),
        )
        // i18n: Settings > Host, the orchestrator row's label (noun): the orchestrator that routes messages to agents
        .label_from(|_| tr!("Orchestrator"))
        // i18n: Settings > Host, the orchestrator row's value, a button-like word (verb): opens the orchestrator settings
        .detail(|_| tr!("verb" => "Open")),
        Setting::toggle(
            e(
                "shared-filesystem",
                // i18n: Settings page and Ctrl+K, a setting's name, the row label (a switch). What it does: Lets the app open the Host's files directly (a trusted Host on this machine).
                en::tr!("Shared filesystem"),
                "host",
                // i18n: Settings page and Ctrl+K, the line under the setting “Shared filesystem” saying what it does
                en::tr!("Lets the app open the Host's files directly (a trusted Host on this machine)."),
                &["files", "local folders", "filesystem", "trusted host", "disk", "toggle"],
            ),
            "HOST",
            |app| app.engine.borrow().shared_filesystem(),
            |app, _, on| app.engine.borrow_mut().set_shared_filesystem(on),
        ),
    ]
}

fn keyboard() -> Vec<Setting> {
    vec![Setting::choice(
        e(
            "double-press",
            // i18n: Settings page and Ctrl+K, a setting's name, the row label (a choice that opens a list). What it does: How quickly the second press of a double press (Right Right) must follow; opens a list.
            en::tr!("Double-press window"),
            "keyboard",
            // i18n: Settings page and Ctrl+K, the line under the setting “Double-press window” saying what it does
            en::tr!("How quickly the second press of a double press (Right Right) must follow; opens a list."),
            &["double press", "double tap", "double click", "key timing", "delay", "speed", "milliseconds"],
        ),
        "KEYBOARD",
        |_| {
            DOUBLE_PRESS
                .iter()
                .map(|ms| {
                    // i18n: Settings > Double-press window, the line beside the usual value (300 ms)
                    let about = if *ms == 300 { tr!("The usual") } else { String::new() };
                    Choice::new(ms.to_string(), super::shown_number(*ms as f64, 1.0, "ms")).about(about)
                })
                .collect()
        },
        |app| {
            let setting = app.engine.borrow().settings().get("keymap/doublePressMs").and_then(Value::as_i64);
            crate::keymap::double_press_window(setting).as_millis().to_string()
        },
        |app, _, value| match value.parse::<i64>() {
            Ok(ms) => store(app, "keymap/doublePressMs", ms),
            Err(error) => eprintln!("clarp-slint: not a double-press window {value}: {error}"),
        },
    )
    .default("300")]
}
