//! The agent profile (AgentProfilePanel.qml), and the
//! layering of this group's dialogs: the profile and the overview open the
//! voice and orchestrator dialogs over themselves, and closing one returns
//! to the dialog below it.

use std::cell::RefCell;
use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};

use clarp_core::transit::{self, Step};

use crate::{
    App, AppWindow, NamedItem, PlanItem, ProfileArtifact, ProfileBridge, ProfileInfo, PromptItem, TransitChoice, commands, pump_now,
};

/// The overlays this group owns.
const OWNED: &[&str] = &["profile", "overview", "voices", "orchestrator"];

#[derive(Default)]
struct State {
    session: String,
    /// The dialogs below the one shown, to return to.
    below: Vec<String>,
    /// The region that had the keyboard when the profile opened.
    origin: &'static str,
    model_ids: Vec<String>,
    effort_ids: Vec<String>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

fn text(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        _ => String::new(),
    }
}

fn first(values: &[String], fallback: &str) -> String {
    values.iter().find(|v| !v.is_empty()).cloned().unwrap_or_else(|| fallback.to_owned())
}

/// A named entry that may be a bare string or `{name}` (MCP servers).
fn name_of(value: &Value) -> String {
    value.as_str().map_or_else(|| text(value, "name"), str::to_owned)
}

/// Adds `current` (an id the catalog's `options` lack) at the end, named by
/// its id, so a picker can show it chosen.
fn with_current(options: &mut Vec<Value>, current: &str) {
    if !current.is_empty() && !options.iter().any(|o| text(o, "id") == current) {
        options.push(serde_json::json!({"id": current, "label": current}));
    }
}

// ---- dialog layers -----------------------------------------------------------

/// Shows `name`, keeping the dialog it opens over to return to.
pub fn show(app: &App, window: &AppWindow, name: &str) {
    let current = app.overlay.borrow().clone();
    STATE.with(|s| {
        let mut state = s.borrow_mut();
        if current.is_empty() || !OWNED.contains(&current.as_str()) {
            state.below.clear();
        } else if current != name {
            state.below.push(current);
        }
    });
    commands::open_overlay(app, window, name);
}

/// Closes the shown dialog, back to the one below it (if any).
pub fn close(app: &App, window: &AppWindow) {
    if app.overlay.borrow().as_str() == "profile" {
        let session = session();
        if !session.is_empty() && !window.global::<ProfileBridge>().get_transit_step().is_empty() {
            app.engine.borrow_mut().reset_transit(&session);
        }
        window.global::<ProfileBridge>().set_transit_step("".into());
    }
    let below = STATE.with(|s| s.borrow_mut().below.pop());
    match below {
        Some(name) => {
            commands::open_overlay(app, window, &name);
            // Back on the profile from its voices: its keys again.
            if name == "profile" {
                hold_keyboard(window);
            }
        }
        None => commands::close_overlay(app, window),
    }
    window.global::<ProfileBridge>().set_confirm("".into());
}

/// Whether the Backend picker is open: the keymap's "transit" context.
pub fn transit_open(app: &App, window: &AppWindow) -> bool {
    app.overlay.borrow().as_str() == "profile" && !window.global::<ProfileBridge>().get_transit_step().is_empty()
}

/// Up/Down in the Backend picker: to the next choice that can be chosen,
/// skipping dimmed ones; at either end it stays.
pub fn transit_move(window: &AppWindow, step: i32) {
    let bridge = window.global::<ProfileBridge>();
    let enabled: Vec<bool> = bridge.get_transit_choices().iter().map(|c| c.enabled).collect();
    let from = bridge.get_transit_cursor();
    let mut at = from;
    let next = loop {
        at += step;
        if at < 0 || at as usize >= enabled.len() {
            break if from >= 0 && (from as usize) < enabled.len() { from } else { enabled.iter().position(|e| *e).map_or(-1, |i| i as i32) };
        }
        if enabled[at as usize] {
            break at;
        }
    };
    bridge.set_transit_cursor(next);
}

/// Enter in the Backend picker: picks the highlighted choice, or moves on
/// the last step.
pub fn transit_enter(window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    if bridge.get_transit_step() == "confirm" {
        bridge.invoke_confirm_transit();
        return;
    }
    let cursor = bridge.get_transit_cursor();
    if let Some(choice) = (cursor >= 0).then(|| bridge.get_transit_choices().row_data(cursor as usize)).flatten().filter(|c| c.enabled) {
        bridge.invoke_pick_transit(choice.id);
    }
}

// ---- the keyboard in the profile ------------------------------------------------

/// The profile's sections by digit, as its cards' titles show them: a digit
/// puts the cursor on the section's first row. The ids are rows or cards
/// of `stops`.
pub const SECTIONS: &[(&str, &str)] = &[
    // Both open the one Publications overlay: everything, or its images.
    ("publications", "Publications"),
    ("images", "Images"),
    ("files", "Files"),
    ("terminal", "Terminal"),
    ("voice", "Voice"),
    ("plan", "Plan"),
    ("history", "History"),
    ("model", "Model"),
    ("backend", "Backend"),
];

/// A row the keyboard can be on: its id (the `Spot` that draws it in
/// profile.slint) and its card.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stop {
    pub id: String,
    pub card: &'static str,
}

/// What the profile shows that the keyboard can reach, top to bottom.
#[derive(Debug, Clone, Default)]
pub struct Shown {
    pub chat_view: bool,
    pub agent_messages: bool,
    pub tool_activity: bool,
    pub backend: bool,
    pub plan: bool,
    pub prompts_shown: bool,
    pub prompts: usize,
    pub prompts_more: bool,
    pub mcp: usize,
    pub schedules: usize,
}

