//! A chat's own choices (#653): its Tool activity (this device's, kept by
//! its Host's server id and its session; "Follow Settings" follows the
//! global choice) and its messages between agents (the Host's per-chat
//! choice, `POST /agent-coordination`). Both are in the chat's "⋯" menu on
//! its pane's frame, each a submenu showing its current value, and in its
//! profile.

use clarp_engine::Engine;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::{App, AppWindow, ChatChoice, ChatMenu};

/// A chat's choice for messages between agents, and the words for each.
pub const COORDINATION: [(&str, &str); 3] = [("default", "Follow Settings"), ("show", "Show"), ("hide", "Hide")];

/// The chat's Tool activity choice: "follow" or the mode's index.
pub fn activity_value(engine: &Engine, session: &str) -> String {
    engine.chat_activity_choice(session).map_or_else(|| "follow".to_owned(), |mode| mode.to_string())
}

/// The words for the chat's Tool activity choice ("Follow Settings",
/// "Grouped", …).
pub fn activity_label(value: &str) -> &'static str {
    clarp_core::presentation::CHAT_ACTIVITY_CHOICES.iter().find(|(v, _)| *v == value).map_or("Follow Settings", |(_, label)| label)
}

/// Sets the chat's Tool activity: "follow" or a mode's index.
pub fn set_activity(engine: &mut Engine, session: &str, value: &str) {
    engine.set_chat_activity(session, value.parse::<i32>().ok());
}

/// The words for the chat's messages-between-agents choice.
pub fn coordination_label(value: &str) -> &'static str {
    COORDINATION.iter().find(|(v, _)| *v == value).map_or("Follow Settings", |(_, label)| label)
}

/// The menu's rows for the chat on pane `pane_id`, its current values marked.
fn fill(app: &App, window: &AppWindow, session: &str) {
    let engine = app.engine.borrow();
    let menu = window.global::<ChatMenu>();
    let activity = activity_value(&engine, session);
    let coordination = engine.chat_coordination_choice(session);
    let choices = |list: &[(&str, &str)], now: &str| -> ModelRc<ChatChoice> {
        ModelRc::new(VecModel::from(
            list.iter().map(|(value, label)| ChatChoice { value: (*value).into(), label: (*label).into(), current: *value == now }).collect::<Vec<_>>(),
        ))
    };
    menu.set_activity_choices(choices(&clarp_core::presentation::CHAT_ACTIVITY_CHOICES, &activity));
    menu.set_activity_label(SharedString::from(activity_label(&activity)));
    menu.set_coordination_choices(choices(&COORDINATION, &coordination));
    menu.set_coordination_label(SharedString::from(coordination_label(&coordination)));
    menu.set_coordination_shown(engine.chat_hidden_coordination(session));
}

/// Opens (or closes again) the menu of pane `pane_id`.
pub fn open(app: &App, window: &AppWindow, pane_id: &str) {
    let menu = window.global::<ChatMenu>();
    if menu.get_open() == pane_id {
        close(window);
        return;
    }
    let session = app.session_of(pane_id);
    if session.is_empty() {
        return;
    }
    fill(app, window, &session);
    menu.set_submenu(SharedString::new());
    menu.set_open(pane_id.into());
}

pub fn close(window: &AppWindow) {
    let menu = window.global::<ChatMenu>();
    menu.set_open(SharedString::new());
    menu.set_submenu(SharedString::new());
}

/// A choice in a submenu: the chat's Tool activity or its messages between
/// agents. The menu closes; the chat shows the choice at once (Tool
/// activity) or once its Host has it (messages between agents).
pub fn pick(app: &App, window: &AppWindow, submenu: &str, value: &str) {
    let session = app.session_of(&window.global::<ChatMenu>().get_open());
    if !session.is_empty() {
        match submenu {
            "activity" => set_activity(&mut app.engine.borrow_mut(), &session, value),
            "coordination" => app.engine.borrow_mut().set_chat_coordination(&session, value),
            _ => {}
        }
    }
    close(window);
}

pub fn wire(window: &AppWindow) {
    let menu = window.global::<ChatMenu>();
    menu.on_opened(|pane| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open(&app, &window, &pane);
        }
    });
    menu.on_pick(|submenu, value| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            pick(&app, &window, &submenu, &value);
            crate::pump_now(&app);
        }
    });
}
