//! Starting agents: the New Session hub (NewSessionHub.qml with its
//! LaunchDirectoryPicker.qml), the named Start/Relaunch dialog
//! (StartAgentDialog.qml) and the quick switcher's contacts-only mode.
//! The hub's state lives here; the `LaunchHub` and `StartAgent` globals
//! show it and report what the person did.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use clarp_core::json;
use clarp_core::protocol::display_name;
use clarp_engine::{Change, Engine};
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::commands::{close_overlay, open_overlay};
use crate::{App, AppWindow, ContactCard, DirRow, LaunchHub, McpChoice, PastRow, ProviderChip, StartAgent, pump_now};

/// The overlay names this module owns.
pub const HUB: &str = "new-session";
pub const START: &str = "start-agent";

/// How long typing in the directory picker waits before asking the Host.
const DIRECTORY_QUERY_DELAY: Duration = Duration::from_millis(80);
/// How long typing a workspace waits before asking for folder suggestions.
const SUGGESTION_DELAY: Duration = Duration::from_millis(220);

#[derive(Debug, Clone, PartialEq, Eq)]
struct Row {
    key: String,
    name: String,
    session: String,
    subtitle: String,
    in_chat: bool,
}

#[derive(Default)]
struct Hub {
    submitting: bool,
    backend: String,
    model: String,
    effort: String,
    editing_model: bool,
    show_all: bool,
    new_contact: bool,
    choosing_directory: bool,
    directory: String,
    query: String,
    name: String,
    selected_key: String,
    /// A command-line launch waiting for the Host ("anonymous" or "contact").
    pending_launch: String,
    directory_query: String,
    directory_index: usize,
    confirm_when_ready: bool,
    focus_request: i32,
}

#[derive(Default)]
struct Start {
    replace_session: String,
    backend: String,
    model: String,
    effort: String,
    mode: String,
    past_session_id: String,
    mcp_servers: Vec<String>,
    focus_request: i32,
}

thread_local! {
    static STATE: RefCell<Hub> = RefCell::new(Hub::default());
    static START_STATE: RefCell<Start> = RefCell::new(Start::default());
    static DIRECTORY_TIMER: slint::Timer = slint::Timer::default();
    static SUGGESTION_TIMER: slint::Timer = slint::Timer::default();
}

fn hub<R>(act: impl FnOnce(&mut Hub) -> R) -> R {
    STATE.with(|s| act(&mut s.borrow_mut()))
}

fn start_state<R>(act: impl FnOnce(&mut Start) -> R) -> R {
    START_STATE.with(|s| act(&mut s.borrow_mut()))
}

fn overlay_is(app: &App, name: &str) -> bool {
    *app.overlay.borrow() == name
}

fn model<T: Clone + 'static>(rows: Vec<T>) -> ModelRc<T> {
    ModelRc::new(VecModel::from(rows))
}

fn strings(rows: impl IntoIterator<Item = String>) -> ModelRc<SharedString> {
    model(rows.into_iter().map(SharedString::from).collect())
}

fn initial(name: &str) -> SharedString {
    name.chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default().into()
}

/// `{id, label}` choices as ids and labels, without the catalog's own
/// default row: the hub puts "Server default" first instead.
fn choices(values: &[Value]) -> Vec<(String, String)> {
    values
        .iter()
        .filter_map(Value::as_object)
        .map(|o| (json::string(o, "id"), json::string(o, "label")))
        .filter(|(id, _)| !id.is_empty())
        .map(|(id, label)| (id.clone(), if label.is_empty() { id } else { label }))
        .collect()
}

fn providers(engine: &Engine) -> Vec<(String, String)> {
    choices(&engine.backend_options())
}

// ---- the hub's rows -----------------------------------------------------------

/// Idle contacts by name, then (with Show all) the chats, as in the QML.
fn rows(engine: &Engine, state: &Hub) -> Vec<Row> {
    let needle = state.query.trim().to_lowercase();
    let mut idle: Vec<Row> = engine
        .matching_contacts(&state.query)
        .iter()
        .filter_map(Value::as_object)
        .map(|c| {
            let name = json::string(c, "name");
            Row { key: format!("contact:{name}"), subtitle: json::string(c, "description"), name, session: String::new(), in_chat: false }
        })
        .collect();
    idle.sort_by_key(|r| r.name.to_lowercase());
    if !state.show_all {
        return idle;
    }
    let mut chats: Vec<Row> = engine
        .roster()
        .agents()
        .iter()
        .filter(|a| !a.janitor && (needle.is_empty() || display_name(a).to_lowercase().contains(&needle)))
        .map(|a| Row {
            key: format!("agent:{}", a.session),
            name: display_name(a).to_owned(),
            session: a.session.clone(),
            subtitle: a.backend.clone(),
            in_chat: true,
        })
        .collect();
    chats.sort_by_key(|r| r.name.to_lowercase());
    idle.extend(chats);
    idle
}