/// Every row the keyboard walks, in the order the profile draws them.
pub fn stops(shown: &Shown) -> Vec<Stop> {
    let mut out = Vec::new();
    let mut add = |id: String, card: &'static str| out.push(Stop { id, card });
    for id in ["files", "terminal", "voice"] {
        add(id.into(), "chat");
    }
    for (on, id) in [(shown.chat_view, "chat-view"), (shown.agent_messages, "agent-messages"), (shown.tool_activity, "tool-activity")] {
        if on {
            add(id.into(), "chat");
        }
    }
    if shown.backend {
        add("backend".into(), "backend");
    }
    if shown.plan {
        add("plan".into(), "plan");
    }
    if shown.prompts_shown {
        for index in 0..shown.prompts {
            add(format!("prompt:{index}"), "history");
        }
        if shown.prompts_more {
            add("history-more".into(), "history");
        } else if shown.prompts == 0 {
            add("history".into(), "history");
        }
    }
    for id in ["model", "effort"] {
        add(id.into(), "model");
    }
    for id in ["heartbeat", "dreaming", "push-alerts"] {
        add(id.into(), "autonomy");
    }
    for index in 0..shown.mcp {
        add(format!("mcp:{index}"), "mcp");
    }
    add("compact".into(), "context");
    for id in ["publications", "images"] {
        add(id.into(), "publications");
    }
    add("queue".into(), "queue");
    for index in 0..shown.schedules {
        add(format!("schedule:{index}"), "schedules");
    }
    for id in ["relaunch", "release"] {
        add(id.into(), "actions");
    }
    out
}

/// Where `step` rows (or, with `by_card`, cards) from `cursor` lands: the
/// first row before the cursor has one; rows stop at the ends, cards go round.
pub fn moved(stops: &[Stop], cursor: &str, step: i32, by_card: bool) -> Option<String> {
    if stops.is_empty() {
        return None;
    }
    let at = stops.iter().position(|s| s.id == cursor);
    if !by_card {
        let next = match at {
            None if step > 0 => 0,
            None => stops.len().checked_sub(1)?,
            Some(i) => (i as i64 + step as i64).clamp(0, stops.len() as i64 - 1) as usize,
        };
        return stops.get(next).map(|s| s.id.clone());
    }
    let mut cards: Vec<&str> = Vec::new();
    for stop in stops {
        if !cards.contains(&stop.card) {
            cards.push(stop.card);
        }
    }
    let here = at.map(|i| stops[i].card);
    let index = match here.and_then(|c| cards.iter().position(|x| *x == c)) {
        None if step > 0 => 0,
        None => cards.len().checked_sub(1)?,
        Some(i) => (i as i64 + step as i64).rem_euclid(cards.len() as i64) as usize,
    };
    let card = cards.get(index)?;
    stops.iter().find(|s| s.card == *card).map(|s| s.id.clone())
}

/// The first row of section `digit` (1 is the first), if the profile shows it.
pub fn section(stops: &[Stop], digit: usize) -> Option<String> {
    let (key, _) = SECTIONS.get(digit.checked_sub(1)?)?;
    stops.iter().find(|s| s.id == *key || s.card == *key).map(|s| s.id.clone())
}

fn shown_now(window: &AppWindow) -> Shown {
    let bridge = window.global::<ProfileBridge>();
    let info = bridge.get_info();
    Shown {
        chat_view: info.chat_view_shown,
        agent_messages: info.agent_messages_shown,
        tool_activity: info.tool_activity_applies,
        backend: info.backend_shown,
        plan: info.plan_shown,
        prompts_shown: info.prompts_shown,
        prompts: bridge.get_prompts().row_count(),
        prompts_more: info.prompts_more,
        mcp: if info.mcp_shown { bridge.get_mcp().row_count() } else { 0 },
        schedules: bridge.get_schedules().row_count(),
    }
}

/// The profile takes the window's focus, so no key reaches the composer
/// behind it; the window's keys still read every key first.
fn hold_keyboard(window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    bridge.set_focus_request(bridge.get_focus_request().wrapping_add(1));
}

/// Whether the profile has the keyboard: the keymap's "profile" context.
/// Not while its Backend picker shows ("transit"), nor while one of its
/// choices has the keyboard after a click (its own arrows open and move it).
pub fn keyboard(app: &App, window: &AppWindow) -> bool {
    let bridge = window.global::<ProfileBridge>();
    app.overlay.borrow().as_str() == "profile" && bridge.get_transit_step().is_empty() && !bridge.get_choice_focused()
}

/// Where closing the profile gives the keyboard back (the switcher says,
/// for a profile opened from it).
pub fn set_origin(region: &'static str) {
    STATE.with(|s| s.borrow_mut().origin = region);
}

/// Closing the profile gives the keyboard back where it was; false where
/// another surface takes it (Updates, Teams, Settings).
pub fn give_back(app: &App, window: &AppWindow) -> bool {
    if window.get_surface() != "chats" {
        return false;
    }
    match STATE.with(|s| std::mem::take(&mut s.borrow_mut().origin)) {
        "sidebar" => window.invoke_focus_sidebar(),
        "search" => window.invoke_focus_search(),
        "pane" => app.focus_transcript(),
        _ => app.focus_composer(),
    }
    true
}

/// The agent's publications, in the one overlay that lists them all (the
/// gallery's): `filter` 0 is everything, 1 its images. Its Escape comes back
/// to the profile (`resume`).
pub fn open_publications(window: &AppWindow, filter: i32) {
    let publications = window.global::<crate::PublicationsBridge>();
    publications.invoke_open(session().into());
    publications.invoke_choose_filter(filter);
}

/// Back on the profile from a dialog opened over it (the publications):
/// it shows with its keys and is brought up to date.
pub fn resume(app: &App, window: &AppWindow) {
    commands::open_overlay(app, window, "profile");
    hold_keyboard(window);
    app.refresh(&[Change::Profile]);
}

