//! The Clarp desktop in Slint: a thin view over `clarp-engine`. The engine
//! owns behaviour; this file turns its state into Slint models and the
//! window's actions into engine commands.
//!
//! `clarp-slint` opens the window. `--headless` runs it without a display on
//! Slint's software renderer (for checks), and `--e2e-out DIR` drives it like
//! a person would, saving a screenshot per stage (see `driver.rs`).

mod catalogue;
mod commands;
mod driver;
mod headless;
mod keymap;
mod keymap_view;
mod link_hints;
mod live_view;
mod launch;
mod panes;
mod perf;
mod scroll_book;
mod scroll_journal;
mod preview_view;
mod platform;
mod settings_view;
// ---- every preference, applied (Settings, Ctrl+K, :set, the settings file)
mod look;
mod switcher;
mod search_view;
mod mention_view;
mod view;
// ---- updates and teams
mod teams_view;
mod updates_view;
// ---- launch dialogs
mod agent_dialogs_view;
mod launch_view;
// ---- profile and overview
mod artifacts_view;
mod form_server;
mod cells_view;
mod orchestrator_view;
mod overview_view;
mod profile_view;
mod avatar_view;
mod voice_view;
mod font_view;

use view::{apply_theme, chat_row, initial, open_link};

use std::cell::RefCell;
use std::rc::Rc;

use clarp_core::settings::Settings;
use clarp_engine::{Change, Config, Engine};
use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};

slint::include_modules!();

thread_local! {
    static APP: RefCell<Option<Rc<App>>> = const { RefCell::new(None) };
}

pub struct App {
    pub engine: RefCell<Engine>,
    pub window: slint::Weak<AppWindow>,
    chats: Rc<VecModel<ChatRow>>,
    rooms: Rc<VecModel<ChatRow>>,
    archived: Rc<VecModel<ChatRow>>,
    panes: Rc<VecModel<PaneView>>,
    pane_state: RefCell<Vec<panes::PaneState>>,
    sidebar: RefCell<clarp_core::sidebar::Sidebar>,
    workspaces: RefCell<clarp_core::workspace::WorkspaceContext>,
    /// Messages whose tool calls the reader opened (not groups).
    expanded: RefCell<std::collections::HashSet<String>>,
    /// A live-preview selection (explorer cursor): the keyboard stays put.
    pub previewing: std::cell::Cell<bool>,
    /// Chats opened in this window, the most recent first (Ctrl+R).
    pub recent: RefCell<Vec<String>>,
    /// Chats whose sub-agents show in the explorer; the rest fold them.
    pub unfolded: RefCell<std::collections::HashSet<String>>,
    pub prefs: RefCell<Prefs>,
    pub switcher: RefCell<SwitcherState>,
    /// The dialog over the window ("" for none).
    pub overlay: RefCell<String>,
    /// An agent the launch asked for, until the window starts it.
    pub launch: RefCell<Option<launch::Request>>,
    /// The artifact card the keyboard is on (J/K in the chat), "" for none.
    pub artifact_cursor: RefCell<String>,
    /// The answer chosen on each decision or question card (1-9).
    pub artifact_choices: RefCell<std::collections::HashMap<String, i32>>,
    /// Answers of one's own as typed, and the card whose field is open.
    pub artifact_drafts: RefCell<std::collections::HashMap<String, String>>,
    pub artifact_editing: RefCell<String>,
    /// Decision cards this window has shown pending.
    pub artifact_seen_pending: RefCell<std::collections::HashSet<String>>,
}

/// View preferences the window keeps (the Qt controller's names).
#[derive(Debug, Clone, Copy)]
pub struct Prefs {
    pub timestamps: bool,
    pub workspace_bar: bool,
}

impl Prefs {
    fn load(settings: &Settings) -> Self {
        Self {
            timestamps: settings.boolean("conversation/timestampsVisible", false),
            workspace_bar: settings.boolean("appearance/workspaceBar", true),
        }
    }
}

#[derive(Debug, Default)]
pub struct SwitcherState {
    pub open: bool,
    pub query: String,
    pub items: Vec<switcher::Item>,
    pub selected: String,
    /// The composer had the keyboard when the switcher opened.
    pub restore_composer: bool,
    // ---- launch dialogs: "Start an idle contact" lists only idle contacts.
    pub contacts_only: bool,
    /// Ctrl+R: recent agents only, the last opened first.
    pub recent_only: bool,
    /// The setting whose choices are listed (Enter on a choice), if any.
    pub picker: String,
    /// Ctrl+F: messages only (message search).
    pub messages_only: bool,
}

pub fn app() -> Option<Rc<App>> {
    APP.with(|a| a.borrow().clone())
}

pub fn window() -> Option<AppWindow> {
    app()?.window.upgrade()
}

/// The Slint app's own settings, apart from the Qt apps'.
fn settings() -> Settings {
    match std::env::var("CLARP_SETTINGS") {
        Ok(value) if value == "off" => Settings::in_memory(),
        Ok(value) if !value.is_empty() => Settings::at(value),
        _ => match clarp_core::settings::config_home() {
            Some(config) => Settings::at(config.join("MaxTeaBag").join("ClarpSlint").join("settings.json")),
            None => Settings::in_memory(),
        },
    }
}