fn selected_row(engine: &Engine, state: &Hub) -> Option<Row> {
    rows(engine, state).into_iter().find(|r| r.key == state.selected_key)
}

fn select_first(engine: &Engine, state: &mut Hub) {
    state.selected_key = rows(engine, state).first().map(|r| r.key.clone()).unwrap_or_default();
}

fn starting(engine: &Engine, state: &Hub) -> bool {
    state.submitting || !engine.starting_contact().is_empty()
}

fn can_confirm(engine: &Engine, state: &Hub) -> bool {
    engine.connected()
        && !starting(engine, state)
        && !state.choosing_directory
        && if state.new_contact { !state.name.trim().is_empty() } else { selected_row(engine, state).is_some() }
}

// ---- the directory picker -------------------------------------------------------

fn directory_timer_running() -> bool {
    DIRECTORY_TIMER.with(slint::Timer::running)
}

/// LaunchDirectoryPicker.qml `choices`: "~" until the Host answers an empty
/// query; nothing while a typed query is pending.
fn directory_choices(engine: &Engine, state: &Hub) -> Vec<(String, String)> {
    let typed = !state.directory_query.trim().is_empty();
    let home = || if typed { Vec::new() } else { vec![("~".to_owned(), "~".to_owned())] };
    if !engine.connected() || directory_timer_running() || engine.launch_directories_loading() {
        return home();
    }
    let found: Vec<(String, String)> = engine
        .launch_directories()
        .iter()
        .filter_map(Value::as_object)
        .map(|o| {
            let path = json::string(o, "path");
            let label = json::string(o, "label");
            (path.clone(), if label.is_empty() { path } else { label })
        })
        .filter(|(path, _)| !path.is_empty())
        .collect();
    if found.is_empty() { home() } else { found }
}

fn reload_directories(app: &Rc<App>) {
    let query = hub(|s| s.directory_query.trim().to_owned());
    if app.engine.borrow().connected() {
        app.engine.borrow_mut().load_launch_directories(&query);
        pump_now(app);
    }
}

fn open_directory_picker(app: &Rc<App>, window: &AppWindow) {
    DIRECTORY_TIMER.with(slint::Timer::stop);
    hub(|s| {
        s.choosing_directory = true;
        s.editing_model = false;
        s.directory_query.clear();
        s.directory_index = 0;
        s.confirm_when_ready = false;
    });
    window.global::<LaunchHub>().set_directory_text("".into());
    reload_directories(app);
    focus(window, "directory");
    show(app, window);
}

fn directory_chosen(app: &Rc<App>, window: &AppWindow, path: &str) {
    hub(|s| {
        s.directory = path.to_owned();
        s.choosing_directory = false;
        s.confirm_when_ready = false;
    });
    app.engine.borrow_mut().set_launch_directory(path);
    focus(window, "search");
    show(app, window);
}

/// LaunchDirectoryPicker.qml `confirm`: an empty query is home; a query
/// still in flight is chosen once the Host answers.
fn directory_confirm(app: &Rc<App>, window: &AppWindow) {
    let (typed, index) = hub(|s| (s.directory_query.trim().to_owned(), s.directory_index));
    if typed.is_empty() && index == 0 {
        directory_chosen(app, window, "~");
        return;
    }
    if directory_timer_running() {
        DIRECTORY_TIMER.with(slint::Timer::stop);
        hub(|s| s.confirm_when_ready = true);
        reload_directories(app);
        return;
    }
    let (loading, connected) = {
        let engine = app.engine.borrow();
        (engine.launch_directories_loading(), engine.connected())
    };
    if loading || !connected {
        hub(|s| s.confirm_when_ready = true);
        return;
    }
    hub(|s| s.confirm_when_ready = false);
    let choices = hub(|s| directory_choices(&app.engine.borrow(), s));
    if let Some((path, _)) = choices.get(index.min(choices.len().saturating_sub(1))).filter(|_| !choices.is_empty()) {
        directory_chosen(app, window, &path.clone());
    } else if !typed.is_empty() {
        directory_chosen(app, window, &typed);
    }
}

fn directory_edited(app: &Rc<App>, window: &AppWindow, text: &str) {
    hub(|s| {
        s.directory_query = text.to_owned();
        s.directory_index = 0;
        s.confirm_when_ready = false;
    });
    DIRECTORY_TIMER.with(|timer| {
        timer.start(slint::TimerMode::SingleShot, DIRECTORY_QUERY_DELAY, || {
            crate::with_window(|app, window| {
                reload_directories(app);
                show(app, window);
            });
        });
    });
    show(app, window);
}

fn directory_move(app: &Rc<App>, window: &AppWindow, delta: i32) {
    let limit = {
        let engine = app.engine.borrow();
        if directory_timer_running() || engine.launch_directories_loading() { 15 } else { hub(|s| directory_choices(&engine, s).len().saturating_sub(1)) }
    };
    hub(|s| s.directory_index = (s.directory_index as i64 + i64::from(delta)).clamp(0, limit as i64) as usize);
    show(app, window);
}