/// One of the profile's choices one step on (Left/Right, Enter goes round).
fn step_choice(window: &AppWindow, id: &str, step: i32, round: bool) {
    let bridge = window.global::<ProfileBridge>();
    let mut info = bridge.get_info();
    let (current, count) = match id {
        "chat-view" => (info.chat_view_index, bridge.get_chat_view_choices().row_count()),
        "agent-messages" => (info.agent_messages_index, bridge.get_agent_message_choices().row_count()),
        "tool-activity" => (info.tool_activity_index, bridge.get_tool_activity_choices().row_count()),
        "model" => (info.model_index, bridge.get_models().row_count()),
        "effort" => (info.effort_index, bridge.get_efforts().row_count()),
        _ => return,
    };
    if count == 0 {
        return;
    }
    let next = if round { (current.max(0) + step).rem_euclid(count as i32) } else { (current + step).clamp(0, count as i32 - 1) };
    if next == current {
        return;
    }
    match id {
        "chat-view" => info.chat_view_index = next,
        "agent-messages" => info.agent_messages_index = next,
        "tool-activity" => info.tool_activity_index = next,
        "model" => info.model_index = next,
        _ => info.effort_index = next,
    }
    // Shown at once; the Host's answer refreshes it.
    bridge.set_info(info);
    match id {
        "chat-view" => bridge.invoke_set_chat_view(next),
        "agent-messages" => bridge.invoke_set_agent_messages(next),
        "tool-activity" => bridge.invoke_set_tool_activity(next),
        "model" => bridge.invoke_set_model(next),
        _ => bridge.invoke_set_effort(next),
    }
}

fn index_of(id: &str, prefix: &str) -> Option<usize> {
    id.strip_prefix(prefix).and_then(|n| n.parse().ok())
}

/// Enter on the cursor's row: what a click on it does.
fn enter(window: &AppWindow, id: &str) {
    let bridge = window.global::<ProfileBridge>();
    let info = bridge.get_info();
    match id {
        "files" => bridge.invoke_open_files(),
        "terminal" => bridge.invoke_open_terminal(),
        "voice" => bridge.invoke_voice(),
        "chat-view" | "agent-messages" | "tool-activity" | "model" | "effort" => step_choice(window, id, 1, true),
        "backend" if info.backend_enabled => bridge.invoke_open_transit(),
        "history-more" if !info.prompts_loading => bridge.invoke_load_older(),
        "heartbeat" => bridge.invoke_set_heartbeat(!info.heartbeat),
        "dreaming" => bridge.invoke_set_dreaming(!info.dreaming),
        "push-alerts" => bridge.invoke_set_push_alerts(!info.push_alerts),
        "compact" if !info.compacting => bridge.set_confirm("compact".into()),
        "queue" => bridge.invoke_queue(),
        "relaunch" => bridge.invoke_relaunch(),
        "release" => bridge.set_confirm("release".into()),
        "publications" => open_publications(window, 0),
        "images" => open_publications(window, 1),
        _ => {
            if let Some(server) = index_of(id, "mcp:").and_then(|i| bridge.get_mcp().row_data(i)) {
                bridge.invoke_set_mcp(server.name, !server.on);
            } else if let Some(schedule) = index_of(id, "schedule:").and_then(|i| bridge.get_schedules().row_data(i)) {
                bridge.invoke_set_schedule(schedule.id, !schedule.on);
            }
        }
    }
}

/// A key of the keymap's "profile" context (`digit` for profile-section).
/// The confirmation over the profile takes Enter; Escape is the dialogs'
/// own (`escape`).
pub fn key(window: &AppWindow, action: &str, digit: Option<usize>) {
    let bridge = window.global::<ProfileBridge>();
    if !bridge.get_confirm().is_empty() {
        if action == "profile-enter" {
            match bridge.get_confirm().as_str() {
                "compact" => bridge.invoke_compact(),
                "release" => bridge.invoke_release(),
                _ => {}
            }
        }
        return;
    }
    let stops = stops(&shown_now(window));
    let cursor = bridge.get_cursor().to_string();
    let next = match action {
        "profile-section" => digit.and_then(|d| section(&stops, d)),
        "profile-down" => moved(&stops, &cursor, 1, false),
        "profile-up" => moved(&stops, &cursor, -1, false),
        "profile-next-card" => moved(&stops, &cursor, 1, true),
        "profile-previous-card" => moved(&stops, &cursor, -1, true),
        "profile-left" | "profile-right" => {
            step_choice(window, &cursor, if action == "profile-left" { -1 } else { 1 }, false);
            None
        }
        "profile-enter" if stops.iter().any(|s| s.id == cursor) => {
            enter(window, &cursor);
            None
        }
        // Nothing chosen yet: Enter starts at the top.
        "profile-enter" => moved(&stops, "", 1, false),
        _ => None,
    };
    if let Some(next) = next {
        set_cursor(window, &next);
    }
}

/// Puts the keyboard's cursor on row `id`.
fn set_cursor(window: &AppWindow, id: &str) {
    window.global::<ProfileBridge>().set_cursor(id.into());
}

/// The shortcut bar while the profile has the keyboard: what the keys do
/// on the cursor's row, in the user's keys.
pub fn hints(app: &App, window: &AppWindow) -> Vec<crate::Hint> {
    let overrides = commands::overrides(app);
    let key = |action: &str| crate::keymap::shown("profile", action, &overrides).unwrap_or_default();
    let pair = |a: &str, b: &str| format!("{}/{}", key(a), key(b)).trim_matches('/').to_owned();
    let bridge = window.global::<ProfileBridge>();
    let info = bridge.get_info();
    let mut out: Vec<(String, String)> = Vec::new();
    let confirm = bridge.get_confirm();
    if !confirm.is_empty() {
        out.push((key("profile-enter"), if confirm == "release" { "Release" } else { "Compact" }.into()));
        out.push((key("escape"), "Cancel".into()));
    } else {
        let cursor = bridge.get_cursor().to_string();
        let on = |flag: bool| if flag { "Turn off" } else { "Turn on" };
        let entry: Option<String> = match cursor.as_str() {
            "files" => Some("Open files".into()),
            "terminal" => Some("Open terminal".into()),
            "voice" => Some("Voice".into()),
            "backend" if info.backend_enabled => Some("Change backend".into()),
            "history-more" if !info.prompts_loading => Some("Load older".into()),
            "heartbeat" => Some(on(info.heartbeat).into()),
            "dreaming" => Some(on(info.dreaming).into()),
            "push-alerts" => Some(on(info.push_alerts).into()),
            "compact" if !info.compacting => Some("Compact".into()),
            "queue" => Some("Manage queue".into()),
            "relaunch" => Some("Relaunch".into()),
            "release" => Some("Release…".into()),
            "publications" => Some("All publications".into()),
            "images" => Some("Images".into()),
            id if id.starts_with("mcp:") => index_of(id, "mcp:").and_then(|i| bridge.get_mcp().row_data(i)).map(|s| on(s.on).into()),
            id if id.starts_with("schedule:") => index_of(id, "schedule:").and_then(|i| bridge.get_schedules().row_data(i)).map(|s| on(s.on).into()),
            _ => None,
        };
        let choice = matches!(cursor.as_str(), "chat-view" | "agent-messages" | "tool-activity" | "model" | "effort");
        out.push(("1-9".into(), "Sections".into()));
        out.push((pair("profile-up", "profile-down"), "Move".into()));
        out.push((key("profile-next-card"), "Next card".into()));
        if choice {
            out.push((pair("profile-left", "profile-right"), "Change".into()));
        }
        if let Some(label) = entry {
            out.push((key("profile-enter"), label));
        }
        out.push((key("escape"), "Close".into()));
    }
    out.into_iter().filter(|(keys, _)| !keys.is_empty()).map(|(keys, label)| crate::Hint { keys: keys.into(), label: label.into() }).collect()
}

