//! The agent overview (AgentOverview.qml): a card per agent with its
//! actions, archived agents to restore and idle contacts to start.

use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::{App, AppWindow, OverviewArchived, OverviewBridge, OverviewCard, OverviewContact, OverviewSchedule, commands, pump_now};

fn text(value: &Value, key: &str) -> String {
    value.get(key).and_then(Value::as_str).unwrap_or_default().to_owned()
}

/// Opens the overview (Ctrl+Shift+O, the rail's button, the switcher).
pub fn open(app: &Rc<App>, window: &AppWindow) {
    let bridge = window.global::<OverviewBridge>();
    bridge.set_menu("".into());
    bridge.set_confirm_release("".into());
    crate::profile_view::show(app, window, "overview");
    fill(app, window);
}

pub fn refresh(app: &App, window: &AppWindow, changes: &[Change], reduced: bool) {
    window.global::<OverviewBridge>().set_reduced_motion(reduced);
    let relevant = changes.iter().any(|c| {
        matches!(c, Change::Roster | Change::Archive | Change::Selection | Change::Launch | Change::Avatars | Change::AgentMutated(_) | Change::Connection)
    });
    if relevant && *app.overlay.borrow() == "overview" {
        fill(app, window);
    }
}

fn fill(app: &App, window: &AppWindow) {
    let bridge = window.global::<OverviewBridge>();
    let rows = app.engine.borrow().roster().rows();
    let selected = app.engine.borrow().selected_session().to_owned();
    let cards: Vec<OverviewCard> = rows
        .iter()
        .map(|row| {
            let schedules: Vec<OverviewSchedule> = row
                .schedules
                .iter()
                .map(|s| OverviewSchedule {
                    id: text(s, "schedule_id").into(),
                    title: format!("{}  ·  {}", if text(s, "name").is_empty() { "Scheduled task".into() } else { text(s, "name") }, text(s, "cron_expression")).into(),
                    prompt: text(s, "prompt").into(),
                    on: s.get("enabled").and_then(Value::as_bool).unwrap_or(false),
                })
                .collect();
            let preview = [&row.last_message, &row.status_text, &row.state].into_iter().find(|t| !t.is_empty()).cloned().unwrap_or_default();
            OverviewCard {
                session: row.session.as_str().into(),
                name: row.name.as_str().into(),
                initial: crate::view::initial(&row.name),
                symbol: row.avatar_symbol.as_str().into(),
                portrait: crate::profile_view::portrait(app, &row.session),
                preview: preview.into(),
                state: row.state.as_str().into(),
                backend: row.backend.as_str().into(),
                model: row.model.as_str().into(),
                folder: row.working_directory.as_str().into(),
                queued: row.queue_count,
                context: if row.context_window > 0 { row.context_tokens as f32 / row.context_window as f32 } else { -1.0 },
                busy: row.busy,
                muted: row.muted,
                heartbeat: row.heartbeat_enabled,
                dreaming: row.dreaming_enabled,
                selected: row.session == selected,
                schedules: ModelRc::new(VecModel::from(schedules)),
            }
        })
        .collect();
    bridge.set_cards(ModelRc::new(VecModel::from(cards)));
    let engine = app.engine.borrow();
    let archived: Vec<OverviewArchived> = engine
        .archived()
        .rows()
        .iter()
        .map(|row| OverviewArchived {
            session: row.session.as_str().into(),
            name: row.name.as_str().into(),
            preview: if row.last_message.is_empty() { row.session.as_str().into() } else { row.last_message.as_str().into() },
        })
        .collect();
    bridge.set_archived(ModelRc::new(VecModel::from(archived)));
    let starting = engine.starting_contact();
    let detail = format!("{} · {}", engine.quick_start_backend(), engine.last_working_directory());
    let contacts: Vec<OverviewContact> = engine
        .matching_contacts("")
        .iter()
        .map(|c| {
            let name = text(c, "name");
            let symbol = text(c, "symbol");
            OverviewContact {
                initial: if symbol.is_empty() { crate::view::initial(&name) } else { symbol.into() },
                detail: detail.as_str().into(),
                starting: starting == name,
                name: name.into(),
            }
        })
        .collect();
    bridge.set_contacts(ModelRc::new(VecModel::from(contacts)));
    bridge.set_starting(!starting.is_empty());
}