/// Tab: the highlighted folder becomes the query, to look inside it.
fn directory_complete(app: &Rc<App>, window: &AppWindow) {
    let (loading, choices) = {
        let engine = app.engine.borrow();
        (engine.launch_directories_loading(), hub(|s| directory_choices(&engine, s)))
    };
    if choices.is_empty() || directory_timer_running() || loading {
        return;
    }
    let index = hub(|s| s.directory_index).min(choices.len() - 1);
    let text = format!("{}/", choices[index].0);
    hub(|s| {
        s.directory_query = text.clone();
        s.directory_index = 0;
    });
    window.global::<LaunchHub>().set_directory_text(text.into());
    reload_directories(app);
    show(app, window);
}

// ---- opening, confirming, stepping back -------------------------------------------

fn focus(window: &AppWindow, target: &str) {
    let request = hub(|s| {
        s.focus_request += 1;
        s.focus_request
    });
    let globals = window.global::<LaunchHub>();
    globals.set_focus_target(target.into());
    globals.set_focus_request(request);
}

/// The Host proposed a workspace root: a still-default "~" follows it.
fn sync_host_default_directory(app: &App) {
    let last = app.engine.borrow().last_working_directory();
    let replace = hub(|s| (s.directory.is_empty() || s.directory == "~") && !last.is_empty() && last != "~");
    if replace {
        hub(|s| s.directory = last.clone());
        app.engine.borrow_mut().set_launch_directory(&last);
    }
}

/// NewSessionHub.qml `open`.
pub fn open_hub(app: &Rc<App>, window: &AppWindow) {
    DIRECTORY_TIMER.with(slint::Timer::stop);
    let (directory, saved, offered) = {
        let engine = app.engine.borrow();
        (engine.last_working_directory(), engine.last_backend(), providers(&engine))
    };
    let backend = if offered.iter().any(|(id, _)| *id == saved) { saved } else { offered.first().map(|p| p.0.clone()).unwrap_or_default() };
    hub(|s| {
        *s = Hub { focus_request: s.focus_request, directory: if directory.is_empty() { "~".into() } else { directory }, backend, ..Hub::default() };
    });
    sync_host_default_directory(app);
    app.engine.borrow_mut().clear_error();
    hub(|s| select_first(&app.engine.borrow(), s));
    let globals = window.global::<LaunchHub>();
    globals.set_query("".into());
    globals.set_name("".into());
    globals.set_directory_text("".into());
    globals.set_open(true);
    open_overlay(app, window, HUB);
    pump_now(app);
    focus(window, "search");
    show(app, window);
}

/// NewSessionHub.qml `openLaunch`: a command-line launch opens the hub and
/// starts at once, anonymously or with whichever contact the pool has
/// free (an empty pool falls back to New contact). Waits for the Host.
pub fn open_launch(app: &Rc<App>, window: &AppWindow, backend: &str, model: &str, effort: &str, anonymous: bool, directory: &str) {
    open_hub(app, window);
    if !directory.is_empty() {
        hub(|s| s.directory = directory.to_owned());
        app.engine.borrow_mut().set_launch_directory(directory);
    }
    let known = providers(&app.engine.borrow()).iter().any(|(id, _)| id == backend);
    hub(|s| {
        if known {
            s.backend = backend.to_owned();
        }
        s.model = model.to_owned();
        s.effort = effort.to_owned();
    });
    if backend.is_empty() {
        show(app, window);
        return;
    }
    if !app.engine.borrow().connected() {
        hub(|s| s.pending_launch = if anonymous { "anonymous" } else { "contact" }.into());
        show(app, window);
        return;
    }
    start_launch(app, anonymous);
    show(app, window);
}

fn start_launch(app: &Rc<App>, anonymous: bool) {
    let (backend, model, effort) = hub(|s| (s.backend.clone(), s.model.clone(), s.effort.clone()));
    let started = if anonymous {
        app.engine.borrow_mut().start_anonymous_agent(&backend, &model, &effort)
    } else {
        app.engine.borrow_mut().start_available_contact(&backend, &model, &effort)
    };
    hub(|s| s.submitting = started);
    pump_now(app);
}

fn close_hub(app: &Rc<App>, window: &AppWindow) {
    DIRECTORY_TIMER.with(slint::Timer::stop);
    hub(|s| {
        s.submitting = false;
        s.pending_launch.clear();
    });
    window.global::<LaunchHub>().set_open(false);
    if overlay_is(app, HUB) {
        window.set_surface("chats".into());
        close_overlay(app, window);
    }
}

/// Escape steps back through the inline editors before it closes the hub;
/// a start in flight keeps it open.
fn step_back(app: &Rc<App>, window: &AppWindow) {
    let (submitting, choosing, editing, new_contact) = hub(|s| (s.submitting, s.choosing_directory, s.editing_model, s.new_contact));
    if submitting {
        return;
    }
    if choosing {
        DIRECTORY_TIMER.with(slint::Timer::stop);
        hub(|s| s.choosing_directory = false);
        focus(window, "search");
    } else if editing {
        hub(|s| s.editing_model = false);
    } else if new_contact {
        hub(|s| s.new_contact = false);
        focus(window, "search");
    } else {
        close_hub(app, window);
        return;
    }
    show(app, window);
}