pub fn owns(app: &App) -> bool {
    OWNED.contains(&app.overlay.borrow().as_str())
}

/// Escape closes the innermost thing: a confirmation or the overview's menu
/// first, then the dialog.
pub fn escape(app: &App, window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    let overview = window.global::<crate::OverviewBridge>();
    let overlay = app.overlay.borrow().clone();
    if overlay == "profile" && !bridge.get_transit_step().is_empty() {
        let session = session();
        app.engine.borrow_mut().transit_back(&session);
        fill(app, window, &session);
    } else if overlay == "profile" && !bridge.get_confirm().is_empty() {
        bridge.set_confirm("".into());
    } else if overlay == "overview" && !overview.get_menu().is_empty() {
        overview.set_menu("".into());
        overview.set_confirm_release("".into());
    } else {
        close(app, window);
    }
}

/// The local path of a `file://` URL (percent-decoded).
pub(crate) fn file_path(url: &str) -> Option<std::path::PathBuf> {
    let encoded = url.strip_prefix("file://")?.as_bytes();
    let mut bytes = Vec::with_capacity(encoded.len());
    let mut index = 0;
    while index < encoded.len() {
        let hex = encoded.get(index + 1..index + 3).and_then(|h| std::str::from_utf8(h).ok()).and_then(|h| u8::from_str_radix(h, 16).ok());
        match (encoded[index], hex) {
            (b'%', Some(byte)) => {
                bytes.push(byte);
                index += 3;
            }
            (byte, _) => {
                bytes.push(byte);
                index += 1;
            }
        }
    }
    use std::os::unix::ffi::OsStringExt;
    Some(std::ffi::OsString::from_vec(bytes).into())
}

pub fn reduced_motion(app: &App) -> bool {
    app.engine.borrow().settings().boolean("appearance/reducedMotion", false)
}

/// The live status line under the chat (Settings, off by default).
pub fn live_status_line(app: &App) -> bool {
    app.engine.borrow().settings().boolean(LIVE_STATUS_LINE, false)
}

pub const LIVE_STATUS_LINE: &str = "appearance/liveStatusLine";

/// The send animation (Settings, on by default).
pub fn send_animation(app: &App) -> bool {
    app.engine.borrow().settings().boolean(SEND_ANIMATION, true)
}

pub const SEND_ANIMATION: &str = "appearance/sendAnimation";

/// Opens a `file://` URL in the desktop's file manager; checks record it in
/// `CLARP_TEST_OPEN_URL` instead.
pub(crate) fn open_file_url(url: &str) {
    if !url.starts_with("file://") {
        eprintln!("clarp-slint: not opening {url}: only local folders open here");
        return;
    }
    if let Some(path) = std::env::var_os("CLARP_TEST_OPEN_URL").filter(|p| !p.is_empty()) {
        use std::io::Write;
        let written = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{url}"));
        if let Err(error) = written {
            eprintln!("clarp-slint: could not record {url}: {error}");
        }
        return;
    }
    crate::platform::opener::open(url, false);
}

// ---- the profile ---------------------------------------------------------------

pub fn session() -> String {
    STATE.with(|s| s.borrow().session.clone())
}

/// Opens `session`'s profile (Main.qml `onProfileRequested`): the chat is
/// selected and its plan, prompts, updates, teams and images load.
pub fn open(app: &Rc<App>, window: &AppWindow, session: &str) {
    if session.is_empty() || session.starts_with("pair:") {
        return;
    }
    // Where the keyboard was, to give it back on closing (not over another
    // of this group's dialogs, which keeps its own).
    if app.overlay.borrow().is_empty() {
        let origin = commands::keyboard_region(app, window);
        STATE.with(|s| s.borrow_mut().origin = origin);
    }
    STATE.with(|s| s.borrow_mut().session = session.to_owned());
    {
        let mut engine = app.engine.borrow_mut();
        if engine.selected_session() != session {
            engine.select(session);
        }
        engine.load_agent_profile(session);
        // Its images' sources also resolve the chat's own image links.
        engine.load_media(session);
    }
    let bridge = window.global::<ProfileBridge>();
    bridge.set_confirm("".into());
    set_cursor(window, "");
    show(app, window, "profile");
    hold_keyboard(window);
    pump_now(app);
    refresh(app, window, &[Change::Profile]);
}

/// Brings the open profile up to date.
pub fn refresh(app: &App, window: &AppWindow, changes: &[Change]) {
    let reduced = reduced_motion(app);
    window.global::<ProfileBridge>().set_reduced_motion(reduced);
    let look = window.global::<crate::ChatLook>();
    look.set_reduced_motion(reduced);
    look.set_live_status_line(live_status_line(app));
    look.set_send_animation(send_animation(app));
    window.global::<crate::SendFlight>().set_status_grace(crate::view::SEND_STATUS_GRACE.as_millis() as i64);
    crate::overview_view::refresh(app, window, changes, reduced);
    crate::voice_view::refresh(app, window, changes, reduced);
    crate::orchestrator_view::refresh(app, window, changes, reduced);
    let session = session();
    if session.is_empty() || *app.overlay.borrow() != "profile" {
        return;
    }
    let relevant = changes.iter().any(|c| {
        matches!(
            c,
            Change::Profile
                | Change::Roster
                | Change::AgentMutated(_)
                | Change::Launch
                | Change::Updates
                | Change::Teams
                | Change::Avatars
                | Change::Connection
        )
    });
    if relevant {
        fill(app, window, &session);
        // The bar says what Enter does to the row as it is now ("Turn off").
        if keyboard(app, window) {
            commands::show_hints(app, window);
        }
    }
}