fn with_app(act: impl FnOnce(&Rc<App>, &AppWindow)) {
    if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
        act(&app, &window);
        pump_now(&app);
        fill(&app, &window);
    }
}

/// A card's row in the roster.
fn row(app: &App, session: &str) -> Option<clarp_core::roster::AgentRow> {
    app.engine.borrow().roster().rows().into_iter().find(|r| r.session == session)
}

/// A menu entry ran: the menu closes (QML's Menu does on a trigger).
fn done_with_menu(window: &AppWindow) {
    window.global::<OverviewBridge>().set_menu("".into());
}

pub fn wire(window: &AppWindow) {
    let bridge = window.global::<OverviewBridge>();
    bridge.on_close(|| with_app(|app, window| crate::profile_view::close(app, window)));
    bridge.on_new_agent(|| {
        with_app(|app, window| {
            if !commands::run(app, window, "new") {
                eprintln!("clarp-slint: starting an agent is not available yet");
            }
        });
    });
    bridge.on_orchestrator(|| with_app(|app, window| crate::orchestrator_view::open(app, window)));
    bridge.on_open(|session| {
        with_app(|app, window| {
            app.engine.borrow_mut().select(&session);
            crate::profile_view::close(app, window);
        });
    });
    bridge.on_relaunch(|session| {
        with_app(|app, window| {
            done_with_menu(window);
            app.engine.borrow_mut().select(&session);
            if !commands::run(app, window, "relaunch-agent") {
                eprintln!("clarp-slint: relaunching is not available yet");
            }
        });
    });
    bridge.on_voice(|session| {
        with_app(|app, window| {
            done_with_menu(window);
            let name = app.engine.borrow().chat_name(&session);
            crate::voice_view::open(app, window, &session, &name);
        });
    });
    bridge.on_heartbeat(|session| {
        with_app(|app, window| {
            done_with_menu(window);
            if let Some(row) = row(app, &session) {
                app.engine.borrow_mut().set_agent_heartbeat(&session, !row.heartbeat_enabled);
            }
        });
    });
    bridge.on_dreaming(|session| {
        with_app(|app, window| {
            done_with_menu(window);
            if let Some(row) = row(app, &session) {
                app.engine.borrow_mut().set_agent_dreaming(&session, !row.dreaming_enabled);
            }
        });
    });
    bridge.on_push_alerts(|session| {
        with_app(|app, window| {
            done_with_menu(window);
            if let Some(row) = row(app, &session) {
                app.engine.borrow_mut().set_agent_push_muted(&session, !row.muted);
            }
        });
    });
    bridge.on_archive(|session| {
        with_app(|app, window| {
            done_with_menu(window);
            app.engine.borrow_mut().set_agent_archived(&session, true);
        });
    });
    // The first click asks, the second releases.
    bridge.on_release(|session| {
        with_app(|app, window| {
            let bridge = window.global::<OverviewBridge>();
            if bridge.get_confirm_release() == session {
                bridge.set_confirm_release(SharedString::new());
                done_with_menu(window);
                app.engine.borrow_mut().release_agent(&session);
            } else {
                bridge.set_confirm_release(session.clone());
            }
        });
    });
    bridge.on_restore(|session| with_app(|app, _| app.engine.borrow_mut().set_agent_archived(&session, false)));
    bridge.on_start(|name| {
        with_app(|app, window| {
            let backend = app.engine.borrow().quick_start_backend();
            if app.engine.borrow_mut().quick_start_contact(&name, &backend, "", "") {
                crate::profile_view::close(app, window);
                window.set_surface("chats".into());
            }
        });
    });
    bridge.on_schedule(|id, on| with_app(|app, _| app.engine.borrow_mut().set_schedule_enabled(&id, on)));
}
