//! A failed turn's card (docs/turn-errors.md, Rust Host contract 217): the
//! Host's sentence in the device's language, the buttons that fit, a count
//! for repeats and a closed Details. `clarp_core::turn_error` decides what
//! shows; this fills the card and does what its buttons say.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use clarp_core::turn_error::{Action, LIVE_PREFIX, Plan, TurnError};
use clarp_engine::{Change, Engine, RetryState};
use slint::{ModelRc, VecModel};

use crate::{App, AppWindow, ErrorAction, ErrorCard};

/// What the card's buttons call `ArtifactBridge.open` with:
/// `turn-error:<action>␟<session>␟<card id>`.
const PREFIX: &str = "turn-error:";
const DETAILS: &str = "details";
/// How long "Copied" shows.
const COPIED_FOR: Duration = Duration::from_secs(2);

thread_local! {
    /// The language the cards speak: the device's, or a check's choice.
    static NORWEGIAN: Cell<Option<bool>> = const { Cell::new(None) };
    /// Cards whose Details are open (`<session>␟<card id>`).
    static OPEN: RefCell<HashSet<String>> = RefCell::default();
    /// Cards whose details were just copied, and when.
    static COPIED: RefCell<HashMap<String, Instant>> = RefCell::default();
}

/// Whether the cards speak Norwegian (the device prefers `nb`, `nn`, `no`).
pub(crate) fn norwegian() -> bool {
    NORWEGIAN.with(|n| {
        if n.get().is_none() {
            n.set(Some(clarp_core::turn_error::device_prefers_norwegian()));
        }
        n.get().unwrap_or(false)
    })
}

/// A check speaks the language it shows.
#[allow(dead_code)]
pub(crate) fn set_norwegian(on: bool) {
    NORWEGIAN.with(|n| n.set(Some(on)));
}

fn card_key(session: &str, id: &str) -> String {
    format!("{session}\u{1f}{id}")
}

fn key(action: &str, session: &str, id: &str) -> String {
    format!("{PREFIX}{action}\u{1f}{session}\u{1f}{id}")
}

fn say(nb: bool, norwegian: &str, english: &str) -> String {
    if nb { norwegian } else { english }.to_owned()
}

/// The card's own words, before the pane knows its place: sentence,
/// severity, Details. `finish` adds the rest.
pub(crate) fn card(error: &TurnError) -> ErrorCard {
    let nb = norwegian();
    ErrorCard {
        sentence: error.sentence(nb).into(),
        severity: error.severity.as_str().into(),
        count: 1,
        detail: error.detail.as_str().into(),
        meta: error.meta().into(),
        details_label: if nb { "Detaljer" } else { "Details" }.into(),
        ..ErrorCard::default()
    }
}

/// The note a pressed retry leaves under the buttons, and whether it says
/// it failed.
fn retry_note(state: &RetryState, nb: bool) -> (String, bool) {
    match state {
        RetryState::Sending => (String::new(), false),
        RetryState::Done(status) => (
            match status.as_str() {
                "scheduled" => say(nb, "Planlagt.", "Scheduled."),
                "compacting" => say(nb, "Komprimerer samtalen, så sendes meldingen på nytt.", "Compacting the conversation, then sending the message again."),
                "refreshing" => say(nb, "Starter en ny økt med samme samtale.", "Starting a fresh session with the same conversation."),
                _ => say(nb, "Sendt på nytt.", "Sent again."),
            },
            false,
        ),
        RetryState::Failed(404, _) => (say(nb, "Meldingen finnes ikke lenger i samtalen.", "The message is no longer in the conversation."), true),
        RetryState::Failed(409, _) => (say(nb, "Agenten har allerede svart på en nyere melding.", "The agent has already answered a newer message."), true),
        RetryState::Failed(_, words) => {
            let words = words.trim();
            let lead = say(nb, "Kunne ikke sende den på nytt", "Could not send it again");
            (if words.is_empty() { format!("{lead}.") } else { format!("{lead}: {words}") }, true)
        }
    }
}

/// The whole card for `error` drawn as `id` in `session`: its count, the
/// buttons the plan shows with their state, Details open or not and a note.
pub(crate) fn finish(error: &TurnError, id: &str, session: &str, plan: &Plan, engine: &Engine) -> ErrorCard {
    let nb = norwegian();
    let mut card = card(error);
    let open_key = card_key(session, id);
    let copied = COPIED.with(|c| c.borrow().get(&open_key).is_some_and(|at| at.elapsed() < COPIED_FOR));
    let mut note = (String::new(), false);
    let mut actions = Vec::new();
    for action in &error.actions {
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
            let said = retry_note(state, nb);
            if !said.0.is_empty() {
                note = said;
            }
        }
        let done = matches!(state, Some(RetryState::Done(_))) || (action.id == "copy_details" && copied);
        if action.id == "copy_details" && copied {
            note = (say(nb, "Kopiert.", "Copied."), false);
        }
        actions.push(ErrorAction {
            id: action.id.as_str().into(),
            label: action.label.get(nb).into(),
            key: key(&action.id, session, id).into(),
            primary: actions.is_empty(),
            busy: matches!(state, Some(RetryState::Sending)),
            done,
        });
    }
    card.id = id.into();
    card.count = plan.count(id) as i32;
    card.actions = ModelRc::new(VecModel::from(actions));
    card.details_key = key(DETAILS, session, id).into();
    card.details_open = OPEN.with(|o| o.borrow().contains(&open_key));
    card.note = note.0.into();
    card.note_failed = note.1;
    card
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
            let pressed: Option<Action> = error.actions.iter().find(|a| a.id == id).cloned();
            if let Some(pressed) = pressed {
                app.engine.borrow_mut().turn_retry(session, &error, &pressed);
            }
            refresh(app);
        }
        _ => {}
    }
    true
}