fn fill(app: &App, window: &AppWindow, session: &str) {
    let bridge = window.global::<ProfileBridge>();
    let Some(details) = app.engine.borrow_mut().agent_details(session) else {
        // Released or gone: the profile has nothing left to show.
        close(app, window);
        return;
    };
    let portrait = crate::avatar_view::portrait(app, session, crate::avatar_view::sizes(app).card);
    let engine = app.engine.borrow();
    let value = |key: &str| text(&details, key);
    let flag = |key: &str| details.get(key).and_then(Value::as_bool).unwrap_or(false);
    let count = |key: &str| details.get(key).and_then(Value::as_i64).unwrap_or(0);
    let backend = value("backend");
    let name = first(&[value("name")], "Agent");

    // Model and effort, as the catalog lists them. What the agent runs
    // (its roster row) is always among them: a model or effort the catalog
    // does not list (one set with /agent-llm, a provider's own) is added,
    // so the box says "Harness default" only when the agent runs it.
    let mut models = engine.models_for_backend(&backend);
    with_current(&mut models, &value("model"));
    let model_ids: Vec<String> = models.iter().map(|m| text(m, "id")).collect();
    let model_index = model_ids.iter().position(|id| *id == value("model")).unwrap_or(0);
    let mut efforts = engine.efforts_for_model(&backend, &value("model"));
    with_current(&mut efforts, &value("effort"));
    let effort_ids: Vec<String> = efforts.iter().map(|e| text(e, "id")).collect();
    let effort_index = effort_ids.iter().position(|id| *id == value("effort")).unwrap_or(0);
    bridge.set_models(ModelRc::new(VecModel::from(models.iter().map(|m| SharedString::from(first(&[text(m, "label"), text(m, "id")], "")))
        .collect::<Vec<_>>())));
    bridge.set_efforts(ModelRc::new(VecModel::from(efforts.iter().map(|e| SharedString::from(first(&[text(e, "label"), text(e, "id")], "")))
        .collect::<Vec<_>>())));
    STATE.with(|s| {
        let mut state = s.borrow_mut();
        state.model_ids = model_ids;
        state.effort_ids = effort_ids;
    });

    // The plan.
    let plan = engine.profile_task_plan();
    let plan_value = Value::Object(plan.clone());
    let items: Vec<PlanItem> = plan
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|item| {
            let steps = item.get("subtasks").and_then(Value::as_array).map_or(0, Vec::len);
            PlanItem {
                title: first(&[text(item, "title")], "Task").into(),
                status: text(item, "status").into(),
                steps: if steps > 0 { format!("{steps} steps") } else { String::new() }.into(),
            }
        })
        .collect();
    let number = |key: &str| plan.get(key).and_then(Value::as_i64).unwrap_or(0);
    bridge.set_plan(ModelRc::new(VecModel::from(items)));

    // Prompts, heartbeat, teams, MCP servers, schedules.
    let prompts: Vec<PromptItem> = engine
        .profile_prompts()
        .iter()
        .map(|p| {
            let channel = first(&[p.get("prompt_origin").map(|o| text(o, "channel")).unwrap_or_default()], "chat");
            let stamp = text(p, "created_at");
            PromptItem {
                text: first(&[text(p, "text"), text(p, "preview")], "").into(),
                meta: if stamp.is_empty() { channel } else { format!("{stamp}  ·  {channel}") }.into(),
            }
        })
        .collect();
    bridge.set_prompts(ModelRc::new(VecModel::from(prompts)));
    let heartbeat = Value::Object(engine.profile_heartbeat().clone());
    let schedule = heartbeat.get("schedule").cloned().unwrap_or(Value::Null);
    let scheduled = |key: &str| schedule.get(key).and_then(Value::as_bool).unwrap_or(false);
    let history: Vec<SharedString> = heartbeat
        .get("history")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|h| SharedString::from(first(&[text(h, "text")], "Heartbeat check")))
        .collect();
    bridge.set_heartbeat_history(ModelRc::new(VecModel::from(history)));
    let teams: Vec<NamedItem> = details
        .get("team_ids")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(|id| NamedItem { id: id.into(), name: engine.team_name_by_id(id).into(), ..NamedItem::default() })
        .collect();
    bridge.set_teams(ModelRc::new(VecModel::from(teams)));
    let chosen: Vec<String> = details.get("mcp_servers").and_then(Value::as_array).into_iter().flatten().map(name_of).collect();
    let available: Vec<String> = engine.available_mcp_servers().iter().map(name_of).filter(|n| !n.is_empty()).collect();
    let mcp: Vec<NamedItem> =
        available.iter().map(|n| NamedItem { id: n.as_str().into(), name: n.as_str().into(), on: chosen.contains(n), ..NamedItem::default() }).collect();
    bridge.set_mcp(ModelRc::new(VecModel::from(mcp)));
    let schedules: Vec<NamedItem> = details
        .get("schedules")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|s| NamedItem {
            id: text(s, "schedule_id").into(),
            name: first(&[text(s, "name")], "Scheduled task").into(),
            detail: text(s, "cron_expression").into(),
            on: s.get("enabled").and_then(Value::as_bool).unwrap_or(false),
        })
        .collect();
    bridge.set_schedules(ModelRc::new(VecModel::from(schedules)));

    // The latest artifacts (the menu's checks read them; the Publications
    // overlay draws them).
    let artifacts: Vec<ProfileArtifact> = engine
        .artifacts_for_session(session)
        .iter()
        .map(|a| ProfileArtifact {
            title: first(&[text(a, "title")], "Artifact").into(),
            kind: first(&[text(a, "type")], "item").to_uppercase().into(),
            summary: text(a, "summary").into(),
        })
        .collect();
    bridge.set_artifacts(ModelRc::new(VecModel::from(artifacts)));

    let transit_row = backend_row(&engine, session, &name, &backend, &value("model"));

    // Unknown usage (null) stays unknown: no gauge and no "0 tokens" (#403).
    let (tokens, window_size) = (details.get("context_tokens").and_then(Value::as_i64), count("context_window"));
    let queue = count("queue_count");
    let state = value("state");
    let info = ProfileInfo {
        session: session.into(),
        name: name.as_str().into(),
        initial: crate::view::initial(&name),
        symbol: SharedString::new(),
        portrait,
        backend: backend.into(),
        folder: first(&[value("working_directory")], "Unavailable").into(),
        state: first(&[state.clone()], "offline").into(),
        model_index: model_index as i32,
        effort_index: effort_index as i32,
        heartbeat: flag("heartbeat_enabled"),
        dreaming: flag("dreaming_enabled"),
        push_alerts: !flag("muted"),
        context_fraction: match tokens {
            Some(tokens) if window_size > 0 => tokens as f32 / window_size as f32,
            Some(_) => 0.0,
            None => -1.0,
        },
        context_used: match tokens {
            Some(tokens) => format!("{tokens} / {} tokens", if window_size > 0 { window_size.to_string() } else { "unknown".into() }),
            // Unknown usage says nothing: the row is left out.
            None => String::new(),
        }
        .into(),
        compacting: state == "compacting",
        queue_text: if queue == 0 { "Nothing waiting".to_owned() } else { format!("{queue} waiting") }.into(),
        plan_shown: engine.profile_loading() || !plan.is_empty(),
        plan_loading: engine.profile_loading(),
        plan_title: text(&plan_value, "title").into(),
        plan_progress: format!("{}/{}", number("completed_count"), number("total_count")).into(),
        // The Heartbeat switch says on or off; only a dormant one adds a word.
        heartbeat_note: if scheduled("dormant") { "Dormant" } else { "" }.into(),
        prompts_loading: engine.profile_prompts_loading(),
        prompts_more: engine.profile_prompts_have_more(),
        prompts_shown: engine.profile_prompts_offered(),
        mcp_shown: value("backend") == "claude" && !available.is_empty(),
        agent_messages_shown: engine.chat_hidden_coordination(session),
        agent_messages_index: crate::chat_menu::COORDINATION.iter().position(|(v, _)| *v == engine.chat_coordination_choice(session)).unwrap_or(0) as i32,
        tool_activity_index: engine.chat_activity_choice(session).map_or(0, |mode| mode + 1),
        backend_shown: transit_row.shown,
        backend_value: transit_row.value.into(),
        backend_detail: transit_row.detail.into(),
        backend_enabled: transit_row.enabled,
        backend_moving: transit_row.moving,
        backend_failed: transit_row.failed,
        // The chat's view (#702), as its menu offers it.
        chat_view_shown: engine.chat_view_offered(session),
        chat_view_index: clarp_core::chat_view::MENU.iter().position(|(v, _)| *v == engine.chat_view_menu_value(session)).unwrap_or(0) as i32,
        tool_activity_applies: !(engine.chat_view_offered(session) && engine.chat_view_conversation(session)),
        error: engine.profile_error().into(),
    };
    drop(engine);
    bridge.set_info(info);
    bridge.set_transit_step(transit_row.picker.step.into());
    bridge.set_transit_title(transit_row.picker.title.into());
    bridge.set_transit_body(transit_row.picker.body.into());
    bridge.set_transit_loading(transit_row.picker.loading);
    bridge.set_transit_error(transit_row.picker.error.into());
    // The keyboard starts on what the agent runs now, else the first
    // choice that can be chosen; a new list (backends, then models) resets it.
    let choices = transit_row.picker.choices;
    let start = choices.iter().position(|c| c.current && c.enabled).or_else(|| choices.iter().position(|c| c.enabled));
    let same = bridge.get_transit_choices().iter().map(|c| c.id).eq(choices.iter().map(|c| c.id.clone()));
    if !same {
        bridge.set_transit_cursor(start.map_or(-1, |i| i as i32));
    }
    bridge.set_transit_choices(ModelRc::new(VecModel::from(choices)));
}

