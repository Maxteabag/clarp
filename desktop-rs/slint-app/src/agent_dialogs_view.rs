//! Managing the open chat's agent: renaming its contact (RenameAgentDialog
//! .qml), assigning one (AssignAgentDialog.qml), its turn queue
//! (QueueDialog.qml), and the release, retry and terminal actions. The
//! `AgentDialogs` global shows the state kept here.

use std::cell::RefCell;
use std::rc::Rc;

use clarp_core::json;
use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::commands::{close_overlay, open_overlay};
use crate::{AgentDialogs, App, AppWindow, QueuedTurn, pump_now};

pub const RENAME: &str = "rename-agent";
pub const ASSIGN: &str = "assign-agent";
pub const QUEUE: &str = "queue";

const MODES: [&str; 3] = ["auto", "choose", "create"];

#[derive(Default)]
struct Dialogs {
    /// The chat a rename or assignment is for, captured when it opened so
    /// switching panes underneath cannot retarget it.
    session: String,
    current_name: String,
    submitting: bool,
    mode: String,
    queue_session: String,
    focus_request: i32,
}

thread_local! {
    static STATE: RefCell<Dialogs> = RefCell::new(Dialogs::default());
}

fn state<R>(act: impl FnOnce(&mut Dialogs) -> R) -> R {
    STATE.with(|s| act(&mut s.borrow_mut()))
}

fn overlay(app: &App) -> String {
    app.overlay.borrow().clone()
}

fn focus(window: &AppWindow, target: &str) {
    let request = state(|s| {
        s.focus_request += 1;
        s.focus_request
    });
    let globals = window.global::<AgentDialogs>();
    globals.set_focus_target(target.into());
    globals.set_focus_request(request);
}

/// The open chat when it is an agent's (not a pair room).
fn agent_session(app: &App) -> Option<String> {
    let selected = app.engine.borrow().selected_session().to_owned();
    (!selected.is_empty() && !selected.starts_with("pair:")).then_some(selected)
}

fn show(app: &App, window: &AppWindow) {
    let engine = app.engine.borrow();
    let globals = window.global::<AgentDialogs>();
    globals.set_connected(engine.connected());
    globals.set_error(engine.error().into());
    state(|s| {
        globals.set_submitting(s.submitting);
        globals.set_rename_session(s.session.clone().into());
        globals.set_assign_mode(s.mode.clone().into());
        let contacts: Vec<SharedString> =
            engine.assignment_contacts().iter().filter_map(Value::as_object).map(|c| json::string(c, "name").into()).collect();
        let current = globals.get_assign_contact();
        if contacts.is_empty() {
            globals.set_assign_contact(-1);
        } else if current < 0 || current as usize >= contacts.len() {
            globals.set_assign_contact(0);
        }
        globals.set_assign_contacts(ModelRc::new(VecModel::from(contacts)));
        globals.set_queue_title(engine.chat_name(&s.queue_session).into());
        globals.set_queue_paused(engine.turn_queue_paused());
        globals.set_queue_loading(engine.turn_queue_loading());
        globals.set_queue_error(engine.turn_queue_error().into());
        let items: Vec<QueuedTurn> = engine
            .turn_queue(&s.queue_session)
            .iter()
            .filter_map(Value::as_object)
            .map(|item| {
                let id = json::string(item, "queue_id");
                QueuedTurn {
                    id: if id.is_empty() { json::string(item, "id") } else { id }.into(),
                    text: json::string(item, "text").into(),
                    stamp: json::string(item, "enqueued_at").into(),
                }
            })
            .collect();
        globals.set_queue_items(ModelRc::new(VecModel::from(items)));
    });
}

// ---- RenameAgentDialog.qml ----------------------------------------------------

fn open_rename(app: &Rc<App>, window: &AppWindow, session: &str) {
    let name = app.engine.borrow().chat_name(session);
    state(|s| {
        s.session = session.to_owned();
        s.current_name = name.clone();
        s.submitting = false;
    });
    app.engine.borrow_mut().clear_error();
    window.global::<AgentDialogs>().set_rename_name(name.into());
    open_overlay(app, window, RENAME);
    pump_now(app);
    show(app, window);
    focus(window, "rename");
}

fn rename_submit(app: &Rc<App>, window: &AppWindow) {
    let value = window.global::<AgentDialogs>().get_rename_name().trim().to_owned();
    let (submitting, current, session) = state(|s| (s.submitting, s.current_name.clone(), s.session.clone()));
    if submitting || value.is_empty() || !app.engine.borrow().connected() {
        return;
    }
    if value == current {
        close_overlay(app, window);
        return;
    }
    state(|s| s.submitting = true);
    app.engine.borrow_mut().clear_error();
    app.engine.borrow_mut().rename_agent(&session, &value);
    pump_now(app);
    show(app, window);
}

// ---- AssignAgentDialog.qml ----------------------------------------------------

fn open_assign(app: &Rc<App>, window: &AppWindow, session: &str, automatic: bool) {
    state(|s| {
        s.session = session.to_owned();
        s.mode = "auto".into();
        s.submitting = false;
    });
    let globals = window.global::<AgentDialogs>();
    globals.set_assign_name("".into());
    globals.set_assign_contact(-1);
    app.engine.borrow_mut().clear_error();
    open_overlay(app, window, ASSIGN);
    app.engine.borrow_mut().load_assignment_contacts(session);
    pump_now(app);
    show(app, window);
    focus(window, "assign");
    if automatic && app.engine.borrow().connected() {
        assign_submit(app, window);
    }
}

fn assign_mode(app: &Rc<App>, window: &AppWindow, mode: &str) {
    if state(|s| s.submitting) {
        return;
    }
    state(|s| s.mode = mode.to_owned());
    show(app, window);
    focus(window, "assign");
}