/// The compact explorer's width: an average row (avatar and name), so most
/// names fit and the longest are elided. The chrome is a monospace face,
/// about 0.6 em per character.
fn compact_width(window: &AppWindow, chats: &[ChatRow], avatar: f32) -> f32 {
    let names: Vec<usize> = chats.iter().map(|c| c.name.chars().count()).filter(|n| *n > 0).collect();
    let average = if names.is_empty() { 8.0 } else { names.iter().sum::<usize>() as f32 / names.len() as f32 };
    let char_width = window.global::<Palette>().get_body_size() * 0.6;
    let queue_badge = if chats.iter().any(|c| c.queue > 0) { 22.0 } else { 0.0 };
    // Sidebar and frame padding, row padding, the avatar and its gap.
    let chrome = 16.0 + 12.0 + 20.0 + avatar + 10.0 + 12.0;
    (chrome + queue_badge + average.ceil() * char_width).clamp(120.0, 320.0)
}

impl App {
    /// The chat list, through the same search/scope/nesting rules as the
    /// Qt sidebar (`clarp_core::sidebar`).
    fn chat_rows(&self, engine: &Engine, window: &AppWindow) -> Vec<ChatRow> {
        use clarp_core::sidebar::{FilterInput, TreeInput};
        let mut laps = perf::Laps::new();
        let rows = engine.roster().rows();
        laps.lap("roster rows");
        let trees: Vec<TreeInput> = rows
            .iter()
            .map(|r| TreeInput {
                session: r.session.clone(),
                agent_id: r.agent_id.clone(),
                agent_role: r.agent_role.clone(),
                parent_agent_id: r.parent_agent_id.clone(),
                helper_state: r.helper_state.clone(),
            })
            .collect();
        let filters: Vec<FilterInput> = rows
            .iter()
            .map(|r| FilterInput {
                session: r.session.clone(),
                unread: r.unread,
                name: r.name.clone(),
                backend: r.backend.clone(),
                last_message: r.last_message.clone(),
                working_directory: r.working_directory.clone(),
            })
            .collect();
        let mut sidebar = self.sidebar.borrow_mut();
        sidebar.query = window.get_query().to_string();
        sidebar.unread_only = window.get_scope() == "unread";
        laps.lap("inputs");
        sidebar.rebuild(&trees);
        laps.lap("sidebar rebuild");
        let selected = engine.selected_session();
        let tree_active = sidebar.tree_active();
        let visible: Vec<&clarp_core::roster::AgentRow> =
            sidebar.visible(&trees, &filters).into_iter().filter_map(|session| rows.iter().find(|r| r.session == session)).collect();
        laps.lap("visible");
        // Sub-agents fold under their chat unless it is unfolded; the open
        // chat's own ancestors stay open so it is always in the list.
        let parent_of = |row: &clarp_core::roster::AgentRow| -> Option<&clarp_core::roster::AgentRow> {
            if row.agent_role != "helper" || row.parent_agent_id.is_empty() {
                return None;
            }
            rows.iter().find(|r| r.agent_id == row.parent_agent_id && r.session != row.session)
        };
        let ancestors = |row: &clarp_core::roster::AgentRow| -> Vec<String> {
            let (mut chain, mut current) = (Vec::new(), parent_of(row));
            while let Some(parent) = current {
                if chain.contains(&parent.session) {
                    break;
                }
                chain.push(parent.session.clone());
                current = parent_of(parent);
            }
            chain
        };
        let mut unfolded = self.unfolded.borrow().clone();
        if let Some(open) = rows.iter().find(|r| r.session == selected) {
            unfolded.extend(ancestors(open));
        }
        let queue = engine.attention_queue();
        laps.lap("queue");
        let mut folded_under: std::collections::HashMap<String, i32> = std::collections::HashMap::new();
        // Of those, the helpers still at work.
        let mut working_under: std::collections::HashMap<String, i32> = std::collections::HashMap::new();
        let mut shown = Vec::new();
        for row in &visible {
            let chain = if tree_active { ancestors(row) } else { Vec::new() };
            if let Some(first) = chain.first() {
                *folded_under.entry(first.clone()).or_default() += 1;
                if !clarp_core::sidebar::finished_helper_state(&row.helper_state) {
                    *working_under.entry(first.clone()).or_default() += 1;
                }
            }
            if chain.iter().all(|a| unfolded.contains(a)) {
                shown.push(*row);
            }
        }
        laps.lap("folding");
        let chats = shown
            .into_iter()
            .map(|row| {
                let mut chat = chat_row(row, sidebar.depth(&row.session), selected);
                chat.fold_count = folded_under.get(&row.session).copied().unwrap_or(0);
                (chat.running_helpers, chat.running_processes) = view::running_work(&chat, working_under.get(&row.session).copied().unwrap_or(0));
                chat.queue = queue.iter().position(|s| *s == row.session).map_or(0, |i| i as i32 + 1);
                if row.busy {
                    let letters: Vec<slint::SharedString> = row.name.chars().map(|c| c.to_string().into()).collect();
                    chat.letters = ModelRc::new(VecModel::from(letters));
                }
                chat.folded = !unfolded.contains(&row.session);
                if let Some(line) = sidebar.footers(&row.session).first().filter(|_| !chat.folded) {
                    chat.done_parent = line.parent_agent_id.clone().into();
                    chat.done_count = line.count as i32;
                    chat.done_expanded = line.expanded;
                }
                chat
            })
            .collect();
        laps.lap("rows");
        laps.done("chat rows");
        chats
    }