// ---- the Backend row (feature agent_backend_transit) ---------------------------

#[derive(Default)]
struct BackendRow {
    shown: bool,
    value: String,
    detail: String,
    enabled: bool,
    moving: bool,
    failed: bool,
    picker: Picker,
}

#[derive(Default)]
struct Picker {
    /// "", "backends", "models" or "confirm".
    step: &'static str,
    title: String,
    body: String,
    loading: bool,
    error: String,
    choices: Vec<TransitChoice>,
}

/// The chat header's "Moving to …" while the agent changes backend (#723),
/// the words the profile's Backend row shows; empty otherwise. The roster
/// row's `transit` drives it (also a move another app started), and it
/// clears when the move is done or failed.
pub(crate) fn moving_line(engine: &clarp_engine::Engine, session: &str) -> String {
    let Some(agent) = engine.roster().find(session) else { return String::new() };
    let row = backend_row(engine, session, &engine.chat_name(session), &agent.backend, &agent.model);
    if row.moving { row.detail } else { String::new() }
}

/// The Backend row and its picker, from the agent's roster row, its flow and
/// the options the Host offered it. Nothing without the feature.
fn backend_row(engine: &clarp_engine::Engine, session: &str, name: &str, backend: &str, model: &str) -> BackendRow {
    if !engine.transit_offered(session) {
        return BackendRow::default();
    }
    let options = engine.transit_options(session);
    let flow = engine.transit_flow(session);
    // A backend and a model in words: the picker's labels, else the catalog's.
    let backend_label = |id: &str| -> String {
        clarp_core::catalog::antigravity_label(id)
            .map(str::to_owned)
            .or_else(|| options.backends.iter().find(|b| b.id == id).map(|b| b.label.clone()))
            .or_else(|| engine.backend_options().iter().find(|o| text(o, "id") == id).map(|o| text(o, "label")))
            .filter(|l| !l.is_empty())
            .unwrap_or_else(|| id.to_owned())
    };
    let model_label = |backend: &str, id: &str| -> String {
        if id.is_empty() {
            return String::new();
        }
        options
            .backends
            .iter()
            .find(|b| b.id == backend)
            .and_then(|b| b.models.iter().find(|m| m.id == id).map(|m| m.label.clone()))
            .or_else(|| engine.models_for_backend(backend).iter().find(|m| text(m, "id") == id).map(|m| text(m, "label")))
            .filter(|l| !l.is_empty())
            .unwrap_or_else(|| id.to_owned())
    };
    let blocked = engine.transit_blocked(session);
    // The row names the tool alone: the Model line below says the model.
    let mut row = BackendRow { shown: true, value: backend_label(backend), ..BackendRow::default() };
    match &flow.step {
        Step::Moving { backend: target, phase, .. } => {
            row.moving = true;
            let target_model = model_label(target, &flow.target_model);
            let shown_model = if target_model.is_empty() { "the default model".to_owned() } else { target_model };
            row.detail = transit::progress(&backend_label(target), &shown_model, target == backend, *phase);
        }
        _ if blocked => row.detail = transit::TURN_BLOCKED.to_owned(),
        Step::Failed { reason, .. } => {
            row.detail = reason.clone();
            row.failed = true;
        }
        _ => {}
    }
    row.enabled = !blocked && !row.moving;
    let picker = &mut row.picker;
    picker.loading = options.loading;
    picker.error = options.error.clone();
    match &flow.step {
        Step::Picking => {
            picker.step = "backends";
            picker.title = transit::ROW_TITLE.to_owned();
            picker.choices = options
                .choices()
                .map(|b| TransitChoice {
                    id: b.id.as_str().into(),
                    label: b.label.as_str().into(),
                    detail: b.reason.as_str().into(),
                    enabled: b.available,
                    current: b.id == backend,
                })
                .collect();
        }
        Step::Model { backend: chosen } => {
            picker.step = "models";
            picker.title = format!("{} · Model", backend_label(chosen));
            let models = options.backends.iter().find(|b| b.id == *chosen).map(|b| b.models.clone()).unwrap_or_default();
            let current = |id: &str| chosen == backend && id == model;
            picker.choices = if models.is_empty() {
                // A tool that lists no models runs its own default ("").
                vec![TransitChoice { id: "".into(), label: transit::DEFAULT_MARK.into(), enabled: !current(""), current: current(""), ..TransitChoice::default() }]
            } else {
                models
                    .iter()
                    .map(|m| TransitChoice {
                        id: m.id.as_str().into(),
                        label: m.label.as_str().into(),
                        detail: if m.default { transit::DEFAULT_MARK } else { "" }.into(),
                        enabled: !current(&m.id),
                        current: current(&m.id),
                    })
                    .collect()
            };
        }
        Step::Confirming { backend: chosen, model: target } => {
            picker.step = "confirm";
            let label = model_label(chosen, target);
            let label = if label.is_empty() { transit::DEFAULT_MARK.to_lowercase() + " model" } else { label };
            if chosen == backend {
                picker.title = transit::switch_title(name, &label);
                picker.body = transit::SWITCH_BODY.to_owned();
            } else {
                picker.title = transit::confirm_title(name, &backend_label(chosen), &label);
                picker.body = transit::confirm_body(engine.transit_carries_tools(session)).to_owned();
            }
        }
        _ => {}
    }
    row
}

