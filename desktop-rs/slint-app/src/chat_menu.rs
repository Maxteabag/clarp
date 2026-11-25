//! A chat's own choices (#653): its Tool activity (this device's, kept by
//! its Host's server id and its session; "Follow Settings" follows the
//! global choice) and its messages between agents (the Host's per-chat
//! choice, `POST /agent-coordination`), and on a Host that offers it its
//! Conversation or Session view (#702: this device's own choice, written to
//! the Host's agent setting too). All are in the chat's "⋯" menu on
//! its pane's frame, each a submenu showing its current value, and in its
//! profile. The menu also opens the agent's artifacts (its profile's
//! Artifacts).

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
    // Conversation or Session view (#702): Follow Settings shows what it
    // follows ("Follow Settings · Conversation").
    let offered = engine.chat_view_offered(session);
    let own = engine.chat_view_menu_value(session);
    menu.set_view_shown(offered);
    menu.set_view_choices(choices(&clarp_core::chat_view::MENU, own));
    menu.set_view_label(SharedString::from(view_label(&engine, session)));
    menu.set_activity_shown(!(offered && engine.chat_view_conversation(session)));
    // Snooze, kept on this device (triage.rs).
    let snooze = engine.roster().rows().iter().find(|r| r.session == session).and_then(|row| crate::triage::snooze_of(&engine, row));
    menu.set_snooze_label(snooze.as_ref().map_or_else(|| "Off".to_owned(), |s| format!("Until {}", crate::triage::label(s))).into());
    let snooze_choices: Vec<(&str, &str)> = if snooze.is_some() {
        vec![("wake", "Wake now"), ("message", "Until its next message"), ("hour", "For an hour"), ("morning", "Until tomorrow 08:00")]
    } else {
        vec![("message", "Until its next message"), ("hour", "For an hour"), ("morning", "Until tomorrow 08:00")]
    };
    menu.set_snooze_choices(choices(&snooze_choices, ""));
}

/// The words for the chat's view: its own choice, or "Follow Settings ·
/// Conversation" with what it follows.
pub fn view_label(engine: &Engine, session: &str) -> String {
    let own = engine.chat_view_menu_value(session);
    if own == "default" {
        format!("Follow Settings · {}", clarp_core::chat_view::label(engine.chat_view(session)))
    } else {
        clarp_core::chat_view::menu_label(own).to_owned()
    }
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
    if submenu == "snooze" {
        close(window);
        crate::triage::set(app, &session, value);
        return;
    }
    if !session.is_empty() {
        match submenu {
            "activity" => set_activity(&mut app.engine.borrow_mut(), &session, value),
            "coordination" => app.engine.borrow_mut().set_chat_coordination(&session, value),
            "view" => app.engine.borrow_mut().set_chat_view(&session, value),
            _ => {}
        }
    }
    close(window);
}

/// The menu's Artifacts: the chat's profile, at its Artifacts (reports,
/// forms, files, decision cards, media), for the chat's agent on its own
/// Host.
pub fn artifacts(app: &std::rc::Rc<App>, window: &AppWindow) {
    let session = app.session_of(&window.global::<ChatMenu>().get_open());
    close(window);
    if session.is_empty() {
        return;
    }
    crate::profile_view::open(app, window, &session);
    let bridge = window.global::<crate::ProfileBridge>();
    bridge.set_artifacts_request(bridge.get_artifacts_request() + 1);
}

pub fn wire(window: &AppWindow) {
    let menu = window.global::<ChatMenu>();
    menu.on_opened(|pane| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open(&app, &window, &pane);
        }
    });
    menu.on_artifacts(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            artifacts(&app, &window);
        }
    });
    menu.on_pick(|submenu, value| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            pick(&app, &window, &submenu, &value);
            crate::pump_now(&app);
        }
    });
}