    /// Folds (`Some(false)`), unfolds (`Some(true)`) or toggles (`None`) the
    /// sub-agents under `session`.
    pub fn fold(&self, session: &str, open: Option<bool>) {
        let mut unfolded = self.unfolded.borrow_mut();
        let open = open.unwrap_or(!unfolded.contains(session));
        let changed = if open { unfolded.insert(session.to_owned()) } else { unfolded.remove(session) };
        drop(unfolded);
        if changed {
            self.refresh(&[Change::Roster]);
        }
    }

    /// Portraits for the chat list: an agent's own, a pair room's two
    /// agents' in one (from `pair:<agent>:<agent>`), at the explorer's size.
    fn with_portraits(&self, window: &AppWindow, mut rows: Vec<ChatRow>) -> Vec<ChatRow> {
        let sizes = avatar_view::sizes(self);
        let logical = if window.get_explorer_compact() { sizes.compact } else { sizes.row };
        for row in &mut rows {
            let session = row.session.to_string();
            if let Some(pair) = session.strip_prefix("pair:") {
                let sessions: Vec<String> = {
                    let engine = self.engine.borrow();
                    pair.split(':').filter_map(|id| engine.roster().find_by_agent_id(id).map(|a| a.session.clone())).collect()
                };
                if let [first, second] = sessions.as_slice() {
                    row.portrait = avatar_view::pair_portrait(self, first, second, logical);
                }
            } else {
                row.portrait = avatar_view::portrait(self, &session, logical);
            }
        }
        rows
    }

    fn refresh(&self, changes: &[Change]) {
        let Some(window) = self.window.upgrade() else { return };
        let mut whole = perf::Laps::new();
        let engine = self.engine.borrow();
        if changes.contains(&Change::Preferences) {
            view::apply_app_theme(self, &window);
            look::apply(self, &window);
        }
        let selected = engine.selected_session().to_owned();
        if changes.contains(&Change::Selection) && !selected.is_empty() {
            let mut recent = self.recent.borrow_mut();
            recent.retain(|s| *s != selected);
            recent.insert(0, selected.clone());
        }
        let mut list_changed = changes.iter().any(|c| matches!(c, Change::Roster | Change::Selection | Change::Rooms | Change::Archive | Change::Avatars | Change::Updates));
        drop(engine);
        // New portraits alone: the list changes once they are decoded, at
        // most every 100 ms.
        let portraits_only = changes.iter().all(|c| !matches!(c, Change::Roster | Change::Selection | Change::Rooms | Change::Archive | Change::Updates));
        if list_changed && portraits_only && !avatar_view::portraits_due(self, window.get_explorer_compact()) {
            list_changed = false;
        }
        if list_changed {
            let started = std::time::Instant::now();
            let mut laps = perf::Laps::new();
            let chats = self.chat_rows(&self.engine.borrow(), &window);
            laps.lap("chat rows");
            let chats = self.with_portraits(&window, chats);
            laps.lap("portraits");
            window.set_explorer_compact_width(compact_width(&window, &chats, avatar_view::sizes(self).compact));
            laps.lap("compact width");
            sync_rows(&self.chats, chats);
            laps.lap("set");
            let engine = self.engine.borrow();
            let rooms: Vec<ChatRow> = engine
                .rooms()
                .iter()
                .filter_map(|room| room.as_object())
                .map(|room| {
                    let session = clarp_core::json::string(room, "conversation_id");
                    let title = engine.chat_name(&session);
                    ChatRow {
                        initial: "↔".into(),
                        name: title.into(),
                        stamp: String::new().into(),
                        preview: clarp_core::json::string(room, "preview").into(),
                        unread: room.get("unread").and_then(|v| v.as_bool()).unwrap_or(false),
                        selected: session == selected,
                        session: session.into(),
                        ..ChatRow::default()
                    }
                })
                .collect();
            let archived: Vec<ChatRow> = engine
                .archived()
                .rows()
                .iter()
                .map(|row| ChatRow { archived: true, ..chat_row(row, 0, &selected) })
                .collect();
            window.set_unread_rooms(engine.unread_rooms() as i32);
            drop(engine);
            let rooms = self.with_portraits(&window, rooms);
            sync_rows(&self.rooms, rooms);
            let archived = self.with_portraits(&window, archived);
            sync_rows(&self.archived, archived);
            laps.lap("rooms and archive");
            laps.done("list");
            perf::rebuilt(started, format!("{} rows; {}", self.chats.row_count(), change_names(changes)));
            self.startup_milestones();
        }
        whole.lap("list");
        self.refresh_panes(&window, changes);
        whole.lap("panes");
        // A chat opens ready to type into, unless the reader is scrolling.
        if changes.contains(&Change::Selection) && !self.active_report().transcript_focused && !self.previewing.get() {
            self.focus_composer();
        }
        let engine = self.engine.borrow();
        window.set_selected_name(if selected.is_empty() { String::new() } else { engine.chat_name(&selected) }.into());
        window.set_connection(engine.connection_state().into());
        window.set_base_url(engine.base_url().into());
        window.set_connecting(matches!(engine.connection_state(), "connecting" | "pairing"));
        window.set_stored_credential(engine.has_stored_credential());
        window.set_has_agents(!engine.roster().agents().is_empty());
        // No Host and no agents: the connection page asks for one (as the
        // Qt app does), and goes once the Host answers.
        let state = engine.connection_state().to_owned();
        let empty = engine.roster().agents().is_empty();
        let overlay = self.overlay.borrow().clone();
        if overlay.is_empty() && empty && matches!(state.as_str(), "offline" | "unauthorized") && changes.contains(&Change::Connection) {
            commands::open_overlay(self, &window, "connection");
            window.invoke_open_connection_page();
        } else if overlay == "connection" && state == "live" && !empty && changes.contains(&Change::Roster) {
            commands::close_overlay(self, &window);
        }
        window.set_muted(engine.muted());
        let name = engine.server_name();
        window.set_server_initial(initial(if name.is_empty() { "C" } else { name }));
        let conversation_error = engine.conversation(&selected).map(|c| {
            if c.error().is_empty() { c.voice_error().to_owned() } else { c.error().to_owned() }
        });
        let error = if engine.error().is_empty() { conversation_error.unwrap_or_default() } else { engine.error().to_owned() };
        window.set_error(SharedString::from(error));
        drop(engine);
        // Live items say when the agent works: the bar names Stop.
        if changes.iter().any(|c| matches!(c, Change::Selection | Change::Panes | Change::Composer(_)) || matches!(c, Change::Live(s) if *s == selected)) {
            commands::show_hints(self, &window);
        }
        whole.lap("status");
        if changes.iter().any(|c| matches!(c, Change::Roster | Change::Preferences | Change::Search)) {
            commands::refresh_switcher(self, &window);
        }
        whole.lap("switcher");
        self.voice(changes);
        if changes.iter().any(|c| matches!(c, Change::HostStatus | Change::Preferences | Change::Connection | Change::ServerInfo)) {
            settings_view::show(self, &window);
        }
        // ---- updates and teams
        whole.lap("voice and settings");
        updates_view::refresh(self, &window, changes);
        whole.lap("updates");
        teams_view::refresh(self, &window, changes);
        whole.lap("teams");
        if changes.contains(&Change::Roster) {
            commands::show_hints(self, &window);
        }
        // ---- launch dialogs
        if let Some(app) = app() {
            launch_view::refresh(&app, &window, changes);
            agent_dialogs_view::refresh(&app, &window, changes);
        }
        whole.lap("launch");
        // ---- profile and overview
        profile_view::refresh(self, &window, changes);
        whole.lap("profile");
        whole.done("refresh");
        if self.active_messages().is_some_and(|m| m.row_count() > 0) {
            perf::reached(|s| &mut s.first_chat, "first chat");
        }
    }