fn assign_submit(app: &Rc<App>, window: &AppWindow) {
    if state(|s| s.submitting) || !app.engine.borrow().connected() {
        return;
    }
    let globals = window.global::<AgentDialogs>();
    let (mode, session) = state(|s| (s.mode.clone(), s.session.clone()));
    let name = match mode.as_str() {
        "create" => globals.get_assign_name().trim().to_owned(),
        "choose" => {
            let index = globals.get_assign_contact();
            app.engine
                .borrow()
                .assignment_contacts()
                .get(usize::try_from(index).unwrap_or(usize::MAX))
                .and_then(Value::as_object)
                .map(|c| json::string(c, "name"))
                .unwrap_or_default()
        }
        _ => String::new(),
    };
    if mode != "auto" && name.is_empty() {
        return;
    }
    state(|s| s.submitting = true);
    app.engine.borrow_mut().assign_contact(&session, &mode, &name);
    pump_now(app);
    show(app, window);
}

// ---- QueueDialog.qml -------------------------------------------------------------

/// Opens a chat's queue (from its composer's queue line).
pub fn open_queue(app: &Rc<App>, window: &AppWindow, session: &str) {
    if session.is_empty() {
        return;
    }
    state(|s| s.queue_session = session.to_owned());
    open_overlay(app, window, QUEUE);
    app.engine.borrow_mut().load_turn_queue(session);
    pump_now(app);
    show(app, window);
    focus(window, "queue");
}

// ---- wiring ------------------------------------------------------------------------

fn close(app: &Rc<App>, window: &AppWindow) {
    let name = overlay(app);
    // An assignment in flight finishes before the dialog goes.
    if name == ASSIGN && state(|s| s.submitting) {
        return;
    }
    if [RENAME, ASSIGN, QUEUE].contains(&name.as_str()) {
        state(|s| s.submitting = false);
        close_overlay(app, window);
    }
}

/// The keyboard-map actions this module does; None for others.
pub fn run(app: &Rc<App>, window: &AppWindow, action: &str) -> Option<bool> {
    let switcher_open = app.switcher.borrow().open;
    match action {
        "rename-agent" => {
            if let Some(session) = agent_session(app) {
                open_rename(app, window, &session);
            }
        }
        "assign-agent" | "auto-assign-agent" => {
            if let Some(session) = agent_session(app) {
                open_assign(app, window, &session, action == "auto-assign-agent");
            }
        }
        "release-agent" => {
            let selected = app.engine.borrow().selected_session().to_owned();
            app.engine.borrow_mut().release_agent(&selected);
            pump_now(app);
        }
        "retry-message" => {
            app.engine.borrow_mut().retry_latest_failed_message();
            pump_now(app);
        }
        "agent-terminal" => {
            let selected = app.engine.borrow().selected_session().to_owned();
            app.engine.borrow_mut().open_agent_terminal(&selected);
            pump_now(app);
        }
        "escape" if !switcher_open && [RENAME, ASSIGN, QUEUE].contains(&overlay(app).as_str()) => close(app, window),
        _ => return None,
    }
    Some(true)
}

/// Follows what the engine reported while a dialog is open.
pub fn refresh(app: &Rc<App>, window: &AppWindow, changes: &[Change]) {
    for change in changes {
        let name = overlay(app);
        let (submitting, session) = state(|s| (s.submitting, s.session.clone()));
        match change {
            Change::AgentMutated(changed) if name == RENAME && submitting && *changed == session => close(app, window),
            Change::AssignmentSucceeded(changed) if name == ASSIGN && submitting && *changed == session => {
                state(|s| s.submitting = false);
                close(app, window);
            }
            Change::AssignmentRequested { session, automatic } if name.is_empty() && !app.switcher.borrow().open => {
                open_assign(app, window, session, *automatic);
            }
            Change::Error if !app.engine.borrow().error().is_empty() => state(|s| s.submitting = false),
            _ => {}
        }
    }
    if [RENAME, ASSIGN, QUEUE].contains(&overlay(app).as_str()) {
        show(app, window);
    }
}

/// Connects the `AgentDialogs` global to this module.
pub fn wire(window: &AppWindow) {
    use crate::with_window;
    let globals = window.global::<AgentDialogs>();
    globals.on_rename_submit(|| with_window(|app, window| rename_submit(app, window)));
    globals.on_assign_mode_chosen(|mode| with_window(|app, window| assign_mode(app, window, &mode)));
    globals.on_assign_step(|delta| with_window(|app, window| {
        let current = state(|s| MODES.iter().position(|m| *m == s.mode).unwrap_or(0)) as i32;
        let next = MODES[(current + delta).rem_euclid(MODES.len() as i32) as usize];
        assign_mode(app, window, next);
    }));
    globals.on_assign_submit(|| with_window(|app, window| assign_submit(app, window)));
    globals.on_queue_open(|session| with_window(|app, window| open_queue(app, window, &session)));
    globals.on_queue_refresh(|| with_window(|app, window| {
        let session = state(|s| s.queue_session.clone());
        app.engine.borrow_mut().load_turn_queue(&session);
        pump_now(app);
        show(app, window);
    }));
    globals.on_queue_save(|id, text| with_window(|app, _| {
        app.engine.borrow_mut().update_queued_turn(&id, &text);
        pump_now(app);
    }));
    globals.on_queue_send(|id| with_window(|app, _| {
        app.engine.borrow_mut().send_queued_turn(&id);
        pump_now(app);
    }));
    globals.on_queue_delete(|id| with_window(|app, _| {
        app.engine.borrow_mut().delete_queued_turn(&id);
        pump_now(app);
    }));
    globals.on_cancel(|| with_window(|app, window| close(app, window)));
}