fn with_session(act: impl FnOnce(&Rc<App>, &AppWindow, &str)) {
    let session = session();
    if session.is_empty() {
        return;
    }
    if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
        act(&app, &window, &session);
        pump_now(&app);
    }
}

/// The agent's current details (the switches' state before a change).
fn detail(app: &App, session: &str, key: &str) -> Value {
    app.engine.borrow_mut().agent_details(session).and_then(|d| d.get(key).cloned()).unwrap_or(Value::Null)
}

/// Connects the profile's commands.
pub fn wire(window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    bridge.on_close(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            close(&app, &window);
        }
    });
    bridge.on_open_for_pane(|pane| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            let session = app.session_of(&pane);
            open(&app, &window, &session);
        }
    });
    bridge.on_open_files(|| {
        with_session(|app, _, session| {
            let url = app.engine.borrow_mut().agent_files_url(session);
            if let Some(url) = url {
                open_file_url(&url);
            }
        });
    });
    bridge.on_open_terminal(|| with_session(|app, _, session| app.engine.borrow_mut().open_agent_terminal(session)));
    bridge.on_voice(|| {
        with_session(|app, window, session| {
            let name = app.engine.borrow().chat_name(session);
            crate::voice_view::open(app, window, session, &name);
        });
    });
    bridge.on_queue(|| {
        with_session(|app, window, _| {
            if !commands::run(app, window, "manage-queue") {
                eprintln!("clarp-slint: the queue dialog is not available yet");
            }
        });
    });
    bridge.on_relaunch(|| {
        with_session(|app, window, _| {
            if !commands::run(app, window, "relaunch-agent") {
                eprintln!("clarp-slint: relaunching is not available yet");
            }
        });
    });
    bridge.on_load_older(|| with_session(|app, _, session| app.engine.borrow_mut().load_prompt_history(session, true)));
    bridge.on_set_model(|index| {
        with_session(|app, _, session| {
            let backend = detail(app, session, "backend").as_str().unwrap_or_default().to_owned();
            let chosen = STATE.with(|s| s.borrow().model_ids.get(index as usize).cloned()).unwrap_or_default();
            let options: Vec<String> = app.engine.borrow().efforts_for_model(&backend, &chosen).iter().map(|e| text(e, "id")).collect();
            // An effort the new model lacks (or its first) becomes the default.
            let mut effort = detail(app, session, "effort").as_str().unwrap_or_default().to_owned();
            if !effort.is_empty() && options.iter().position(|id| *id == effort).unwrap_or(0) == 0 {
                effort.clear();
            }
            app.engine.borrow_mut().set_agent_llm(session, &chosen, &effort);
        });
    });
    bridge.on_set_effort(|index| {
        with_session(|app, _, session| {
            let model = detail(app, session, "model").as_str().unwrap_or_default().to_owned();
            let effort = STATE.with(|s| s.borrow().effort_ids.get(index as usize).cloned()).unwrap_or_default();
            app.engine.borrow_mut().set_agent_llm(session, &model, &effort);
        });
    });
    bridge.on_open_transit(|| {
        with_session(|app, window, session| {
            app.engine.borrow_mut().open_transit(session);
            fill(app, window, session);
        });
    });
    bridge.on_pick_transit(|id| {
        with_session(|app, window, session| {
            let step = window.global::<ProfileBridge>().get_transit_step();
            if step == "backends" {
                app.engine.borrow_mut().pick_transit_backend(session, &id);
            } else if step == "models" {
                app.engine.borrow_mut().pick_transit_model(session, &id);
            }
            fill(app, window, session);
        });
    });
    bridge.on_transit_back(|| {
        with_session(|app, window, session| {
            app.engine.borrow_mut().transit_back(session);
            fill(app, window, session);
        });
    });
    bridge.on_confirm_transit(|| {
        with_session(|app, window, session| {
            app.engine.borrow_mut().confirm_transit(session);
            fill(app, window, session);
        });
    });
    bridge.on_set_heartbeat(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_heartbeat(session, on)));
    bridge.on_set_dreaming(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_dreaming(session, on)));
    bridge.on_set_push_alerts(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_push_muted(session, !on)));
    // This chat's own choices (#653): messages between agents (its Host's)
    // and Tool activity (this device's).
    bridge.on_set_agent_messages(|index| {
        with_session(|app, _, session| {
            let choice = crate::chat_menu::COORDINATION.get(index.max(0) as usize).map_or("default", |(v, _)| v);
            if app.engine.borrow().chat_coordination_choice(session) != choice {
                app.engine.borrow_mut().set_chat_coordination(session, choice);
            }
        });
    });
    // The chat's view (#702): this device's own choice, written to its Host.
    bridge.on_set_chat_view(|index| {
        with_session(|app, _, session| {
            let value = clarp_core::chat_view::MENU.get(index.max(0) as usize).map_or("default", |(v, _)| v);
            if app.engine.borrow().chat_view_menu_value(session) != value {
                app.engine.borrow_mut().set_chat_view(session, value);
            }
        });
    });
    bridge.on_set_tool_activity(|index| {
        with_session(|app, _, session| {
            let value = clarp_core::presentation::CHAT_ACTIVITY_CHOICES.get(index.max(0) as usize).map_or("follow", |(v, _)| v);
            crate::chat_menu::set_activity(&mut app.engine.borrow_mut(), session, value);
        });
    });
    bridge.on_set_mcp(|name, on| {
        with_session(|app, _, session| {
            let mut chosen: Vec<String> = detail(app, session, "mcp_servers").as_array().into_iter().flatten().map(name_of).collect();
            let name = name.to_string();
            match chosen.iter().position(|n| *n == name) {
                Some(index) if !on => {
                    chosen.remove(index);
                }
                None if on => chosen.push(name),
                _ => {}
            }
            app.engine.borrow_mut().set_agent_mcp(session, &chosen);
        });
    });
    bridge.on_set_schedule(|id, on| with_session(|app, _, _| app.engine.borrow_mut().set_schedule_enabled(&id, on)));
    bridge.on_compact(|| {
        with_session(|app, window, session| {
            window.global::<ProfileBridge>().set_confirm("".into());
            app.engine.borrow_mut().compact_session(session);
        });
    });
    bridge.on_release(|| {
        with_session(|app, window, session| {
            window.global::<ProfileBridge>().set_confirm("".into());
            app.engine.borrow_mut().release_agent(session);
            close(app, window);
        });
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(stops: &[Stop]) -> Vec<&str> {
        stops.iter().map(|s| s.id.as_str()).collect()
    }

    /// Every section the profile shows has a digit, and every row a stop:
    /// Up/Down walk them all, Tab the cards, a digit its section's first row.
    #[test]
    fn the_keyboard_reaches_every_section() {
        let shown = Shown { backend: true, plan: true, prompts_shown: true, prompts: 2, prompts_more: true, mcp: 1, schedules: 1, ..Shown::default() };
        let all = stops(&shown);
        let found: Vec<Option<String>> = (1..=SECTIONS.len()).map(|d| section(&all, d)).collect();
        let want = ["publications", "images", "files", "terminal", "voice", "plan", "prompt:0", "model", "backend"];
        assert_eq!(found, want.map(|w| Some(w.to_owned())));
        assert_eq!(section(&all, 10), None);
        assert!(ids(&all).contains(&"history-more") && ids(&all).contains(&"mcp:0") && ids(&all).contains(&"schedule:0") && ids(&all).contains(&"release"));
        // Rows stop at the ends; nothing chosen, Down is the first.
        assert_eq!(moved(&all, "", 1, false).as_deref(), Some("files"));
        assert_eq!(moved(&all, "files", -1, false).as_deref(), Some("files"));
        assert_eq!(moved(&all, "release", 1, false).as_deref(), Some("release"));
        assert_eq!(moved(&all, "publications", 1, false).as_deref(), Some("images"));
        // Cards go round.
        assert_eq!(moved(&all, "terminal", 1, true).as_deref(), Some("backend"));
        assert_eq!(moved(&all, "model", 1, true).as_deref(), Some("heartbeat"));
        assert_eq!(moved(&all, "relaunch", 1, true).as_deref(), Some("files"));
        assert_eq!(moved(&all, "files", -1, true).as_deref(), Some("relaunch"));
        // A section the profile does not show: the digit does nothing.
        let plain = stops(&Shown::default());
        assert_eq!(section(&plain, 6), None, "no plan");
        assert_eq!(section(&plain, 9), None, "no backend row");
        assert_eq!(section(&plain, 1).as_deref(), Some("publications"), "always there, empty or not");
        assert_eq!(section(&plain, 2).as_deref(), Some("images"));
    }
}