    /// The `perf` milestones the chat list reached: the roster, every agent
    /// listed (helpers under their chat), every portrait shown.
    fn startup_milestones(&self) {
        let engine = self.engine.borrow();
        let agents = engine.roster().agents();
        if agents.is_empty() {
            return;
        }
        perf::reached(|s| &mut s.snapshot, "roster");
        let listed: std::collections::HashMap<String, bool> =
            self.chats.iter().map(|c| (c.session.to_string(), c.portrait.size().width > 0)).collect();
        if agents.iter().filter(|a| a.role != "helper").all(|a| listed.contains_key(&a.session)) {
            perf::reached(|s| &mut s.explorer_complete, "explorer complete");
        }
        let with_portrait = |a: &&clarp_core::protocol::Agent| {
            !clarp_core::media::avatar_url(&a.avatar_url, clarp_core::protocol::display_name(a)).is_empty()
        };
        if agents.iter().filter(with_portrait).all(|a| listed.get(&a.session).is_none_or(|shown| *shown)) {
            perf::reached(|s| &mut s.portraits_complete, "portraits complete");
        }
    }
}

/// Puts `rows` in `model`, changing only the rows that differ: replacing
/// the whole list makes the view build every row again. A busy row keeps
/// its shimmer letters while its name stays.
fn sync_rows(model: &VecModel<ChatRow>, mut rows: Vec<ChatRow>) {
    for (index, row) in rows.iter_mut().enumerate() {
        let Some(old) = model.row_data(index) else { break };
        if old.session == row.session && old.name == row.name && old.letters.row_count() > 0 && row.letters.row_count() > 0 {
            row.letters = old.letters.clone();
        }
        if old != *row {
            model.set_row_data(index, row.clone());
        }
    }
    let shared = model.row_count().min(rows.len());
    for _ in shared..model.row_count() {
        model.remove(model.row_count() - 1);
    }
    for row in rows.into_iter().skip(shared) {
        model.push(row);
    }
}

/// `Roster, Updates, Conversation` for a perf log line.
fn change_names(changes: &[Change]) -> String {
    let names: Vec<String> = changes.iter().map(|c| format!("{c:?}").split(['(', ' ', '{']).next().unwrap_or_default().to_owned()).collect();
    names.join(", ")
}

impl App {
    /// Engine changes the voice follows: clips to play, the Host, mute,
    /// and a sent message silencing speech.
    fn voice(&self, changes: &[Change]) {
        for change in changes {
            match change {
                Change::Clips(clips) => {
                    for clip in clips {
                        platform::audio::with(|audio| audio.enqueue_clip(clip.clone()));
                    }
                }
                Change::Silence => {
                    platform::audio::with(platform::audio::Audio::silence);
                }
                Change::Endpoint => {
                    let endpoint = self.engine.borrow().endpoint();
                    platform::audio::with(|audio| match &endpoint {
                        Some((url, token)) => audio.set_endpoint(Some(url.clone()), token),
                        None => audio.set_endpoint(None, ""),
                    });
                }
                Change::Preferences => {
                    let muted = self.engine.borrow().muted();
                    platform::audio::with(|audio| audio.set_muted(muted));
                    platform::desktop::muted_changed(muted);
                }
                Change::Notification { title, body } => {
                    let (on, sound) = {
                        let engine = self.engine.borrow();
                        (clarp_core::prefs::flag(engine.settings(), "notifyreplies"), clarp_core::prefs::flag(engine.settings(), "notifysound"))
                    };
                    if on {
                        platform::desktop::notify(title.clone(), body.clone(), sound);
                    }
                }
                _ => {}
            }
        }
    }
}