fn confirm(app: &Rc<App>, window: &AppWindow) {
    if !hub(|s| can_confirm(&app.engine.borrow(), s)) {
        return;
    }
    app.engine.borrow_mut().clear_error();
    let (new_contact, name, directory, backend, model, effort) =
        hub(|s| (s.new_contact, s.name.trim().to_owned(), s.directory.clone(), s.backend.clone(), s.model.clone(), s.effort.clone()));
    if new_contact {
        hub(|s| s.submitting = true);
        app.engine.borrow_mut().create_agent(&name, &directory, &backend, &model, &effort, "", "fresh", "", Vec::new());
        pump_now(app);
        show(app, window);
        return;
    }
    let Some(row) = hub(|s| selected_row(&app.engine.borrow(), s)) else { return };
    if row.in_chat {
        app.engine.borrow_mut().select(&row.session);
        pump_now(app);
        close_hub(app, window);
        return;
    }
    app.engine.borrow_mut().set_launch_directory(&directory);
    let started = app.engine.borrow_mut().quick_start_contact(&row.name, &backend, &model, &effort);
    hub(|s| s.submitting = started);
    pump_now(app);
    show(app, window);
}

fn step_provider(app: &Rc<App>, window: &AppWindow, index: i64) {
    let offered = providers(&app.engine.borrow());
    if offered.is_empty() {
        return;
    }
    let next = offered[index.rem_euclid(offered.len() as i64) as usize].0.clone();
    hub(|s| {
        if s.backend != next {
            s.backend = next;
            s.model.clear();
            s.effort.clear();
        }
    });
    show(app, window);
}

fn provider_index(engine: &Engine, state: &Hub) -> usize {
    providers(engine).iter().position(|(id, _)| *id == state.backend).unwrap_or(0)
}

fn move_selection(app: &Rc<App>, window: &AppWindow, delta: i32) {
    hub(|s| {
        let rows = rows(&app.engine.borrow(), s);
        if rows.is_empty() {
            s.selected_key.clear();
            return;
        }
        let next = match rows.iter().position(|r| r.key == s.selected_key) {
            None => 0,
            Some(i) => (i as i64 + i64::from(delta)).clamp(0, rows.len() as i64 - 1) as usize,
        };
        s.selected_key = rows[next].key.clone();
    });
    show(app, window);
}

/// Pushes the hub's state to the window.
fn show(app: &App, window: &AppWindow) {
    if !overlay_is(app, HUB) {
        return;
    }
    let engine = app.engine.borrow();
    let globals = window.global::<LaunchHub>();
    hub(|s| {
        let offered = providers(&engine);
        globals.set_connected(engine.connected());
        globals.set_providers(model(
            offered.iter().map(|(id, label)| ProviderChip { id: id.into(), label: label.into(), initial: initial(label) }).collect(),
        ));
        globals.set_provider_index(provider_index(&engine, s) as i32);
        let models = choices(&engine.models_for_backend(&s.backend));
        let efforts = choices(&engine.efforts_for_model(&s.backend, &s.model));
        let label = |list: &[(String, String)], id: &str| list.iter().find(|(i, _)| i == id).map(|(_, l)| l.clone()).unwrap_or_else(|| id.to_owned());
        let model_label = if s.model.is_empty() { "Server default".to_owned() } else { label(&models, &s.model) };
        let summary = if s.effort.is_empty() { model_label } else { format!("{model_label} · {}", label(&efforts, &s.effort)) };
        globals.set_model_summary(summary.into());
        globals.set_editing_model(s.editing_model);
        let with_default =
            |list: &[(String, String)]| -> Vec<String> { std::iter::once("Server default".to_owned()).chain(list.iter().map(|(_, l)| l.clone())).collect() };
        globals.set_model_labels(strings(with_default(&models)));
        globals.set_model_index(models.iter().position(|(id, _)| *id == s.model).map_or(0, |i| i as i32 + 1));
        globals.set_effort_labels(strings(with_default(&efforts)));
        globals.set_effort_index(efforts.iter().position(|(id, _)| *id == s.effort).map_or(0, |i| i as i32 + 1));
        globals.set_directory(s.directory.clone().into());
        globals.set_choosing_directory(s.choosing_directory);
        let folders = directory_choices(&engine, s);
        let loading = directory_timer_running() || engine.launch_directories_loading();
        globals.set_directory_empty(if engine.connected() && !loading && folders.is_empty() { "No matching directories" } else { "" }.into());
        globals.set_directory_current(if folders.is_empty() { -1 } else { s.directory_index.min(folders.len() - 1) as i32 });
        globals.set_directory_rows(model(folders.into_iter().map(|(path, label)| DirRow { path: path.into(), label: label.into() }).collect()));
        globals.set_show_all(s.show_all);
        globals.set_new_contact(s.new_contact);
        let rows = rows(&engine, s);
        if !rows.iter().any(|r| r.key == s.selected_key) {
            s.selected_key = rows.first().map(|r| r.key.clone()).unwrap_or_default();
        }
        let current = rows.iter().position(|r| r.key == s.selected_key);
        globals.set_current(current.map_or(-1, |i| i as i32));
        globals.set_empty_text(
            if !s.query.is_empty() {
                "No contact matches"
            } else if s.show_all {
                "No contacts"
            } else {
                "Every contact is in a chat · Show all to open one"
            }
            .into(),
        );
        let in_chat = current.is_some_and(|i| rows[i].in_chat);
        let label = if starting(&engine, s) {
            "Starting…".to_owned()
        } else {
            format!("{} · Enter", if !s.new_contact && in_chat { "Open chat" } else { "Create session" })
        };
        globals.set_confirm_label(label.into());
        globals.set_can_confirm(can_confirm(&engine, s));
        globals.set_rows(model(
            rows.iter()
                .map(|r| ContactCard {
                    key: r.key.clone().into(),
                    name: r.name.clone().into(),
                    subtitle: r.subtitle.clone().into(),
                    initial: initial(&r.name),
                    in_chat: r.in_chat,
                })
                .collect(),
        ));
    });
    globals.set_error(engine.error().into());
}

