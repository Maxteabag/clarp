//! The orchestrator dialog (OrchestratorDialog.qml): its fields take the
//! Host's settings each time they finish loading; Save sends them back.

use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, Model};

use crate::{App, AppWindow, OrchestratorBridge, pump_now};

/// Opens the dialog (the `orchestrator` action, the settings row, the
/// overview) and loads the Host's settings into it.
pub fn open(app: &Rc<App>, window: &AppWindow) {
    window.global::<OrchestratorBridge>().set_loaded(false);
    app.engine.borrow_mut().load_orchestrator();
    crate::profile_view::show(app, window, "orchestrator");
    pump_now(app);
}

pub fn refresh(app: &App, window: &AppWindow, changes: &[Change], reduced: bool) {
    let bridge = window.global::<OrchestratorBridge>();
    bridge.set_reduced_motion(reduced);
    if !changes.contains(&Change::Orchestrator) {
        return;
    }
    let engine = app.engine.borrow();
    bridge.set_loading(engine.orchestrator_loading());
    bridge.set_last_decision(engine.orchestrator_last_decision().into());
    if engine.orchestrator_loading() {
        return;
    }
    let settings = engine.orchestrator_settings();
    let text = |key: &str| settings.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    let index_in = |list: slint::ModelRc<slint::SharedString>, value: &str| list.iter().position(|v| v == value).unwrap_or(0) as i32;
    bridge.set_enabled(settings.get("enabled").and_then(Value::as_bool).unwrap_or(false));
    bridge.set_fallback_only(settings.get("fallback_only").and_then(Value::as_bool) != Some(false));
    bridge.set_confidence(settings.get("confidence_threshold").and_then(Value::as_f64).filter(|c| *c != 0.0).unwrap_or(0.78) as f32);
    let provider = text("provider");
    bridge.set_provider_index(index_in(bridge.get_providers(), if provider.is_empty() { "openai" } else { &provider }));
    let effort = text("effort");
    bridge.set_effort_index(if effort.is_empty() { 0 } else { index_in(bridge.get_efforts(), &effort) });
    bridge.set_model(text("model").into());
    let timeout = settings.get("timeout_ms").and_then(Value::as_i64).filter(|t| *t != 0).unwrap_or(30_000);
    bridge.set_timeout_ms(timeout.clamp(250, 60_000) as i32);
    bridge.set_loaded(true);
}

pub fn wire(window: &AppWindow) {
    let bridge = window.global::<OrchestratorBridge>();
    bridge.on_close(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            crate::profile_view::close(&app, &window);
        }
    });
    bridge.on_save(|| {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        let bridge = window.global::<OrchestratorBridge>();
        let pick = |list: slint::ModelRc<slint::SharedString>, index: i32| list.row_data(index.max(0) as usize).map(|s| s.to_string()).unwrap_or_default();
        let provider = pick(bridge.get_providers(), bridge.get_provider_index());
        // The first effort is the provider's own: sent as none.
        let effort = if bridge.get_effort_index() <= 0 { String::new() } else { pick(bridge.get_efforts(), bridge.get_effort_index()) };
        app.engine.borrow_mut().save_orchestrator(
            bridge.get_enabled(),
            bridge.get_fallback_only(),
            (f64::from(bridge.get_confidence()) * 100.0).round() / 100.0,
            &provider,
            &bridge.get_model(),
            &effort,
            bridge.get_timeout_ms(),
        );
        crate::profile_view::close(&app, &window);
        pump_now(&app);
    });
}
