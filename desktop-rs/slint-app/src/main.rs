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
// Import, export and rebinding serve the keymap editor.
#[allow(dead_code)]
mod keymap;
mod panes;
mod settings_view;
mod switcher;
mod view;

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
        if changes.iter().any(|c| matches!(c, Change::HostStatus | Change::Preferences | Change::Connection | Change::ServerInfo)) {
            settings_view::show(self, &window);
        }
    }
}

/// Applies what the engine has queued; scheduled on the UI thread by wake.
pub fn pump() {
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
    state.engine.borrow_mut().start();
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
    std::process::exit(driver::exit_code());
}

/// Commands change state synchronously (an optimistic row, a selection):
/// show it without waiting for the next wake.
pub fn pump_now(app: &Rc<App>) {
    let mut changes = app.engine.borrow_mut().pump();
    let sessions: Vec<String> = app.pane_state.borrow().iter().map(|p| p.session.clone()).collect();
    changes.extend(sessions.into_iter().map(Change::Conversation));
    app.refresh(&changes);
}