// ---- StartAgentDialog.qml -----------------------------------------------------------

/// The last folder of a path, for a suggestion's button.
fn path_label(path: &str) -> String {
    let trimmed = path.strip_suffix('/').unwrap_or(path);
    trimmed.rsplit('/').next().unwrap_or(trimmed).to_owned()
}

fn server_name(value: &Value) -> String {
    match value {
        Value::String(name) => name.clone(),
        Value::Object(row) => json::string(row, "name"),
        _ => String::new(),
    }
}

/// Opens the Start dialog for a new agent named `name`, or to relaunch
/// `replace_session` (AgentOverview and the profile ask for these).
pub fn open_start_agent(app: &Rc<App>, window: &AppWindow, replace_session: &str, name: &str) {
    let (workspace, backend, model, effort, servers) = {
        let engine = app.engine.borrow();
        match engine.roster().find(replace_session).filter(|_| !replace_session.is_empty()) {
            Some(agent) => (
                agent.working_directory.clone(),
                agent.backend.clone(),
                agent.model.clone(),
                agent.effort.clone(),
                agent.mcp_servers.iter().map(server_name).filter(|n| !n.is_empty()).collect(),
            ),
            None => ("~".to_owned(), engine.last_backend(), String::new(), String::new(), Vec::new()),
        }
    };
    let workspace = if workspace.is_empty() { "~".to_owned() } else { workspace };
    start_state(|s| {
        *s = Start {
            replace_session: replace_session.to_owned(),
            backend,
            model,
            effort,
            mode: "fresh".into(),
            mcp_servers: servers,
            focus_request: s.focus_request + 1,
            ..Start::default()
        };
    });
    let globals = window.global::<StartAgent>();
    globals.set_name(name.into());
    globals.set_workspace(workspace.clone().into());
    app.engine.borrow_mut().clear_error();
    app.engine.borrow_mut().load_favorite_paths();
    app.engine.borrow_mut().request_directory_suggestions(&workspace);
    open_overlay(app, window, START);
    pump_now(app);
    show_start(app, window);
    globals.set_focus_request(start_state(|s| s.focus_request));
}

fn start_backends(engine: &Engine) -> Vec<(String, String)> {
    providers(engine)
}

fn start_models(engine: &Engine, backend: &str) -> Vec<(String, String)> {
    engine
        .models_for_backend(backend)
        .iter()
        .filter_map(Value::as_object)
        .map(|o| (json::string(o, "id"), json::string(o, "label")))
        .collect()
}

fn start_efforts(engine: &Engine, backend: &str, model: &str) -> Vec<(String, String)> {
    engine
        .efforts_for_model(backend, model)
        .iter()
        .filter_map(Value::as_object)
        .map(|o| (json::string(o, "id"), json::string(o, "label")))
        .collect()
}

fn past_id(value: &Value) -> String {
    let Some(row) = value.as_object() else { return String::new() };
    let id = json::string(row, "id");
    if id.is_empty() { json::string(row, "session_id") } else { id }
}