/// What the voice asks of the window (`platform::audio::Notice`).
pub fn audio_notices(notices: Vec<platform::audio::Notice>) {
    use platform::audio::Notice;
    let Some(app) = app() else { return };
    for notice in notices {
        match notice {
            Notice::Muted(muted) => {
                if app.engine.borrow().muted() != muted {
                    app.engine.borrow_mut().set_muted(muted);
                }
            }
            Notice::Error(message) => app.engine.borrow_mut().report_error(&message),
            Notice::Transcribed { text, trace, transcription, hands_free, target } => {
                app.engine.borrow_mut().send_dictation(&text, &trace, &transcription, hands_free, &target);
            }
            Notice::Changed => {
                app.voice_state();
                artifacts_view::media_changed(&app);
                platform::publish_playback();
            }
        }
    }
    pump_now(&app);
}

/// Applies what the engine has queued; scheduled on the UI thread by wake.
pub fn pump() {
    platform::diagnostics::awake();
    let Some(app) = app() else { return };
    let started = std::time::Instant::now();
    let changes = app.engine.borrow_mut().pump();
    if !changes.is_empty() {
        let engine = started.elapsed();
        app.refresh(&changes);
        perf::woke(started, format!("engine {:.1} ms; {}", perf::ms(engine), change_names(&changes)));
    }
}

