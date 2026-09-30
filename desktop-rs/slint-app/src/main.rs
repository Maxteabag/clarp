//! The Clarp desktop in Slint: a thin view over `clarp-engine`. The engine
//! owns behaviour; this file turns its state into Slint models and the
//! window's actions into engine commands.
//!
//! `clarp-slint` opens the window. `--headless` runs it without a display on
//! Slint's software renderer (for checks), and `--e2e-out DIR` drives it like
//! a person would, saving a screenshot per stage (see `driver.rs`).

mod commands;
mod driver;
mod headless;
mod keymap;
mod launch;
mod panes;
mod platform;
mod settings_view;
mod switcher;
mod view;
// ---- updates and teams
mod teams_view;
mod updates_view;

use view::{apply_theme, chat_row, initial, open_link};

use std::cell::RefCell;
use std::rc::Rc;

use clarp_core::settings::Settings;
use clarp_engine::{Change, Config, Engine};
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

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
    pub prefs: RefCell<Prefs>,
    pub switcher: RefCell<SwitcherState>,
    /// The dialog over the window ("" for none).
    pub overlay: RefCell<String>,
    /// An agent the launch asked for, until the window starts it.
    pub launch: RefCell<Option<launch::Request>>,
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

impl App {
    /// The chat list, through the same search/scope/nesting rules as the
    /// Qt sidebar (`clarp_core::sidebar`).
    fn chat_rows(&self, engine: &Engine, window: &AppWindow) -> Vec<ChatRow> {
        use clarp_core::sidebar::{FilterInput, TreeInput};
        let rows = engine.roster().rows();
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
        sidebar.rebuild(&trees);
        let selected = engine.selected_session();
        sidebar
            .visible(&trees, &filters)
            .into_iter()
            .filter_map(|session| rows.iter().find(|r| r.session == session))
            .map(|row| chat_row(row, sidebar.depth(&row.session), selected))
            .collect()
    }