fn show_start(app: &App, window: &AppWindow) {
    if !overlay_is(app, START) {
        return;
    }
    let engine = app.engine.borrow();
    let globals = window.global::<StartAgent>();
    start_state(|s| {
        let backends = start_backends(&engine);
        if !backends.iter().any(|(id, _)| *id == s.backend) {
            s.backend = backends.first().map(|b| b.0.clone()).unwrap_or_default();
        }
        let models = start_models(&engine, &s.backend);
        let efforts = start_efforts(&engine, &s.backend, &s.model);
        let index = |list: &[(String, String)], id: &str| list.iter().position(|(i, _)| i == id).map_or(0, |i| i as i32);
        globals.set_relaunch(!s.replace_session.is_empty());
        globals.set_suggestions(strings(engine.directory_suggestions().iter().map(|p| path_label(p))));
        globals.set_favorites(strings(engine.favorite_paths().iter().map(|p| path_label(p))));
        globals.set_backend_labels(strings(backends.iter().map(|(_, l)| l.clone())));
        globals.set_backend_index(index(&backends, &s.backend));
        globals.set_model_labels(strings(models.iter().map(|(_, l)| l.clone())));
        globals.set_model_index(index(&models, &s.model));
        globals.set_effort_labels(strings(efforts.iter().map(|(_, l)| l.clone())));
        globals.set_effort_index(index(&efforts, &s.effort));
        let servers: Vec<McpChoice> = if s.backend == "claude" {
            engine
                .available_mcp_servers()
                .iter()
                .map(server_name)
                .filter(|n| !n.is_empty())
                .map(|name| McpChoice { on: s.mcp_servers.contains(&name), name: name.into() })
                .collect()
        } else {
            Vec::new()
        };
        globals.set_mcp_servers(model(servers));
        globals.set_can_resume(engine.backend_supports_resume(&s.backend));
        globals.set_can_fork(engine.backend_supports_fork(&s.backend));
        globals.set_mode(s.mode.clone().into());
        let past = engine.past_sessions();
        globals.set_past_current(past.iter().position(|p| !s.past_session_id.is_empty() && past_id(p) == s.past_session_id).map_or(-1, |i| i as i32));
        globals.set_past_sessions(model(
            past.iter()
                .filter_map(Value::as_object)
                .map(|p| {
                    let title = ["title", "preview", "id", "session_id"].iter().map(|k| json::string(p, k)).find(|t| !t.is_empty()).unwrap_or_default();
                    PastRow { title: title.into(), cwd: json::string(p, "cwd").into() }
                })
                .collect(),
        ));
        globals.set_past_loading(engine.past_sessions_loading());
        globals.set_past_ready(s.mode == "fresh" || !s.past_session_id.is_empty());
    });
    globals.set_error(engine.error().into());
}

fn load_past(app: &Rc<App>, window: &AppWindow) {
    let (mode, backend) = start_state(|s| (s.mode.clone(), s.backend.clone()));
    if mode != "fresh" {
        let workspace = window.global::<StartAgent>().get_workspace().to_string();
        app.engine.borrow_mut().load_past_sessions(&workspace, &backend, false);
        pump_now(app);
    }
}

fn choose_workspace(app: &Rc<App>, window: &AppWindow, path: &str) {
    window.global::<StartAgent>().set_workspace(path.into());
    app.engine.borrow_mut().request_directory_suggestions(path);
    pump_now(app);
    load_past(app, window);
    show_start(app, window);
}

fn set_mode(app: &Rc<App>, window: &AppWindow, mode: &str) {
    start_state(|s| {
        s.mode = mode.to_owned();
        s.past_session_id.clear();
    });
    load_past(app, window);
    show_start(app, window);
}

fn start_agent(app: &Rc<App>, window: &AppWindow) {
    show_start(app, window);
    let globals = window.global::<StartAgent>();
    if !globals.get_can_start() {
        return;
    }
    let (name, workspace) = (globals.get_name().to_string(), globals.get_workspace().to_string());
    let (backend, model, effort, replace, mode, past, servers) =
        start_state(|s| (s.backend.clone(), s.model.clone(), s.effort.clone(), s.replace_session.clone(), s.mode.clone(), s.past_session_id.clone(), s.mcp_servers.clone()));
    let servers: Vec<Value> = if backend == "claude" { servers.into_iter().map(Value::from).collect() } else { Vec::new() };
    app.engine.borrow_mut().create_agent(&name, &workspace, &backend, &model, &effort, &replace, &mode, &past, servers);
    pump_now(app);
    show_start(app, window);
}

// ---- the switcher's contacts-only mode ----------------------------------------------

/// "Start an idle contact": the switcher listing only idle contacts.
pub fn open_contacts(app: &Rc<App>, window: &AppWindow, restore_composer: Option<bool>) {
    crate::commands::open_switcher(app, window);
    {
        let mut state = app.switcher.borrow_mut();
        state.contacts_only = true;
        if let Some(restore) = restore_composer {
            state.restore_composer = restore;
        }
    }
    window.set_switcher_placeholder("Start an idle contact".into());
    window.set_switcher_empty("No idle contacts".into());
    crate::commands::refresh_switcher(app, window);
}

// ---- wiring --------------------------------------------------------------------------