fn main() {
    perf::launched();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let e2e_out = args.iter().position(|a| a == "--e2e-out").and_then(|i| args.get(i + 1)).cloned();
    let arg = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let shot = arg("--shot");
    let check = arg("--check");
    let headless = e2e_out.is_some() || shot.is_some() || check.is_some() || args.iter().any(|a| a == "--headless");
    let options = launch::options(&launch_arguments(&args));
    // Checks run alone; a desktop launch shares one process per desktop.
    let socket = if headless && std::env::var_os("CLARP_TEST_INSTANCE").is_none() { None } else { launch::forward_or_socket(&launch_arguments(&args)) };
    if headless && let Err(error) = headless::install(1280, 800, 1.0) {
        eprintln!("clarp-slint: {error}");
        std::process::exit(1);
    }
    if options.preview_versions {
        preview_view::run_manager();
    }
    let window = match AppWindow::new() {
        Ok(window) => window,
        Err(error) => {
            eprintln!("clarp-slint: cannot open the window: {error}");
            std::process::exit(1);
        }
    };
    let settings = settings();
    let theme = std::env::args()
        .skip_while(|a| a != "--theme")
        .nth(1)
        .unwrap_or_else(|| settings.string("appearance/readingTheme", clarp_core::reading_theme::default_theme_id()));
    // The reader's own font for the theme (Settings → Font).
    let chosen = clarp_core::reading_theme::font_override(
        settings.get(clarp_core::reading_theme::FONT_OVERRIDES_KEY),
        &clarp_core::reading_theme::normalized_theme_id(&theme),
    );
    apply_theme(&window, &theme, chosen.as_ref());
    view::load_fonts(theme.clone(), chosen);
    window.set_minimal_ui(settings.boolean("appearance/minimalUi", false));
    window.set_nav_rail_visible(settings.boolean("appearance/navRail", true));
    window.set_explorer_compact(settings.boolean("explorer/compact", false));
    window.set_shortcuts_visible(settings.boolean("appearance/shortcutsVisible", true));
    // The reader's interface scale (1.15 as in the Qt app); checks draw at 1.0.
    let ui_scale = if headless { 1.0 } else { settings.get("appearance/uiScale").and_then(serde_json::Value::as_f64).unwrap_or(1.15) as f32 };
    let prefs = Prefs::load(&settings);
    let engine = match Engine::new(Config::from_env(settings), || {
        if let Err(error) = slint::invoke_from_event_loop(pump) {
            eprintln!("clarp-slint: dropped an engine wake: {error}");
        }
    }) {
        Ok(engine) => engine,
        Err(error) => {
            eprintln!("clarp-slint: {error}");
            std::process::exit(1);
        }
    };
    let chats = Rc::new(VecModel::<ChatRow>::default());
    let rooms = Rc::new(VecModel::<ChatRow>::default());
    let archived = Rc::new(VecModel::<ChatRow>::default());
    let pane_views = Rc::new(VecModel::<PaneView>::default());
    window.set_chats(ModelRc::from(chats.clone()));
    window.set_rooms(ModelRc::from(rooms.clone()));
    window.set_archived(ModelRc::from(archived.clone()));
    window.set_panes(ModelRc::from(pane_views.clone()));
    let state = Rc::new(App {
        engine: RefCell::new(engine),
        window: window.as_weak(),
        chats,
        rooms,
        archived,
        panes: pane_views,
        pane_state: RefCell::new(Vec::new()),
        sidebar: RefCell::new(clarp_core::sidebar::Sidebar::default()),
        unfolded: RefCell::new(std::collections::HashSet::new()),
        recent: RefCell::new(Vec::new()),
        previewing: std::cell::Cell::new(false),
        workspaces: RefCell::new(clarp_core::workspace::WorkspaceContext::default()),
        prefs: RefCell::new(prefs),
        switcher: RefCell::new(SwitcherState::default()),
        overlay: RefCell::new(String::new()),
        launch: RefCell::new(None),
        artifact_cursor: RefCell::new(String::new()),
        artifact_choices: RefCell::default(),
        artifact_drafts: RefCell::default(),
        artifact_editing: RefCell::default(),
        artifact_seen_pending: RefCell::default(),
        expanded: RefCell::new(std::collections::HashSet::new()),
    });
    APP.with(|a| *a.borrow_mut() = Some(state.clone()));
    look::wire(&state, &window);

    window.on_fold_toggled(|session| with_window(|app, _| app.fold(&session, None)));
    window.on_done_helpers_toggled(|parent| with_window(|app, _| {
        if app.sidebar.borrow_mut().toggle_done_helpers(&parent) {
            app.refresh(&[Change::Roster]);
        }
    }));
    window.on_restore_agent(|session| with_window(|app, _| {
        app.engine.borrow_mut().set_agent_archived(&session, false);
        pump_now(app);
    }));
    window.on_chat_chosen(|session| {
        if let Some(app) = app() {
            app.engine.borrow_mut().select(&session);
            pump_now(&app);
        }
    });
    window.on_send(|pane, text, queue| {
        if let Some(app) = app() {
            let session = app.session_of(&pane);
            // A draft naming another agent goes to that agent's chat, which
            // then opens with it.
            let route = mention_view::route(&mention_view::candidates(&app.engine.borrow()), &session, &text);
            let target = route.as_ref().map_or(session.as_str(), |c| c.session.as_str()).to_owned();
            let sent = app.engine.borrow_mut().send_composer_to(&session, &target, &text, queue);
            if sent {
                app.replace_draft(&session, "", None);
                if target != session {
                    app.engine.borrow_mut().select(&target);
                }
                // Your own message always brings the latest into view.
                app.to_latest();
            }
            pump_now(&app);
        }
    });
    {
        let bridge = window.global::<MentionBridge>();
        bridge.on_scan(|pane, text, cursor| with_window(|app, _| mention_view::scan(app, &pane, &text, cursor)));
        bridge.on_select(|pane, index| with_window(|app, _| mention_view::moved(app, &pane, index)));
        bridge.on_accept(|pane, index| with_window(|app, _| mention_view::accept(app, &pane, index)));
        bridge.on_dismiss(|pane| with_window(|app, _| mention_view::dismiss(app, &pane)));
    }
    window.on_draft_edited(|pane, text| {
        if let Some(app) = app() {
            app.draft_edited(&pane, &text);
        }
    });
    window.on_remove_attachment(|pane, id| {
        if let Some(app) = app() {
            let session = app.session_of(&pane);
            app.engine.borrow_mut().remove_attachment(&session, &id);
            pump_now(&app);
        }
    });
    window.on_attach(|pane| {
        if let Some(app) = app() {
            view::choose_attachment(app.session_of(&pane));
        }
    });
    window.on_stop(|pane| {
        if let Some(app) = app() {
            let session = app.session_of(&pane);
            app.engine.borrow_mut().stop_session(&session);
        }
    });
    window.on_pane_activated(|pane| {
        if let Some(app) = app() {
            let active = app.engine.borrow().panes().active_pane_id().to_owned();
            if active != pane.as_str() {
                app.engine.borrow_mut().with_panes(|p| p.focus_pane(&pane));
                pump_now(&app);
            }
        }
    });
    window.on_copy_message(|pane, id| with_window(|app, _| {
        let session = app.session_of(&pane);
        let text = app.engine.borrow().conversation(&session).and_then(|c| {
            c.rows().iter().find(|m| m.id == id.as_str()).map(|m| match clarp_core::decision_receipt::parse(&m.text) {
                // A receipt copies as it reads, not as the Host's prompt.
                Some(receipt) => format!("{}\n{}", receipt.question, receipt.outcome.label()),
                // A reply copies its written form only.
                None if m.display_text.is_empty() && m.role == "user" => m.text.clone(),
                None => m.display_text.clone(),
            })
        });
        match text.map(|text| platform::clipboard::copy(&text)) {
            Some(Ok(())) => {}
            Some(Err(error)) => app.engine.borrow_mut().report_error(&format!("Could not copy: {error}")),
            None => eprintln!("clarp-slint: no message {id} to copy"),
        }
    }));
    window.on_paste_image(|pane| {
        let Some(app) = app() else { return false };
        let session = app.session_of(&pane);
        let pasted = platform::clipboard::paste_image(&mut app.engine.borrow_mut(), &session);
        if pasted {
            pump_now(&app);
        }
        pasted
    });
    window.on_silence(|| {
        platform::audio::with(platform::audio::Audio::silence);
    });
    window.on_cancel_transcription(|pane| with_window(|app, _| {
        let session = app.session_of(&pane);
        platform::audio::with(|audio| audio.cancel_transcriptions_for_session(&session));
    }));
    let book = window.global::<ScrollBook>();
    book.on_measured(|pane, batch, id, height, width| {
        if let Some(app) = app() {
            app.row_measured(&pane, batch, &id, height, width);
        }
    });
    book.on_shown(|pane, id, height, width| {
        if let Some(app) = app() {
            app.row_shown(&pane, &id, height, width);
        }
    });
    book.on_relayout(|pane| {
        if let Some(app) = app() {
            app.relayout(&pane);
        }
    });
    book.on_row_at(|pane, y| app().map_or(0, |app| app.row_at(&pane, y)));
    window.on_scroll_journal(|pane, cause, old, new, old_place, new_place, content, viewport, at_end, follow, rows| {
        scroll_journal::record(scroll_journal::Move { pane: pane.into(), cause: cause.into(), old, new, old_place, new_place, content, viewport, at_end, follow, rows });
    });
    window.on_pane_reported(|pane, follows, at_end, offset, transcript, composer| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            let report = panes::Report { follows, at_end, offset, transcript_focused: transcript, composer_focused: composer };
            app.reported(&pane, report);
            link_hints::reported(&window, &pane, offset, &app.active_id());
            commands::show_hints(&app, &window);
        }
    });
    window.on_focus_moved(|| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            commands::show_hints(&app, &window);
        }
    });
    window.on_shortcut(|text, control, alt, shift, meta, repeat| commands::shortcut(&text, control, alt, shift, meta, repeat));
    window.on_setting_changed(|id, delta| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            settings_view::change(&app, &window, &id, delta);
        }
    });
    window.on_settings_searched(|text| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            settings_view::searched(&app, &window, &text);
        }
    });
    window.on_setting_reset(|id, section| with_window(|app, window| settings_view::reset(app, window, &id, section)));
    window.on_surface_chosen(|surface| {
        if let (Some(app), Some(window)) = (app(), crate::window())
            && !commands::run(&app, &window, &surface)
        {
            eprintln!("clarp-slint: the {surface} surface is not available yet");
        }
    });
    window.on_run_command(|action| {
        if let (Some(app), Some(window)) = (app(), crate::window())
            && !commands::run(&app, &window, &action)
        {
            eprintln!("clarp-slint: {action} is not available yet");
        }
    });
    window.on_keymap_clicked(|action| with_window(|app, window| keymap_view::clicked(app, window, &action)));
    window.on_keymap_import(|text| with_window(|app, window| commands::keymap_import(app, window, &text)));
    window.on_keymap_export(|| with_window(|app, window| commands::keymap_export(app, window)));
    window.on_keymap_reset(|| with_window(|app, window| commands::keymap_import(app, window, &keymap::export(&keymap::Overrides::new()))));
    window.on_connect_host(|url, token| with_window(|app, _| {
        app.engine.borrow_mut().connect_to_server(&url, &token);
        pump_now(app);
    }));
    window.on_pair_host(|url, code| with_window(|app, _| {
        app.engine.borrow_mut().pair_device(&url, &code);
        pump_now(app);
    }));
    window.on_forget_host(|| with_window(|app, _| {
        app.engine.borrow_mut().forget_credential();
        pump_now(app);
    }));
    window.on_overlay_closed(|| with_window(|app, window| commands::close_overlay(app, window)));
    window.on_open_switcher(|| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            commands::open_switcher(&app, &window);
        }
    });
    window.on_switcher_edited(|text| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            app.switcher.borrow_mut().query = text.to_string();
            commands::refresh_switcher(&app, &window);
        }
    });
    window.on_switcher_moved(|index| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            commands::switcher_moved(&app, &window, index);
        }
    });
    window.on_switcher_chosen(|index| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            commands::switcher_chosen(&app, &window, index);
        }
    });
    window.on_switcher_adjusted(|index, delta| with_window(|app, window| commands::switcher_adjusted(app, window, index, delta)));
    window.on_switcher_dismissed(|| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            commands::close_switcher(&app, &window, None);
        }
    });
    window.on_switch_workspace(|id| {
        if let Some(app) = app() {
            app.engine.borrow_mut().with_panes(|p| p.switch_workspace(&id));
            pump_now(&app);
        }
    });
    window.on_create_workspace(|| {
        if let Some(app) = app() {
            let count = app.engine.borrow().panes().workspaces().len();
            app.engine.borrow_mut().with_panes(|p| p.create_workspace(&format!("Workspace {}", count + 1)));
            pump_now(&app);
        }
    });
    window.on_save_layout_instead(|| with_window(|app, window| {
        commands::run(app, window, "keep-layout");
    }));
    window.on_dismiss_save_warning(|| with_window(|app, window| {
        commands::run(app, window, "dismiss-layout-warning");
    }));
    window.on_resize_split(|id, ratio| {
        if let Some(app) = app() {
            app.engine.borrow_mut().with_panes(|p| p.set_split_ratio(&id, f64::from(ratio)));
            pump_now(&app);
        }
    });
    window.on_balance(|| {
        if let Some(app) = app() {
            app.engine.borrow_mut().with_panes(|p| p.equalize());
            pump_now(&app);
        }
    });
    window.on_filter_changed(|| {
        if let Some(app) = app() {
            app.refresh(&[Change::Roster]);
        }
    });
    window.on_toggle_muted(|| {
        if let Some(app) = app() {
            let muted = app.engine.borrow().muted();
            app.engine.borrow_mut().set_muted(!muted);
            pump_now(&app);
        }
    });
    window.on_link_clicked(|url| open_link(&url));
    window.on_load_older(|pane| {
        if let Some(app) = app() {
            let session = app.session_of(&pane);
            app.engine.borrow_mut().load_older(&session);
            pump_now(&app);
        }
    });
    window.on_toggle_activity(|_pane, id, group| {
        if let Some(app) = app() {
            if group.is_empty() {
                let id = id.to_string();
                let mut expanded = app.expanded.borrow_mut();
                if !expanded.remove(&id) {
                    expanded.insert(id);
                }
            } else {
                app.engine.borrow_mut().toggle_group(&group);
            }
            pump_now(&app);
        }
    });
    // ---- updates and teams
    window.on_updates_refresh(|| with_window(|app, _| {
        app.engine.borrow_mut().load_updates();
        pump_now(app);
    }));
    window.on_resolve_decision(|id, choice, revision| with_window(|app, _| {
        app.engine.borrow_mut().resolve_decision(&id, &choice, i64::from(revision));
        pump_now(app);
    }));
    window.on_cancel_job(|id| with_window(|app, _| {
        app.engine.borrow_mut().cancel_background_job(&id);
        pump_now(app);
    }));
    window.on_open_report(|id| with_window(|app, window| updates_view::open_report(app, window, &id)));
    window.on_report_link(updates_view::open_report_link);
    window.on_image_view_moved(|delta| with_window(|app, window| artifacts_view::image_view_moved(app, window, delta)));
    window.on_open_chat(|session| with_window(|app, window| updates_view::open_chat(app, window, &session)));
    window.on_show_processes(|session, x, y| with_window(|app, window| updates_view::open_processes(app, window, &session, x, y)));
    window.on_process_helper_opened(|session| with_window(|app, window| updates_view::open_helper(app, window, &session)));
    window.on_team_action(|action, argument| with_window(|app, window| teams_view::action(app, window, &action, &argument)));
    artifacts_view::bind(&window);
    window.on_team_created(|name| with_window(|app, window| teams_view::created(app, window, &name)));
    window.on_team_saved(|name, colour, leader| with_window(|app, window| teams_view::saved(app, window, &name, &colour, leader)));
    window.on_team_member_added(|index| with_window(|app, window| teams_view::member_added(app, window, index)));
    window.on_team_deleted(|| with_window(|app, window| teams_view::deleted(app, window)));
    // ---- profile and overview
    profile_view::wire(&window);
    overview_view::wire(&window);
    voice_view::wire(&window);
    font_view::wire(&window);
    orchestrator_view::wire(&window);
    commands::refresh_keys(&state, &window);
    window.on_dismiss_error(|| with_window(|app, window| {
        commands::run(app, window, "dismiss-error");
    }));
    // ---- launch dialogs
    launch_view::wire(&window);
    agent_dialogs_view::wire(&window);

    // `--theme` is the reading theme from now on, like choosing it.
    if arg("--theme").is_some() && state.engine.borrow().reading_theme() != theme {
        state.engine.borrow_mut().set_reading_theme(&theme);
        state.engine.borrow_mut().pump();
    }
    platform::runtime::set(state.engine.borrow().runtime());
    let muted = state.engine.borrow().muted();
    platform::audio::start(muted);
    platform::serve_mpris();
    platform::desktop::start();
    avatar_view::prewarm_portraits();
    avatar_view::apply(&state, &window);
    preview_view::start(false);
    preview_view::wire(&window);
    preview_view::show();
    platform::desktop::set_ui_scale(ui_scale);
    // The window system's window exists once the loop runs: scale it then.
    slint::Timer::single_shot(std::time::Duration::from_millis(50), move || platform::desktop::set_ui_scale(ui_scale));
    platform::diagnostics::start(headless);
    if let Some(socket) = &socket {
        launch::listen(socket);
    }
    state.engine.borrow_mut().start();
    let setting = state.engine.borrow().settings().boolean("launch/newAgentOnStartup", false);
    if options.launch_on_startup(false, setting, headless) {
        launch::start(&state, &window, launch::Request::from_options(&options));
    }
    drop(state);
    if let Some(out) = e2e_out {
        driver::start(out);
    } else if let Some(name) = check {
        driver::start_check(&name, arg("--out").unwrap_or_else(|| ".".into()));
    } else if let Some(path) = shot {
        driver::start_shot(path, arg("--select").unwrap_or_default());
    }
    if let Err(error) = window.run() {
        eprintln!("clarp-slint: {error}");
        std::process::exit(1);
    }
    platform::audio::stop();
    // exit() runs no destructors: save drafts and the layout first.
    if let Some(app) = app() {
        app.engine.borrow_mut().shutdown();
    }
    std::process::exit(driver::exit_code());
}