    fn refresh(&self, changes: &[Change]) {
        let Some(window) = self.window.upgrade() else { return };
        let engine = self.engine.borrow();
        if changes.contains(&Change::Preferences) {
            apply_theme(&window, &engine.reading_theme());
        }
        let selected = engine.selected_session().to_owned();
        let list_changed = changes.iter().any(|c| matches!(c, Change::Roster | Change::Selection | Change::Rooms | Change::Archive));
        if list_changed {
            self.chats.set_vec(self.chat_rows(&engine, &window));
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
            self.rooms.set_vec(rooms);
            let archived: Vec<ChatRow> = engine.archived().rows().iter().map(|row| chat_row(row, 0, &selected)).collect();
            self.archived.set_vec(archived);
            window.set_unread_rooms(engine.unread_rooms() as i32);
        }
        drop(engine);
        self.refresh_panes(&window, changes);
        // A chat opens ready to type into, unless the reader is scrolling.
        if changes.contains(&Change::Selection) && !self.active_report().transcript_focused {
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
        if changes.iter().any(|c| matches!(c, Change::Selection | Change::Panes | Change::Composer(_))) {
            commands::show_hints(self, &window);
        }
        if changes.iter().any(|c| matches!(c, Change::Roster | Change::Preferences)) {
            commands::refresh_switcher(self, &window);
        }
        self.voice(changes);
        if changes.iter().any(|c| matches!(c, Change::HostStatus | Change::Preferences | Change::Connection | Change::ServerInfo)) {
            settings_view::show(self, &window);
        }
        // ---- updates and teams
        updates_view::refresh(self, &window, changes);
        teams_view::refresh(self, &window, changes);
        if changes.contains(&Change::Roster) {
            commands::show_hints(self, &window);
        }
    }
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
                Change::Notification { title, body } => platform::desktop::notify(title.clone(), body.clone()),
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
    let changes = app.engine.borrow_mut().pump();
    if !changes.is_empty() {
        app.refresh(&changes);
    }
}

fn main() {
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
    apply_theme(&window, &theme);
    view::load_fonts(theme.clone());
    window.set_minimal_ui(settings.boolean("appearance/minimalUi", false));
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
        workspaces: RefCell::new(clarp_core::workspace::WorkspaceContext::default()),
        prefs: RefCell::new(prefs),
        switcher: RefCell::new(SwitcherState::default()),
        overlay: RefCell::new(String::new()),
        launch: RefCell::new(None),
        expanded: RefCell::new(std::collections::HashSet::new()),
    });
    APP.with(|a| *a.borrow_mut() = Some(state.clone()));

    window.on_chat_chosen(|session| {
        if let Some(app) = app() {
            app.engine.borrow_mut().select(&session);
            pump_now(&app);
        }
    });
    window.on_send(|pane, text, queue| {
        if let Some(app) = app() {
            let session = app.session_of(&pane);
            let sent = app.engine.borrow_mut().send_composer(&session, &text, queue);
            if sent {
                app.replace_draft(&session, "", None);
                // Your own message always brings the latest into view.
                app.to_latest();
            }
            pump_now(&app);
        }
    });
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
    window.on_pane_reported(|pane, follows, at_end, offset, transcript, composer| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            let report = panes::Report { follows, at_end, offset, transcript_focused: transcript, composer_focused: composer };
            app.reported(&pane, report);
            commands::show_hints(&app, &window);
        }
    });
    window.on_focus_moved(|| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            commands::show_hints(&app, &window);
        }
    });
    window.on_shortcut(|text, control, alt, shift| commands::shortcut(&text, control, alt, shift));
    window.on_setting_changed(|id, delta| {
        if let (Some(app), Some(window)) = (app(), crate::window()) {
            settings_view::change(&app, &window, &id, delta);
        }
    });
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
    window.on_keymap_apply(|action, key| with_window(|app, window| commands::keymap_apply(app, window, &action, &key)));
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
    window.on_save_layout_instead(|| {
        if let Some(app) = app() {
            app.engine.borrow_mut().with_panes(|p| p.save_workspace_layout_instead());
            pump_now(&app);
        }
    });
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
    window.on_open_chat(|session| with_window(|app, window| updates_view::open_chat(app, window, &session)));
    window.on_show_processes(|session, x, y| with_window(|app, window| updates_view::open_processes(app, window, &session, x, y)));
    window.on_process_helper_opened(|session| with_window(|app, window| updates_view::open_helper(app, window, &session)));
    window.on_team_action(|action, argument| with_window(|app, window| teams_view::action(app, window, &action, &argument)));
    window.on_team_created(|name| with_window(|app, window| teams_view::created(app, window, &name)));
    window.on_team_saved(|name, colour, leader| with_window(|app, window| teams_view::saved(app, window, &name, &colour, leader)));
    window.on_team_member_added(|index| with_window(|app, window| teams_view::member_added(app, window, index)));
    window.on_team_deleted(|| with_window(|app, window| teams_view::deleted(app, window)));
    window.on_dismiss_error(|| {
        if let Some(app) = app() {
            app.engine.borrow_mut().clear_error();
            pump_now(&app);
        }
    });

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
    platform::desktop::set_ui_scale(ui_scale);
    // The window system's window exists once the loop runs: scale it then.
    slint::Timer::single_shot(std::time::Duration::from_millis(50), move || platform::desktop::set_ui_scale(ui_scale));
    platform::diagnostics::start(headless);
    if let Some(socket) = &socket {
        launch::listen(socket);
    }
    state.engine.borrow_mut().start();
    let setting = state.engine.borrow().settings().boolean("launch/newAgentOnStartup", true);
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
    let mut changes = app.engine.borrow_mut().pump();
    let sessions: Vec<String> = app.pane_state.borrow().iter().map(|p| p.session.clone()).collect();
    changes.extend(sessions.into_iter().map(Change::Conversation));
    app.refresh(&changes);
}
