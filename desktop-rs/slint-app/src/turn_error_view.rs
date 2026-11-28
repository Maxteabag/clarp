//! A failed turn's card (docs/turn-errors.md, Rust Host contract 217): the
//! sentence from the catalog by the Host's key (`clarp_core::turn_error_text`)
//! in the app's language, the buttons that fit, a count for repeats and a
//! closed Details. `clarp_core::turn_error` decides what shows; this fills the
//! card and does what its buttons say.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use clarp_core::json::Object;
use clarp_core::turn_error::{Action, LIVE_PREFIX, Plan, TurnError};
use clarp_core::turn_error_text as text;
use clarp_engine::{Change, Engine, RetryState};
use slint::{ModelRc, VecModel};

use crate::{App, AppWindow, ErrorAction, ErrorCard};

/// What the card's buttons call `ArtifactBridge.open` with:
/// `turn-error:<action>␟<session>␟<card id>`.
const PREFIX: &str = "turn-error:";
const DETAILS: &str = "details";
/// How long "Diagnostics copied" shows.
const COPIED_FOR: Duration = Duration::from_secs(2);

thread_local! {
    /// Cards whose Details are open (`<session>␟<card id>`).
    static OPEN: RefCell<HashSet<String>> = RefCell::default();
    /// Cards whose details were just copied, and when.
    static COPIED: RefCell<HashMap<String, Instant>> = RefCell::default();
}

/// The language in use (`clarp_core::i18n::current()`), read each time a
/// card is drawn so a language change reaches it at once.
pub(crate) fn language() -> &'static str {
    clarp_core::i18n::current()
}

/// A check speaks the language it shows.
#[allow(dead_code)]
pub(crate) fn set_language(language: &'static str) {
    clarp_core::i18n::set_current(language);
}

fn card_key(session: &str, id: &str) -> String {
    format!("{session}\u{1f}{id}")
}

