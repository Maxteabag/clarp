//! The voice dialog (VoiceDialog.qml): an agent's voices, a preview the
//! Host speaks (only asked for here; playback is the audio step's) and the
//! choice of a free one.

use std::cell::RefCell;
use std::rc::Rc;

use clarp_engine::Change;
use slint::{ComponentHandle, ModelRc, VecModel};

use crate::{App, AppWindow, VoiceBridge, VoiceItem, pump_now};

thread_local! {
    /// The agent the dialog is for: (session, name).
    static FOR: RefCell<(String, String)> = RefCell::new((String::new(), String::new()));
}

/// Opens `session`'s voices over whatever dialog is showing.
pub fn open(app: &Rc<App>, window: &AppWindow, session: &str, name: &str) {
    if session.is_empty() {
        return;
    }
    FOR.with(|f| *f.borrow_mut() = (session.to_owned(), name.to_owned()));
    window.global::<VoiceBridge>().set_agent_name(name.into());
    app.engine.borrow_mut().load_voices(session);
    crate::profile_view::show(app, window, "voices");
    pump_now(app);
    fill(app, window);
}

pub fn refresh(app: &App, window: &AppWindow, changes: &[Change], reduced: bool) {
    window.global::<VoiceBridge>().set_reduced_motion(reduced);
    if *app.overlay.borrow() == "voices" && changes.iter().any(|c| matches!(c, Change::Voices | Change::Roster)) {
        fill(app, window);
    }
}

fn fill(app: &App, window: &AppWindow) {
    let bridge = window.global::<VoiceBridge>();
    let engine = app.engine.borrow();
    let voices: Vec<VoiceItem> = engine
        .voices()
        .iter()
        .map(|voice| VoiceItem {
            id: voice.id.as_str().into(),
            label: voice.label.as_str().into(),
            status: if voice.current {
                "Current".to_owned()
            } else if !voice.taken_by.is_empty() {
                format!("Used by {}", voice.taken_by)
            } else {
                "Available".to_owned()
            }
            .into(),
            current: voice.current,
            taken: !voice.taken_by.is_empty(),
        })
        .collect();
    bridge.set_voices(ModelRc::new(VecModel::from(voices)));
    bridge.set_bio(engine.voice_bio().into());
    bridge.set_loading(engine.voices_loading());
}

fn with_agent(act: impl FnOnce(&Rc<App>, &str, &str)) {
    let (session, name) = FOR.with(|f| f.borrow().clone());
    if let (Some(app), false) = (crate::app(), session.is_empty()) {
        act(&app, &session, &name);
        pump_now(&app);
    }
}

pub fn wire(window: &AppWindow) {
    let bridge = window.global::<VoiceBridge>();
    bridge.on_close(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            crate::profile_view::close(&app, &window);
        }
    });
    bridge.on_preview(|id| with_agent(|app, session, name| app.engine.borrow_mut().preview_voice(session, name, &id)));
    bridge.on_choose(|id| with_agent(|app, session, _| app.engine.borrow_mut().choose_voice(session, &id)));
}