/// The keyboard-map and switcher actions this module does; None for others.
pub fn run(app: &Rc<App>, window: &AppWindow, action: &str) -> Option<bool> {
    let switcher_open = app.switcher.borrow().open;
    match action {
        "new" | "quick-new-agent" => open_hub(app, window),
        "new-contact" => open_contacts(app, window, None),
        "change-directory" => {
            if !overlay_is(app, HUB) {
                open_hub(app, window);
            }
            open_directory_picker(app, window);
        }
        "escape" if !switcher_open && overlay_is(app, HUB) => step_back(app, window),
        "escape" if !switcher_open && overlay_is(app, START) => close_overlay(app, window),
        _ => return None,
    }
    Some(true)
}

/// Follows what the engine reported while a dialog is open.
pub fn refresh(app: &Rc<App>, window: &AppWindow, changes: &[Change]) {
    if changes.iter().any(|c| matches!(c, Change::Launch)) {
        crate::commands::refresh_switcher(app, window);
    }
    if overlay_is(app, START) {
        if changes.iter().any(|c| matches!(c, Change::AgentMutated(_))) {
            close_overlay(app, window);
        } else {
            show_start(app, window);
        }
    }
    if !overlay_is(app, HUB) {
        return;
    }
    for change in changes {
        match change {
            Change::LaunchPoolEmpty if hub(|s| s.submitting) => {
                hub(|s| {
                    s.submitting = false;
                    s.new_contact = true;
                });
                focus(window, "name");
            }
            Change::AgentMutated(_) if hub(|s| s.submitting) => {
                close_hub(app, window);
                return;
            }
            Change::Launch => {
                sync_host_default_directory(app);
                let (starting, error) = {
                    let engine = app.engine.borrow();
                    (engine.starting_contact(), engine.error().to_owned())
                };
                let launched = hub(|s| s.submitting && !s.new_contact && selected_row(&app.engine.borrow(), s).is_some());
                if launched && starting.is_empty() && error.is_empty() {
                    close_hub(app, window);
                    return;
                }
                let ready = hub(|s| s.choosing_directory && s.confirm_when_ready) && !directory_timer_running();
                if ready && !app.engine.borrow().launch_directories_loading() {
                    directory_confirm(app, window);
                }
            }
            Change::Error if !app.engine.borrow().error().is_empty() => hub(|s| {
                s.submitting = false;
                s.confirm_when_ready = false;
            }),
            Change::Connection if app.engine.borrow().connected() => {
                let pending = hub(|s| std::mem::take(&mut s.pending_launch));
                if !pending.is_empty() {
                    start_launch(app, pending == "anonymous");
                }
                if hub(|s| s.choosing_directory) {
                    reload_directories(app);
                }
            }
            _ => {}
        }
    }
    show(app, window);
}