fn key(action: &str, session: &str, id: &str) -> String {
    format!("{PREFIX}{action}\u{1f}{session}\u{1f}{id}")
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// The card's own words, before the pane knows its place: sentence,
/// severity, Details. `finish` adds the rest.
pub(crate) fn card(error: &TurnError) -> ErrorCard {
    let language = language();
    ErrorCard {
        sentence: error.sentence_in(language, now_ms()).into(),
        severity: error.severity.as_str().into(),
        count: 1,
        detail: error.detail.as_str().into(),
        meta: error.meta().into(),
        ..ErrorCard::default()
    }
}

/// The note a pressed retry leaves under the buttons, and whether it says
/// it failed (the Host's own reason stays in the log).
fn retry_note(state: &RetryState, action: &Action, language: &str, now: i64) -> (String, bool) {
    match state {
        RetryState::Sending => (String::new(), false),
        RetryState::Done(status) if status == "scheduled" => {
            let mut params = Object::new();
            if let Some(at) = action.at_ms {
                params.insert("time_ms".into(), at.into());
            }
            (text::say_any(language, "error.retry_scheduled", &params, now).unwrap_or_else(|| text::word(language, "error.retry_sent")), false)
        }
        RetryState::Done(_) => (text::word(language, "error.retry_sent"), false),
        RetryState::Failed(status, words) => {
            eprintln!("clarp-slint: the Host refused the retry ({status}): {words}");
            (text::word(language, "error.action_failed"), true)
        }
    }
}

/// The whole card for `error` drawn as `id` in `session`: its count, the
/// buttons the plan shows with their state, Details open or not and a note.
pub(crate) fn finish(error: &TurnError, id: &str, session: &str, plan: &Plan, engine: &Engine) -> ErrorCard {
    let language = language();
    let mut card = card(error);
    let open_key = card_key(session, id);
    let copied = COPIED.with(|c| c.borrow().get(&open_key).is_some_and(|at| at.elapsed() < COPIED_FOR));
    let mut note = (String::new(), false);
    let mut actions = Vec::new();
    let now = now_ms();
    refresh_at(session, error.next_change_ms(now), now);
    for action in &error.actions_at(now) {
        let shown = match action.id.as_str() {
            // The desktop has no sign-in of its own for a backend.
            "sign_in" => false,
            a if clarp_core::turn_error::retries(a) => plan.shows(id, a) && !error.message_id.is_empty(),
            _ => true,
        };
        if !shown {
            continue;
        }
        let state = if clarp_core::turn_error::retries(&action.id) { engine.turn_retry_state(session, &error.message_id, &action.id) } else { None };
        if let Some(state) = &state {
            let said = retry_note(state, action, language, now);
            if !said.0.is_empty() {
                note = said;
            }
        }
        let done = matches!(state, Some(RetryState::Done(_))) || (action.id == "copy_details" && copied);
        if action.id == "copy_details" && copied {
            note = (text::word(language, "error.copied"), false);
        }
        actions.push(ErrorAction {
            id: action.id.as_str().into(),
            label: action.label_in(language, now).into(),
            key: key(&action.id, session, id).into(),
            primary: actions.is_empty(),
            busy: matches!(state, Some(RetryState::Sending)),
            done,
        });
    }
    let count = plan.count(id);
    card.id = id.into();
    card.count = count as i32;
    card.count_label = if count > 1 { text::repeat_count(language, count).into() } else { Default::default() };
    card.actions = ModelRc::new(VecModel::from(actions));
    card.details_key = key(DETAILS, session, id).into();
    card.details_open = OPEN.with(|o| o.borrow().contains(&open_key));
    card.note = note.0.into();
    card.note_failed = note.1;
    card
}

thread_local! {
    /// The times a chat is drawn again for a `retry_at` that turns into a
    /// plain retry (`<session>␟<ms>`), set once each.
    static DUE: RefCell<HashSet<String>> = RefCell::default();
}

/// Draws `session` again when a card's scheduled retry time passes (within
/// a day; a later one is caught by any redraw before then).
fn refresh_at(session: &str, at: Option<i64>, now: i64) {
    let Some(at) = at.filter(|at| *at - now < 86_400_000) else { return };
    let key = format!("{session}\u{1f}{at}");
    if !DUE.with(|d| d.borrow_mut().insert(key.clone())) {
        return;
    }
    let session = session.to_owned();
    slint::Timer::single_shot(Duration::from_millis((at - now).max(0) as u64 + 50), move || {
        DUE.with(|d| d.borrow_mut().remove(&key));
        if let Some(app) = crate::app() {
            app.refresh(&[Change::Conversation(session)]);
        }
    });
}

/// What decides whether a drawn card changed.
pub(crate) fn signature(card: &ErrorCard) -> String {
    use slint::Model;
    let actions: Vec<String> = card.actions.iter().map(|a| format!("{}:{}:{}", a.id, a.busy, a.done)).collect();
    format!("{}|{}|{}|{}|{}", card.count, actions.join(","), card.details_open, card.note, card.sentence)
}

/// The error card `id` of `session` stands for: its `/log` row's, or the
/// live failed turn's.
fn find(engine: &Engine, session: &str, id: &str) -> Option<TurnError> {
    if let Some(turn_id) = id.strip_prefix(LIVE_PREFIX) {
        let turn = engine.live_view(session)?.turn()?;
        return (clarp_core::json::string(turn, "turn_id") == turn_id).then(|| clarp_core::turn_error::of_live_turn(turn)).flatten();
    }
    engine.conversation(session)?.rows().iter().find(|row| row.id == id).and_then(|row| row.turn_error.clone())
}

/// A card's button or its Details was pressed; false for any other key.
pub(crate) fn open(app: &App, window: &AppWindow, key: &str) -> bool {
    let Some(rest) = key.strip_prefix(PREFIX) else { return false };
    let mut parts = rest.splitn(3, '\u{1f}');
    let (Some(action), Some(session), Some(id)) = (parts.next(), parts.next(), parts.next()) else { return true };
    let open_key = card_key(session, id);
    let refresh = |app: &App| app.refresh(&[Change::Conversation(session.to_owned())]);
    if action == DETAILS {
        OPEN.with(|o| {
            let mut open = o.borrow_mut();
            if !open.remove(&open_key) {
                open.insert(open_key.clone());
            }
        });
        refresh(app);
        return true;
    }
    let Some(error) = find(&app.engine.borrow(), session, id) else {
        eprintln!("clarp-slint: no failure card {id} in {session}");
        return true;
    };
    match action {
        "copy_details" => {
            match crate::platform::clipboard::copy(&error.clipboard()) {
                Ok(()) => {
                    COPIED.with(|c| c.borrow_mut().insert(open_key, Instant::now()));
                    let session = session.to_owned();
                    slint::Timer::single_shot(COPIED_FOR + Duration::from_millis(50), move || {
                        if let Some(app) = crate::app() {
                            app.refresh(&[Change::Conversation(session)]);
                        }
                    });
                }
                Err(error) => app.engine.borrow_mut().report_error(&error),
            }
            refresh(app);
        }
        "switch_model" => {
            if let Some(app) = crate::app() {
                crate::profile_view::open(&app, window, session);
                use slint::ComponentHandle;
                // Its Backend picker where the Host moves agents; the
                // profile's model row otherwise.
                window.global::<crate::ProfileBridge>().invoke_open_transit();
            }
        }
        id if clarp_core::turn_error::retries(id) => {
            let pressed: Option<Action> = error.actions_at(chrono::Utc::now().timestamp_millis()).into_iter().find(|a| a.id == id);
            if let Some(pressed) = pressed {
                app.engine.borrow_mut().turn_retry(session, &error, &pressed);
            }
            refresh(app);
        }
        _ => {}
    }
    true
}