/// The launch options, without the check harness's own flags.
fn launch_arguments(args: &[String]) -> Vec<String> {
    const WITH_VALUE: [&str; 6] = ["--e2e-out", "--shot", "--select", "--check", "--out", "--theme"];
    let mut rest = Vec::new();
    let mut arguments = args.iter();
    while let Some(argument) = arguments.next() {
        if WITH_VALUE.contains(&argument.as_str()) {
            arguments.next();
        } else if argument != "--headless" {
            rest.push(argument.clone());
        }
    }
    rest
}

/// Runs `act` with the app and its window, when both are there.
fn with_window(act: impl FnOnce(&Rc<App>, &AppWindow)) {
    if let (Some(app), Some(window)) = (app(), window()) {
        act(&app, &window);
    }
}

/// Commands change state synchronously (an optimistic row, a selection):
/// show it without waiting for the next wake.
pub fn pump_now(app: &Rc<App>) {
    let started = std::time::Instant::now();
    let mut changes = app.engine.borrow_mut().pump();
    let sessions: Vec<String> = app.pane_state.borrow().iter().map(|p| p.session.clone()).collect();
    changes.extend(sessions.into_iter().map(Change::Conversation));
    app.refresh(&changes);
    perf::woke(started, format!("command: {}", change_names(&changes)));
}