/// Connects the `LaunchHub` and `StartAgent` globals to this module.
pub fn wire(window: &AppWindow) {
    use crate::with_window;
    let globals = window.global::<LaunchHub>();
    globals.on_step_provider(|delta| with_window(|app, window| {
        let index = hub(|s| provider_index(&app.engine.borrow(), s)) as i64;
        step_provider(app, window, index + i64::from(delta));
    }));
    globals.on_choose_provider(|index| with_window(|app, window| step_provider(app, window, i64::from(index))));
    globals.on_search_edited(|text| with_window(|app, window| {
        hub(|s| {
            s.query = text.to_string();
            select_first(&app.engine.borrow(), s);
        });
        show(app, window);
    }));
    globals.on_name_edited(|text| with_window(|app, window| {
        hub(|s| s.name = text.to_string());
        show(app, window);
    }));
    globals.on_move(|delta| with_window(|app, window| move_selection(app, window, delta)));
    globals.on_choose_row(|index| with_window(|app, window| {
        hub(|s| {
            if let Some(row) = rows(&app.engine.borrow(), s).get(index as usize) {
                s.selected_key = row.key.clone();
            }
        });
        show(app, window);
    }));
    globals.on_activate_row(|index| with_window(|app, window| {
        hub(|s| {
            if let Some(row) = rows(&app.engine.borrow(), s).get(index as usize) {
                s.selected_key = row.key.clone();
            }
        });
        confirm(app, window);
    }));
    globals.on_toggle_show_all(|| with_window(|app, window| {
        hub(|s| s.show_all = !s.show_all);
        show(app, window);
    }));
    globals.on_toggle_new_contact(|| with_window(|app, window| {
        let now = hub(|s| {
            s.new_contact = !s.new_contact;
            s.new_contact
        });
        focus(window, if now { "name" } else { "search" });
        show(app, window);
    }));
    globals.on_toggle_edit_model(|| with_window(|app, window| {
        hub(|s| s.editing_model = !s.editing_model);
        show(app, window);
    }));
    globals.on_model_chosen(|index| with_window(|app, window| {
        hub(|s| {
            let models = choices(&app.engine.borrow().models_for_backend(&s.backend));
            s.model = if index <= 0 { String::new() } else { models.get(index as usize - 1).map(|m| m.0.clone()).unwrap_or_default() };
            s.effort.clear();
        });
        show(app, window);
    }));
    globals.on_effort_chosen(|index| with_window(|app, window| {
        hub(|s| {
            let efforts = choices(&app.engine.borrow().efforts_for_model(&s.backend, &s.model));
            s.effort = if index <= 0 { String::new() } else { efforts.get(index as usize - 1).map(|e| e.0.clone()).unwrap_or_default() };
        });
        show(app, window);
    }));
    globals.on_change_directory(|| with_window(|app, window| open_directory_picker(app, window)));
    globals.on_directory_edited(|text| with_window(|app, window| directory_edited(app, window, &text)));
    globals.on_directory_move(|delta| with_window(|app, window| directory_move(app, window, delta)));
    globals.on_directory_complete(|| with_window(|app, window| directory_complete(app, window)));
    globals.on_directory_chosen(|index| with_window(|app, window| {
        if index < 0 {
            directory_confirm(app, window);
            return;
        }
        let path = hub(|s| directory_choices(&app.engine.borrow(), s)).get(index as usize).map(|c| c.0.clone());
        if let Some(path) = path {
            directory_chosen(app, window, &path);
        }
    }));
    globals.on_confirm(|| with_window(|app, window| confirm(app, window)));
    globals.on_cancel(|| with_window(|app, window| close_hub(app, window)));
    globals.on_open_connection(|| with_window(|app, window| {
        close_hub(app, window);
        if !crate::commands::run(app, window, "connection") {
            eprintln!("clarp-slint: the Host connection page is not available");
        }
    }));

    let start = window.global::<StartAgent>();
    start.on_workspace_edited(|_| with_window(|_, _| {
        SUGGESTION_TIMER.with(|timer| {
            timer.start(slint::TimerMode::SingleShot, SUGGESTION_DELAY, || {
                with_window(|app, window| {
                    let workspace = window.global::<StartAgent>().get_workspace().to_string();
                    app.engine.borrow_mut().request_directory_suggestions(&workspace);
                    pump_now(app);
                    load_past(app, window);
                    show_start(app, window);
                });
            });
        });
    }));
    start.on_suggestion_chosen(|index| with_window(|app, window| {
        let path = app.engine.borrow().directory_suggestions().get(index as usize).cloned();
        if let Some(path) = path {
            choose_workspace(app, window, &path);
        }
    }));
    start.on_favorite_chosen(|index| with_window(|app, window| {
        let path = app.engine.borrow().favorite_paths().get(index as usize).cloned();
        if let Some(path) = path {
            choose_workspace(app, window, &path);
        }
    }));
    start.on_backend_chosen(|index| with_window(|app, window| {
        let backend = start_backends(&app.engine.borrow()).get(index as usize).map(|b| b.0.clone());
        if let Some(backend) = backend {
            start_state(|s| {
                s.backend = backend;
                s.model.clear();
                s.effort.clear();
            });
        }
        set_mode(app, window, "fresh");
    }));
    start.on_model_chosen(|index| with_window(|app, window| {
        start_state(|s| {
            s.model = start_models(&app.engine.borrow(), &s.backend).get(index as usize).map(|m| m.0.clone()).unwrap_or_default();
            s.effort.clear();
        });
        show_start(app, window);
    }));
    start.on_effort_chosen(|index| with_window(|app, window| {
        start_state(|s| s.effort = start_efforts(&app.engine.borrow(), &s.backend, &s.model).get(index as usize).map(|e| e.0.clone()).unwrap_or_default());
        show_start(app, window);
    }));
    start.on_mcp_toggled(|index| with_window(|app, window| {
        let name = app.engine.borrow().available_mcp_servers().get(index as usize).map(server_name);
        if let Some(name) = name {
            start_state(|s| {
                if let Some(at) = s.mcp_servers.iter().position(|n| *n == name) {
                    s.mcp_servers.remove(at);
                } else {
                    s.mcp_servers.push(name);
                }
            });
        }
        show_start(app, window);
    }));
    start.on_mode_chosen(|mode| with_window(|app, window| set_mode(app, window, &mode)));
    start.on_past_chosen(|index| with_window(|app, window| {
        let id = app.engine.borrow().past_sessions().get(index as usize).map(past_id).unwrap_or_default();
        start_state(|s| s.past_session_id = id);
        show_start(app, window);
    }));
    start.on_start(|| with_window(|app, window| start_agent(app, window)));
    start.on_cancel(|| with_window(|app, window| {
        if overlay_is(app, START) {
            close_overlay(app, window);
        }
    }));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_suggestion_is_labelled_by_its_last_folder() {
        assert_eq!(path_label("/home/fake/src/"), "src");
        assert_eq!(path_label("/home/fake/notes"), "notes");
        assert_eq!(path_label("~"), "~");
    }

    #[test]
    fn catalog_choices_leave_out_the_default_row() {
        let values = vec![serde_json::json!({"id": "", "label": "Provider default"}), serde_json::json!({"id": "opus", "label": ""})];
        assert_eq!(choices(&values), vec![("opus".to_owned(), "opus".to_owned())]);
    }
}
