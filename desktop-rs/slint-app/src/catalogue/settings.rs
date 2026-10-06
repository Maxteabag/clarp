//! The app's settings, registered once: the settings page lists them in
//! this order within their sections, and Ctrl+K reaches every one. A new
//! group of settings can live in a module of its own, added to `register`.

use std::rc::Rc;

use clarp_core::reading_theme::{self, FontOverride};
use clarp_engine::Change;
use serde_json::Value;

use super::{Choice, Setting, e, keyed, shown};
use crate::{App, AppWindow};

const ACTIVITY: [(&str, &str); 3] = [
    ("Grouped", "Tool calls fold into one row per turn."),
    ("Always visible", "Every tool call shows as its own row."),
    ("Group old", "Recent tool calls show; older ones fold."),
];
/// The interface sizes offered, as scale factors (1.15 is the usual).
const SCALES: [f64; 9] = [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4];
/// The double-press windows offered, in milliseconds.
const DOUBLE_PRESS: [u64; 8] = [150, 200, 250, 300, 400, 500, 700, 1000];
/// The chat's font sizes offered, in pixels.
const FONT_SIZES: [u32; 13] = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 24, 28];

pub(super) fn register() -> Vec<Setting> {
    let mut all = Vec::new();
    all.extend(chats());
    all.extend(experiments());
    all.extend(startup());
    all.extend(appearance());
    all.extend(voice());
    all.extend(host());
    all.extend(keyboard());
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
            keyed(e("timestamps", "Timestamps", "chats", "Shows the time beside each message.", shown!["time", "date", "clock", "message times", "stamps"]), &["setting:timestampsVisible"]),
            "CHATS",
            |app| app.prefs.borrow().timestamps,
            |app, _, on| preference(app, "conversation/timestampsVisible", on, true),
        )
        .default("off"),
        Setting::toggle(
            e("show-when-ready", "Show when ready", "chats", "Shows a reply once it is complete instead of while it streams in.", &["stream", "streaming", "typing", "live text", "wait for answer", "toggle"]),
            "CHATS",
            |app| app.engine.borrow().show_when_ready(),
            |app, _, on| app.engine.borrow_mut().set_show_when_ready(on),
        ),
        Setting::stored_toggle(
            e("reduced-motion", "Reduce Motion", "appearance", "Turns off animations and smooth scrolling, the working row's shimmer among them.", &["animations", "animation", "motion", "accessibility", "reduce animations", "toggle", "still", "shimmer"]),
            "CHATS",
            "appearance/reducedMotion",
            false,
        ),
        Setting::choice(
            keyed(
                e("activity", "Tool activity", "chats", "How an agent's tool calls show in the chat: grouped, always visible or old ones grouped.", &["tool calls", "tools", "collapse", "expand", "grouping", "group tools", "activity"]),
                &["tools"],
            ),
            "CHATS",
            |_| ACTIVITY.iter().enumerate().map(|(i, (label, about))| Choice::new(i.to_string(), *label).about(*about)).collect(),
            |app| app.engine.borrow().activity_mode().clamp(0, 2).to_string(),
            |app, _, value| {
                if let Ok(mode) = value.parse() {
                    app.engine.borrow_mut().set_activity_mode(mode);
                }
            },
        )
        .default("0"),
        // Off, the working row shimmers in the chat and the shortcut bar
        // names the stop key.
        Setting::stored_toggle(
            e(
                "live-status-line",
                "Live status line",
                "chats",
                "Shows the line between the chat and the composer with the agent's current step, how long it has run and the Ctrl+. stop key. Off, the working row shimmers in the chat and the shortcut bar names Stop.",
                &["status line", "stop hint", "activity bar under chat", "working indicator", "live status", "current tool", "progress line", "elapsed time"],
            ),
            "CHATS",
            crate::profile_view::LIVE_STATUS_LINE,
            false,
        ),
        // The Host's setting, on Hosts that send live items (§6).
        Setting::toggle(
            keyed(
                e("tool-explanations", "Tool explanations (Host)", "chats", "The Host's plain-language explanation of each tool row.", &["explain", "explanations", "plain english", "narration", "tool labels", "toggle"]),
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
        Setting::toggle(
            keyed(e("narration", "Plain-English tools", "experiment", "Describes tool calls in plain English with Spark (uses extra AI).", &["narration", "narrator", "spark", "explain tools", "plain english", "toggle"]), &["tool-narration"]),
            "EXPERIMENTS",
            |app| app.engine.borrow().narrator_enabled(),
            |app, _, on| app.engine.borrow_mut().set_narrator_enabled(on),
        ),
        Setting::choice(
            e("tool-detail", "Tool detail", "experiment", "How much the plain-English tool descriptions say; opens a list of levels.", &["detail level", "explanation", "verbosity", "narration", "audience", "explanations"]),
            "EXPERIMENTS",
            |_| {
                clarp_engine::Engine::narrator_detail_levels()
                    .iter()
                    .enumerate()
                    .map(|(i, name)| Choice::new(i.to_string(), *name).about(if i == 0 { "No AI" } else { "Uses AI" }))
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

fn startup() -> Vec<Setting> {
    vec![
        Setting::stored_toggle(
            e("new-agent-on-startup", "Start a new agent when opening Clarp", "startup", "Opens the new-agent hub each time Clarp starts.", &["startup", "launch", "on open", "new session", "boot", "toggle"]),
            "STARTUP",
            "launch/newAgentOnStartup",
            false,
        ),
        Setting::stored_toggle(
            e("anonymous-agents", "Anonymous agents by default", "agent", "New agents start without a contact's name and persona.", &["anonymous", "nameless", "persona", "identity", "contact", "toggle"]),
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

fn ui_scale(app: &App) -> f64 {
    app.engine.borrow().settings().get("appearance/uiScale").and_then(Value::as_f64).unwrap_or(1.15)
}

fn appearance() -> Vec<Setting> {
    vec![
        Setting::stored_toggle(
            e("nav-rail", "Activity bar", "layout", "The icon strip on the far left: Chats, Updates, Teams and Settings.", shown!["navigation rail", "nav rail", "rail", "left bar", "left side", "icons", "icon bar", "activity rail", "sidebar icons", "destinations"]),
            "APPEARANCE",
            "appearance/navRail",
            true,
        )
        .then(|app, window| window.set_nav_rail_visible(flag(app, "appearance/navRail", true))),
        Setting::toggle(
            keyed(e("explorer", "Explorer", "layout", "The list of agents and chats beside the conversation.", shown!["sidebar", "side bar", "side panel", "panel", "agents list", "chat list", "left panel"]), &["sidebar"]),
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
            keyed(e("compact-explorer", "Compact explorer", "layout", "Shows only each chat's avatar and name in the explorer.", &["dense", "condensed", "small rows", "avatar only", "sidebar", "toggle", "density"]), &["toggle-compact"]),
            "APPEARANCE",
            "explorer/compact",
            false,
        )
        .then(|app, window| {
            window.set_explorer_compact(flag(app, "explorer/compact", false));
            crate::avatar_view::remake();
        }),
        Setting::choice(
            e("avatar-size", "Agent picture size", "layout", "How large agents' portraits are in the explorer; opens a list of sizes.", &["avatar", "avatars", "portrait", "portraits", "profile picture", "photo", "face", "picture size", "bigger", "smaller"]),
            "APPEARANCE",
            |_| {
                crate::avatar_view::CHOICES
                    .iter()
                    .map(|(id, label, sizes)| Choice::new(*id, format!("{label} · {} px", sizes.row)).about(format!("{} px in the explorer, {} px compact", sizes.row, sizes.compact)))
                    .collect()
            },
            |app| crate::avatar_view::current(app).to_owned(),
            |app, window, value| crate::avatar_view::set(app, window, Some(value), 0),
        )
        .default("medium"),
        Setting::stored_toggle(
            keyed(e("live-preview", "Explorer live preview", "layout", "Opens the chat under the explorer's cursor while you move through the list.", &["preview", "peek", "follow cursor", "sidebar", "toggle", "browse"]), &["toggle-preview"]),
            "APPEARANCE",
            "explorer/livePreview",
            false,
        ),
        Setting::toggle(
            keyed(e("workspace-bar", "Workspace bar", "layout", "The strip of workspace tabs along the top.", shown!["workspaces", "tabs", "tab bar", "top bar", "strip"]), &["setting:workspaceBarVisible"]),
            "APPEARANCE",
            |app| app.prefs.borrow().workspace_bar,
            |app, _, on| preference(app, "appearance/workspaceBar", on, false),
        )
        .default("on"),
        Setting::stored_toggle(
            keyed(e("shortcut-bar", "Shortcut bar", "layout", "The key hints along the bottom of the window.", shown!["keybindings", "key bindings", "hints", "status bar", "bottom bar", "shortcuts", "footer"]), &["shortcut-bar"]),
            "APPEARANCE",
            "appearance/shortcutsVisible",
            true,
        )
        .then(|app, window| window.set_shortcuts_visible(flag(app, "appearance/shortcutsVisible", true))),
        Setting::stored_toggle(
            e("minimal-ui", "Minimal UI", "layout", "Hides chips, switches and other chrome for a quieter window.", shown!["zen", "focus mode", "distraction free", "clean", "simple", "chrome", "declutter"]),
            "APPEARANCE",
            "appearance/minimalUi",
            false,
        )
        .then(|app, window| window.set_minimal_ui(flag(app, "appearance/minimalUi", false))),
        Setting::choice(
            e("reading-theme", "Reading theme", "appearance", "The colours and typeface of the whole window; opens a list to pick one.", &["theme", "colours", "colors", "color scheme", "colour scheme", "dark mode", "light mode", "font", "typeface", "appearance", "skin", "contrast"]),
            "APPEARANCE",
            |_| {
                reading_theme::themes()
                    .iter()
                    .map(|theme| {
                        let text = |key: &str| theme.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
                        Choice::new(text("id"), format!("{} · {}", text("label"), crate::view::theme_font(theme))).about(text("detail"))
                    })
                    .collect()
            },
            |app| app.engine.borrow().reading_theme(),
            |app, _, value| app.engine.borrow_mut().set_reading_theme(value),
        )
        .default(reading_theme::default_theme_id()),
        // The picker shows the font in the chat, so it opens over the chats.
        Setting::action(
            e("font", "Font", "appearance", "The chat's typeface and size for this reading theme; opens the font picker.", &["typeface", "font family", "monospace", "serif", "sans", "choose font"]),
            "APPEARANCE",
            |app, window| crate::font_view::open(app, window),
        )
        .detail(|app| {
            let (_, chosen, font, _) = font(app);
            match (&chosen, &font.missing) {
                (_, Some(missing)) => format!("{missing} is not installed · using {} · {} px", font.family, font.size),
                (Some(_), None) => format!("{} · {} px · your choice", font.family, font.size),
                (None, None) => format!("{} · {} px · theme default", font.family, font.size),
            }
        }),
        Setting::choice(
            e("font-size", "Font size", "appearance", "How large the chat's text is for this reading theme; opens a list of sizes.", &["text size", "font size", "bigger text", "smaller text", "larger text", "type size", "letters", "reading size", "zoom text"]),
            "APPEARANCE",
            |app| {
                let (.., theme_size) = font(app);
                std::iter::once(Choice::new("", format!("Theme default · {theme_size} px")).about("The reading theme's own size"))
                    .chain(FONT_SIZES.iter().map(|px| Choice::new(px.to_string(), format!("{px} px"))))
                    .collect()
            },
            |app| {
                let (_, chosen, ..) = font(app);
                chosen.and_then(|c| c.size).map_or_else(String::new, |size| (size.round() as u32).to_string())
            },
            |app, _, value| {
                let (theme_id, chosen, ..) = font(app);
                let family = chosen.map(|c| c.family).unwrap_or_default();
                let size = value.parse::<f64>().ok();
                let next = (size.is_some() || !family.is_empty()).then_some(FontOverride { family, size });
                app.engine.borrow_mut().set_font_override(&theme_id, next);
            },
        )
        .default(""),
        Setting::action(
            e("reset-font", "Reset font to theme default", "appearance", "Goes back to the reading theme's own typeface and size.", &["font", "typeface", "revert", "default font", "undo font", "restore"]),
            "APPEARANCE",
            |app, _| {
                let theme = app.engine.borrow().reading_theme();
                app.engine.borrow_mut().set_font_override(&theme, None);
            },
        )
        .detail(|app| {
            let (theme_id, chosen, _, theme_size) = font(app);
            let theme = reading_theme::theme(&theme_id);
            if chosen.is_some() { format!("Back to {} · {theme_size} px", crate::view::theme_font(theme)) } else { "Already the theme default".to_owned() }
        }),
        Setting::choice(
            e("ui-scale", "Interface size", "appearance", "How large the whole interface is drawn; opens a list of sizes.", &["zoom", "scale", "size", "bigger", "smaller", "larger", "dpi", "everything bigger"]),
            "APPEARANCE",
            |_| SCALES.iter().map(|scale| Choice::new(format!("{scale:.2}"), format!("{}%", (scale * 100.0).round())).about(if (*scale - 1.15).abs() < 0.001 { "The usual size" } else { "" })).collect(),
            |app| format!("{:.2}", ui_scale(app)),
            |app, _, value| match value.parse::<f64>() {
                Ok(scale) => {
                    store(app, "appearance/uiScale", scale);
                    crate::platform::desktop::set_ui_scale(scale as f32);
                }
                Err(error) => eprintln!("clarp-slint: not an interface size {value}: {error}"),
            },
        )
        .default("1.15"),
    ]
}

/// The voice providers the Host offers, as (id, label); `with_none` adds
/// "No fallback" first.
fn providers(tts: &clarp_core::json::Object, with_none: bool) -> Vec<Choice> {
    let mut out = if with_none { vec![Choice::new("none", "No fallback")] } else { Vec::new() };
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
            keyed(e("spoken-replies", "Spoken replies", "audio", "Reads agents' replies aloud.", &["voice", "mute", "unmute", "speech", "tts", "audio", "sound", "read aloud", "voice replies", "toggle"]), &["mute"]),
            "VOICE & AUDIO",
            |app| !app.engine.borrow().muted(),
            |app, _, on| app.engine.borrow_mut().set_muted(!on),
        ),
        Setting::choice(
            e("voice-provider", "Voice provider", "audio", "The Host's text-to-speech service; opens a list to pick one.", &["tts", "speech", "voice", "elevenlabs", "kokoro", "speaker"]),
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
            label.unwrap_or_else(|| if now.is_empty() { "Unknown".into() } else { now })
        }),
        Setting::choice(
            e("voice-fallback", "Voice fallback", "audio", "The voice the Host uses when the main provider fails; opens a list.", &["backup voice", "tts fallback", "secondary", "speech", "voice"]),
            "VOICE & AUDIO",
            |app| providers(app.engine.borrow().tts_provider_status(), true),
            |app| Some(tts(app, "fallback")).filter(|f| !f.is_empty()).unwrap_or_else(|| "none".into()),
            |app, _, value| {
                let provider = tts(app, "provider");
                app.engine.borrow_mut().set_tts_providers(&provider, value, "");
            },
        ),
        Setting::stored_toggle(
            e("pause-mobile-push", "Pause phone alerts while active on desktop", "notifications", "Holds phone notifications while you are using this window.", &["notifications", "notification", "push", "phone", "mobile", "alerts", "quiet", "do not disturb", "toggle"]),
            "NOTIFICATIONS",
            "notifications/pauseMobileWhileDesktopActive",
            true,
        ),
    ]
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
            e("connection", "Host connection", "host", "The Clarp Host this window talks to, and its address and state.", &["host", "server", "connect", "url", "address", "status"]),
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
            e("orchestrator", "Orchestrator settings", "host", "The orchestrator that routes your messages to agents.", &["orchestrator", "routing", "router", "dispatch", "hands-free"]),
            "HOST",
            run_command("orchestrator"),
        )
        .label_from(|_| "Orchestrator".to_owned())
        .detail(|_| "Open".to_owned()),
        Setting::toggle(
            e("shared-filesystem", "Shared filesystem", "host", "Lets the app open the Host's files directly (a trusted Host on this machine).", &["files", "local folders", "filesystem", "trusted host", "disk", "toggle"]),
            "HOST",
            |app| app.engine.borrow().shared_filesystem(),
            |app, _, on| app.engine.borrow_mut().set_shared_filesystem(on),
        ),
    ]
}

fn keyboard() -> Vec<Setting> {
    vec![Setting::choice(
        e("double-press", "Double-press window", "keyboard", "How quickly the second press of a double press (Right Right) must follow; opens a list.", &["double press", "double tap", "double click", "key timing", "delay", "speed", "milliseconds"]),
        "KEYBOARD",
        |_| DOUBLE_PRESS.iter().map(|ms| Choice::new(ms.to_string(), format!("{ms} ms")).about(if *ms == 300 { "The usual" } else { "" })).collect(),
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
