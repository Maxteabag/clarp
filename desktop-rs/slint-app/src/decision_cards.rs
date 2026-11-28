//! Decision and question cards, in the chat and in Updates: one click on an
//! answer sends it and folds the card to one line ("You chose: B · Pebble")
//! with Undo while the Host still holds the answer back from the agent; the
//! user's own answer sends the same way. On Updates a held microphone takes
//! a spoken answer, which the Host matches to an answer.

use std::cell::RefCell;
use std::collections::HashMap;

use clarp_engine::cards::{CardFeatures, UNDO_MS, UndoOutcome};
use clarp_engine::{Change, Engine};
use serde_json::{Value, json};
use slint::Model;

use crate::artifacts_view::{artifact_item, clock_now, number, pump, text};
use crate::{App, ArtifactItem};

/// How long "Too late to undo" shows, in the cards' clock seconds.
const NOTE_SECONDS: i32 = 2;
/// How long a card answered in Updates stays there, folded, after its
/// undo window (seconds).
const LINGER_SECONDS: i32 = 4;
/// How long a sent answer shows filled before its card folds.
const FLASH: std::time::Duration = std::time::Duration::from_millis(150);
/// A press on the microphone shorter than this is a click: it says to hold.
const HOLD_MS: u128 = 350;

/// An answer this window sent, until the Host's card says it (and after,
/// for its undo and what was heard).
#[derive(Debug, Clone, Default)]
struct Sent {
    /// "chose" or "answered", and with what.
    kind: String,
    value: String,
    /// The user's own words, put back in the field when undone.
    own: Option<String>,
    /// When it was sent, on the cards' clock, and exactly (the clicked
    /// answer shows filled for a moment before the card folds).
    clock: i32,
    at: Option<std::time::Instant>,
    /// Taken back: the card is open again.
    undone: bool,
    /// "too-late", or why an undo failed, until `note_until`.
    note: String,
    note_until: i32,
    /// A spoken answer's words.
    heard: String,
    /// The decision's revision when it was sent: a pending one past it was
    /// opened again by the Host (an undo here or on another device).
    revision: i64,
    /// Updates keeps the folded card until then, drawn from the Host's
    /// artifact or, for a card listed only as attention, as it was sent.
    shown_until: i32,
    artifact: Option<Value>,
}

/// What the card's voice is doing now.
#[derive(Debug, Clone, Default)]
struct Voice {
    /// The card recording, and since when.
    card: String,
    started: Option<std::time::Instant>,
    /// A card's note ("hold", "nothing", "listening" or a failure) until a
    /// clock second.
    notes: HashMap<String, (String, i32)>,
    /// A transcript waiting for the Host's match.
    matching: HashMap<String, String>,
    /// The card the voice moved to ("next").
    focus: String,
}

thread_local! {
    static SENT: RefCell<HashMap<String, Sent>> = RefCell::default();
    static VOICE: RefCell<Voice> = RefCell::default();
}

/// An answer's letter: A, B, C ... (past Z, its number).
pub fn letter(index: usize) -> String {
    if index < 26 { char::from(b'A' + index as u8).to_string() } else { (index + 1).to_string() }
}

/// Text for one line: its whitespace as single spaces, at most 120
/// characters (the line elides the rest to the card's width).
pub fn one_line(value: &str) -> String {
    let flat = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= 120 { flat } else { flat.chars().take(119).collect::<String>() + "…" }
}

/// An attention item as an artifact a card can be drawn from (its
/// decision under `decision`, its kind as the artifact's type).
pub fn attention_artifact(item: &Value) -> Value {
    let id = Some(text(item, "artifact_id")).filter(|id| !id.is_empty()).unwrap_or_else(|| text(item, "decision_id"));
    let kind = if text(item, "kind") == "question" || !matches!(text(item, "response_type").as_str(), "" | "approval") { "question" } else { "decision" };
    // The inbox lists open requests: one without a status is open.
    let mut decision = item.clone();
    if text(item, "status").is_empty() {
        decision["status"] = json!("pending");
    }
    json!({
        "artifact_id": id,
        "type": kind,
        "title": text(item, "title"),
        "session": text(item, "session"),
        "status": "active",
        "decision": decision,
    })
}

/// The artifact a decision card is drawn from: the Updates' (or an agent's
/// publications'), else its attention item.
pub fn decision_artifact(app: &App, id: &str) -> Option<Value> {
    let engine = app.engine.borrow();
    engine.artifact_by_id(id).filter(|a| a.get("decision").is_some_and(Value::is_object)).or_else(|| {
        engine
            .attention_items()
            .iter()
            .find(|item| {
                let artifact = text(item, "artifact_id");
                artifact == id || (artifact.is_empty() && text(item, "decision_id") == id)
            })
            .map(attention_artifact)
    })
}

/// The card as it stands now (pending, folded, its answers), wherever it shows.
pub fn current(app: &App, id: &str) -> Option<ArtifactItem> {
    let artifact = decision_artifact(app, id)?;
    let mut item = artifact_item(&artifact);
    let engine = app.engine.borrow();
    item.status_text = engine.artifact_status(&item.id).into();
    apply(&mut item, &artifact, &engine);
    Some(item)
}

/// The Host's features and this window's answers on a card: an approval's
/// own answer row only where the Host takes words on one, the card folded
/// at once after a click, Undo while the Host still holds the answer, a
/// note for a moment, the words a spoken answer was heard as.
pub fn apply(item: &mut ArtifactItem, artifact: &Value, engine: &Engine) {
    let features = engine.card_features(&item.id);
    if item.approval && !features.approval_text {
        item.allow_custom = false;
    }
    let now = clock_now();
    let sent = SENT.with(|s| s.borrow().get(item.id.as_str()).cloned());
    let failed = item.status_text.starts_with("Not ");
    let decision = artifact.get("decision").cloned().unwrap_or(Value::Null);
    let undoing = engine.undo_in_flight(&item.id, &text(&decision, "decision_id"));
    if item.pending {
        let folding = sent.as_ref().and_then(|s| s.at).is_some_and(|at| at.elapsed() < FLASH);
        if folding {
            item.chosen = app_choice(&item.id);
        }
        // Open again on the Host (undone here or elsewhere): the Host wins.
        let reopened = sent.as_ref().is_some_and(|s| number(&decision, "revision") > s.revision);
        if let Some(sent) = sent.as_ref().filter(|s| !s.undone && !failed && !folding && !reopened) {
            item.collapsed = true;
            item.collapsed_kind = sent.kind.clone().into();
            item.collapsed_value = one_line(&sent.value).into();
            item.collapsed_ok = true;
            if features.undo {
                item.undo_end = sent.clock + (UNDO_MS / 1000) as i32;
            }
        }
    } else if features.undo {
        let until = number(&decision, "undo_until");
        if decision.get("undoable").and_then(Value::as_bool) == Some(true) && until > 0 {
            let left = until - chrono::Utc::now().timestamp_millis();
            if left > 0 {
                item.undo_end = now + ((left + 999) / 1000) as i32;
            }
            // Sent from here: never past the window asked for, whatever
            // the two clocks say.
            if let Some(sent) = sent.as_ref().filter(|s| s.at.is_some() && !s.undone) {
                item.undo_end = item.undo_end.min(sent.clock + (UNDO_MS / 1000) as i32);
            }
        }
    }
    // One undo at a time: Undo hides while it is on its way.
    if undoing {
        item.undo_end = 0;
    }
    if let Some(sent) = sent {
        if sent.note_until > now {
            item.undo_note = sent.note.into();
            refresh_in(std::time::Duration::from_secs((sent.note_until - now) as u64) + std::time::Duration::from_millis(50));
        }
        item.heard = sent.heard.into();
    }
    VOICE.with(|v| {
        let voice = v.borrow();
        if let Some((note, until)) = voice.notes.get(item.id.as_str()).filter(|(_, until)| *until > now) {
            item.voice_note = note.clone().into();
            refresh_in(std::time::Duration::from_secs((until - now) as u64) + std::time::Duration::from_millis(50));
        }
        if voice.card == item.id.as_str() {
            item.voice_note = "listening".into();
        }
        item.voice_focus = voice.focus == item.id.as_str();
    });
}

fn decision(artifact: &Value) -> Value {
    artifact.get("decision").cloned().unwrap_or(Value::Null)
}

thread_local! {
    /// The card whose own-answer field has the keyboard ("" for none).
    static FIELD: RefCell<String> = const { RefCell::new(String::new()) };
}

/// A card's own-answer field took or left the keyboard.
pub fn field_focus(id: &str, focused: bool) {
    FIELD.with(|f| {
        let mut field = f.borrow_mut();
        if focused {
            *field = id.to_owned();
        } else if field.as_str() == id {
            field.clear();
        }
    });
}

/// The card folds and its field goes with it: a field that had the
/// keyboard says nothing as it goes, so the keyboard is given back here
/// (or letters would stay typing into nothing).
fn release_field(app: &App, id: &str) {
    let had = FIELD.with(|f| {
        let mut field = f.borrow_mut();
        let had = field.as_str() == id;
        if had {
            field.clear();
        }
        had
    });
    if !had {
        return;
    }
    app.artifact_editing.borrow_mut().clear();
    if let Some(window) = crate::window() {
        use slint::ComponentHandle;
        window.global::<crate::ArtifactBridge>().set_editing(false);
        if window.get_surface() == "chats" {
            app.focus_transcript();
        }
    }
}

/// Resolves with the undo window where the Host has it.
fn resolve(app: &App, id: &str, artifact: &Value, mut body: Value, sent: Sent) {
    release_field(app, id);
    let decision = decision(artifact);
    let undo = app.engine.borrow().card_features(id).undo;
    if undo {
        body["undo_ms"] = json!(UNDO_MS);
    }
    // Without an undo there is nothing to keep it in Updates for.
    let shown_until = if undo { sent.clock + (UNDO_MS / 1000) as i32 + LINGER_SECONDS } else { sent.clock };
    let revision = number(&decision, "revision");
    SENT.with(|s| s.borrow_mut().insert(id.to_owned(), Sent { shown_until, revision, artifact: Some(artifact.clone()), at: Some(std::time::Instant::now()), ..sent }));
    if undo {
        // Updates lets the folded card go then.
        refresh_in(std::time::Duration::from_secs((shown_until - clock_now()).max(0) as u64 + 1));
    }
    app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "resolve", body, number(&decision, "revision"));
    app.refresh(&[Change::Updates]);
    pump();
    // The card folds once the filled row has shown.
    slint::Timer::single_shot(FLASH + std::time::Duration::from_millis(10), || {
        if let Some(app) = crate::app() {
            app.refresh(&[Change::Updates]);
        }
    });
}

/// Draws the cards again after `after` (a note or a folded card's time is
/// up): one timer, for the earliest such moment.
pub fn refresh_in(after: std::time::Duration) {
    thread_local! {
        static DUE: RefCell<Option<(std::time::Instant, slint::Timer)>> = const { RefCell::new(None) };
    }
    let at = std::time::Instant::now() + after;
    DUE.with(|due| {
        let mut due = due.borrow_mut();
        if due.as_ref().is_some_and(|(when, timer)| timer.running() && *when <= at) {
            return;
        }
        let timer = slint::Timer::default();
        timer.start(slint::TimerMode::SingleShot, after, || {
            if let Some(app) = crate::app() {
                app.refresh(&[Change::Updates]);
            }
        });
        *due = Some((at, timer));
    });
}

/// The card was answered from here a moment ago and not taken back.
fn just_sent(id: &str) -> bool {
    SENT.with(|s| s.borrow().get(id).is_some_and(|sent| !sent.undone && sent.at.is_some_and(|at| at.elapsed() < std::time::Duration::from_secs(2))))
}

/// The answer this window chose on a card (-1 for none).
fn app_choice(id: &str) -> i32 {
    crate::app().and_then(|app| app.artifact_choices.borrow().get(id).copied()).unwrap_or(-1)
}

/// A click (or a digit) on an answer: sent at once.
pub fn answer(app: &App, id: &str, index: i32) {
    answer_heard(app, id, index, "");
}

fn answer_heard(app: &App, id: &str, index: i32, heard: &str) {
    let Some(artifact) = decision_artifact(app, id) else { return };
    let Some(card) = current(app, id).filter(|c| c.pending && !c.collapsed) else { return };
    let Some(option) = usize::try_from(index).ok().and_then(|i| card.options.row_data(i)) else { return };
    let body = if card.approval {
        json!({"choice": if option.id == "yes" { "accepted" } else { "rejected" }})
    } else {
        json!({"answer": {"option_id": option.id.as_str()}})
    };
    let value = if card.approval { option.label.to_string() } else { format!("{} · {}", option.badge, option.label) };
    app.artifact_choices.borrow_mut().insert(id.to_owned(), index);
    resolve(app, id, &artifact, body, Sent { kind: "chose".into(), value, clock: clock_now(), heard: heard.to_owned(), ..Sent::default() });
}

/// The user's own answer, as written in the card's field.
pub fn send_own(app: &App, id: &str) {
    // Answers of one's own are capped at 4,000 characters.
    let written: String = app.artifact_drafts.borrow().get(id).map(|d| d.trim().chars().take(4000).collect()).unwrap_or_default();
    send_words(app, id, &written, "");
}

fn send_words(app: &App, id: &str, written: &str, heard: &str) {
    let Some(artifact) = decision_artifact(app, id) else { return };
    let Some(card) = current(app, id).filter(|c| c.pending && !c.collapsed && c.allow_custom) else { return };
    if written.is_empty() || card.id.is_empty() {
        return;
    }
    let own_answer = app.artifact_editing.borrow().as_str() == id;
    if own_answer {
        app.artifact_editing.borrow_mut().clear();
        // Sent from the chat: the keyboard is back on it.
        if crate::window().is_some_and(|w| w.get_surface() == "chats") {
            app.focus_transcript();
        }
    }
    let sent = Sent { kind: "answered".into(), value: written.to_owned(), own: Some(written.to_owned()), clock: clock_now(), heard: heard.to_owned(), ..Sent::default() };
    resolve(app, id, &artifact, json!({"answer": {"text": written}}), sent);
}

/// Undo on a folded card: the Host takes the answer back while the agent
/// has not started on it.
pub fn undo(app: &App, id: &str) {
    // A native form answered with one click.
    if app.engine.borrow().render_spec(id).is_some() {
        app.engine.borrow_mut().undo_render_spec(id);
        pump();
        return;
    }
    let Some(artifact) = decision_artifact(app, id) else { return };
    let decision = decision(&artifact);
    let pending = text(&decision, "status") == "pending";
    // Sent but not yet listed as answered: its revision is one on.
    let revision = number(&decision, "revision") + i64::from(pending);
    // A card answered elsewhere (another window, before a restart) gets an
    // entry for its undo's outcome.
    note_target(id);
    app.engine.borrow_mut().undo_decision(id, &text(&decision, "decision_id"), revision);
    pump();
}

/// What the engine says came of the cards' undos and spoken answers.
pub fn settle(app: &App) {
    let ids: Vec<String> = SENT.with(|s| s.borrow().keys().cloned().collect());
    let now = clock_now();
    for id in ids {
        let Some(outcome) = app.engine.borrow_mut().take_undo_outcome(&id) else { continue };
        SENT.with(|s| {
            let mut sent = s.borrow_mut();
            let Some(entry) = sent.get_mut(&id) else { return };
            match outcome {
                UndoOutcome::Undone => {
                    entry.undone = true;
                    entry.shown_until = now + LINGER_SECONDS;
                    if let Some(own) = entry.own.clone() {
                        app.artifact_drafts.borrow_mut().insert(id.clone(), own);
                    }
                }
                UndoOutcome::TooLate => {
                    entry.note = "too-late".into();
                    entry.note_until = now + NOTE_SECONDS;
                }
                UndoOutcome::Failed(why) => {
                    entry.note = why;
                    entry.note_until = now + NOTE_SECONDS;
                }
            }
        });
        app.artifact_choices.borrow_mut().remove(&id);
    }
    let waiting: Vec<String> = VOICE.with(|v| v.borrow().matching.keys().cloned().collect());
    for id in waiting {
        let Some(result) = app.engine.borrow_mut().take_decision_match(&id) else { continue };
        let heard = VOICE.with(|v| v.borrow_mut().matching.remove(&id)).unwrap_or_default();
        matched(app, &id, &heard, result);
    }
    // Answers long settled are forgotten.
    SENT.with(|s| s.borrow_mut().retain(|_, sent| sent.shown_until + 60 > now));
}

/// Undo on a card this window did not answer: an entry to hold its note.
pub fn note_target(id: &str) {
    SENT.with(|s| {
        s.borrow_mut().entry(id.to_owned()).or_insert_with(|| Sent { clock: clock_now(), shown_until: clock_now(), ..Sent::default() });
    });
}

/// A card drawn again with the same answers and hints keeps the old
/// models, so its answer rows are not rebuilt under a press (a click that
/// spans a redraw would be lost) and the row compares equal when nothing
/// else changed.
pub fn keep_models(old: &ArtifactItem, new: &mut ArtifactItem) {
    fn same<T: Clone + PartialEq + 'static>(a: &slint::ModelRc<T>, b: &slint::ModelRc<T>) -> bool {
        a.row_count() == b.row_count() && a.iter().zip(b.iter()).all(|(x, y)| x == y)
    }
    if same(&old.options, &new.options) {
        new.options = old.options.clone();
    }
    if same(&old.hints, &new.hints) {
        new.hints = old.hints.clone();
    }
    if same(&old.head, &new.head) {
        new.head = old.head.clone();
    }
    if same(&old.first_row, &new.first_row) {
        new.first_row = old.first_row.clone();
    }
}

// ---- Updates ---------------------------------------------------------------

/// The cards Updates lists: what needs the user, in the Host's order, and
/// the ones answered there a moment ago (folded, with Undo) where they were.
pub fn updates_cards(app: &App) -> Vec<(ArtifactItem, Value)> {
    thread_local! {
        static ORDER: RefCell<Vec<String>> = RefCell::default();
    }
    let engine = app.engine.borrow();
    let now = clock_now();
    let mut cards: Vec<(ArtifactItem, Value)> = engine
        .attention_items()
        .iter()
        .map(|item| {
            let artifact = attention_artifact(item);
            let mut card = artifact_item(&artifact);
            card.status_text = engine.artifact_status(&card.id).into();
            apply(&mut card, &artifact, &engine);
            fill_draft(app, &mut card);
            (card, item.clone())
        })
        .collect();
    let lingering: Vec<(String, Option<Value>)> =
        SENT.with(|s| s.borrow().iter().filter(|(_, sent)| sent.shown_until > now).map(|(id, sent)| (id.clone(), sent.artifact.clone())).collect());
    let previous = ORDER.with(|o| o.borrow().clone());
    for (id, snapshot) in lingering {
        if cards.iter().any(|(card, _)| card.id.as_str() == id) {
            continue;
        }
        let listed = engine.artifact_by_id(&id).filter(|a| a.get("decision").is_some_and(Value::is_object));
        let Some(artifact) = listed.or(snapshot) else { continue };
        let mut card = artifact_item(&artifact);
        card.status_text = engine.artifact_status(&card.id).into();
        apply(&mut card, &artifact, &engine);
        fill_draft(app, &mut card);
        let at = previous.iter().position(|p| *p == id).unwrap_or(cards.len()).min(cards.len());
        let asked = artifact.get("decision").cloned().unwrap_or(Value::Null);
        let item = json!({"agent_name": text(&asked, "agent_name"), "session": text(&artifact, "session"), "host": text(&asked, "host")});
        cards.insert(at, (card, item));
    }
    ORDER.with(|o| *o.borrow_mut() = cards.iter().map(|(card, _)| card.id.to_string()).collect());
    // Each folded card asks to be drawn again when its time in Updates is up.
    SENT.with(|s| {
        for sent in s.borrow().values().filter(|sent| sent.shown_until > now) {
            refresh_in(std::time::Duration::from_secs((sent.shown_until - now) as u64) + std::time::Duration::from_millis(50));
        }
    });
    // A recording for a card that left Updates (answered elsewhere,
    // withdrawn) has nothing to answer: it stops.
    let gone = VOICE.with(|v| {
        let voice = v.borrow();
        (!voice.card.is_empty() && !cards.iter().any(|(card, _)| card.id.as_str() == voice.card)).then(|| voice.card.clone())
    });
    if let Some(card) = gone {
        VOICE.with(|v| {
            let mut voice = v.borrow_mut();
            voice.card.clear();
            voice.started = None;
        });
        crate::platform::audio::with(|audio| {
            if audio.recording_target() == target(&card) {
                audio.cancel_recording();
            }
        });
    }
    cards
}

fn fill_draft(app: &App, card: &mut ArtifactItem) {
    if card.pending && !card.collapsed {
        card.draft = app.artifact_drafts.borrow().get(card.id.as_str()).cloned().unwrap_or_default().into();
    }
}

// ---- voice -----------------------------------------------------------------

/// The command words a spoken answer may be, in the app's language.
fn command_words() -> Value {
    // i18n: a word the user says aloud to a decision card to skip to the next one (one or two short spoken words, not a button label)
    let next = clarp_core::tr!("voice.cmd.next" => "next");
    // i18n: a word the user says aloud to take back the answer just given (one or two short spoken words, not a button label)
    let undo = clarp_core::tr!("voice.cmd.undo" => "undo");
    // i18n: a word the user says aloud to stop answering by voice (one or two short spoken words, not a button label)
    let cancel = clarp_core::tr!("voice.cmd.cancel" => "cancel");
    json!({"next": [next], "undo": [undo], "cancel": [cancel]})
}

fn voice_note(id: &str, note: &str, seconds: i32) {
    VOICE.with(|v| {
        v.borrow_mut().notes.insert(id.to_owned(), (note.to_owned(), clock_now() + seconds));
    });
    refresh_in(std::time::Duration::from_secs(seconds.max(0) as u64) + std::time::Duration::from_millis(50));
}

fn target(id: &str) -> String {
    format!("decision:{id}")
}

/// The microphone held down: recording starts for this card.
pub fn voice_press(app: &App, id: &str) {
    let (recording, held) = crate::platform::audio::with(|audio| (audio.recording(), audio.recording_target())).unwrap_or_default();
    if recording {
        // A card's recording whose release went missing (the pointer let go
        // elsewhere) is dropped, not sent: this press starts afresh.
        if !held.starts_with("decision:") {
            return;
        }
        crate::platform::audio::with(|audio| audio.cancel_recording());
    }
    VOICE.with(|v| {
        let mut voice = v.borrow_mut();
        voice.card = id.to_owned();
        voice.started = Some(std::time::Instant::now());
        voice.focus.clear();
    });
    crate::platform::audio::with(|audio| audio.toggle_recording_for_session(&target(id)));
    app.refresh(&[Change::Updates]);
}

/// The microphone let go: a hold is transcribed and answers the card; a
/// click says to hold.
pub fn voice_release(app: &App, id: &str) {
    let (card, started) = VOICE.with(|v| {
        let mut voice = v.borrow_mut();
        (std::mem::take(&mut voice.card), voice.started.take())
    });
    if card != id {
        return;
    }
    let held = started.map_or(0, |s| s.elapsed().as_millis());
    crate::platform::audio::with(|audio| {
        if audio.recording() && audio.recording_target() == target(id) {
            if held < HOLD_MS {
                audio.cancel_recording();
            } else {
                audio.toggle_recording_for_session(&target(id));
            }
        }
    });
    if held < HOLD_MS {
        voice_note(id, "hold", 2);
    } else {
        voice_note(id, "transcribing", 30);
    }
    app.refresh(&[Change::Updates]);
}

/// A dictation for a card came back as text: the Host says what it means,
/// or on an older Host the words are the user's own answer.
pub fn heard(app: &App, target: &str, words: &str) -> bool {
    let Some(id) = target.strip_prefix("decision:") else { return false };
    VOICE.with(|v| v.borrow_mut().notes.remove(id));
    let words = words.trim();
    if words.is_empty() {
        voice_note(id, "nothing", 2);
        app.refresh(&[Change::Updates]);
        return true;
    }
    let features: CardFeatures = app.engine.borrow().card_features(id);
    let Some(artifact) = decision_artifact(app, id) else { return true };
    if !features.voice_match {
        own_words(app, id, words, words);
        return true;
    }
    let decision = decision(&artifact);
    VOICE.with(|v| v.borrow_mut().matching.insert(id.to_owned(), words.to_owned()));
    app.engine.borrow_mut().match_decision(id, &text(&decision, "decision_id"), words, command_words());
    pump();
    true
}

fn matched(app: &App, id: &str, words: &str, result: Result<Value, String>) {
    let reply = match result {
        Ok(reply) => reply,
        Err(why) => {
            voice_note(id, &why, 3);
            app.refresh(&[Change::Updates]);
            return;
        }
    };
    let Some(card) = current(app, id) else { return };
    match text(&reply, "kind").as_str() {
        "option" => {
            let option = text(&reply, "option_id");
            match card.options.iter().position(|o| o.id.as_str() == option) {
                Some(index) => answer_heard(app, id, index as i32, words),
                None => own_words(app, id, words, words),
            }
        }
        "approval" => {
            let index = if text(&reply, "choice") == "accepted" { 0 } else { 1 };
            answer_heard(app, id, index, words);
        }
        "command" => match text(&reply, "command").as_str() {
            "next" => next_card(app, id),
            "undo" => {
                let last = SENT.with(|s| s.borrow().iter().filter(|(_, sent)| !sent.undone && !sent.kind.is_empty()).max_by_key(|(_, sent)| sent.clock).map(|(id, _)| id.clone()));
                if let Some(last) = last {
                    undo(app, &last);
                }
            }
            _ => {}
        },
        _ => {
            let said = Some(text(&reply, "text")).filter(|t| !t.is_empty()).unwrap_or_else(|| words.to_owned());
            own_words(app, id, &said, words);
        }
    }
    // Answered (folding now): the next card waiting takes the voice.
    if just_sent(id) {
        next_card(app, id);
    }
    app.refresh(&[Change::Updates]);
}

/// Spoken words as the user's own answer, where the card takes one; else
/// the card says it heard nothing it could use.
fn own_words(app: &App, id: &str, said: &str, words: &str) {
    if current(app, id).is_some_and(|c| c.allow_custom) {
        send_words(app, id, said, words);
    } else {
        voice_note(id, "nothing", 2);
        app.refresh(&[Change::Updates]);
    }
}

/// The card's microphone works: the Host matches spoken answers, or the
/// card takes the words as the user's own answer.
pub fn voice_usable(app: &App, card: &ArtifactItem) -> bool {
    card.allow_custom || app.engine.borrow().card_features(&card.id).voice_match
}

/// The next card waiting in Updates after `id` takes the voice's focus.
fn next_card(app: &App, id: &str) {
    let cards = updates_cards(app);
    let at = cards.iter().position(|(card, _)| card.id.as_str() == id).map_or(0, |i| i + 1);
    let next = cards.iter().skip(at).chain(cards.iter().take(at)).find(|(card, _)| card.pending && !card.collapsed && card.id.as_str() != id).map(|(card, _)| card.id.to_string());
    VOICE.with(|v| v.borrow_mut().focus = next.unwrap_or_default());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn answers_are_lettered_in_order() {
        assert_eq!((0..3).map(letter).collect::<Vec<_>>(), ["A", "B", "C"]);
        assert_eq!(letter(26), "27");
    }

    #[test]
    fn a_folded_line_is_one_line_and_short() {
        assert_eq!(one_line("Friday,\n after   lunch"), "Friday, after lunch");
        let long = "word ".repeat(60);
        assert!(one_line(&long).chars().count() <= 120 && one_line(&long).ends_with('…'));
    }

    #[test]
    fn an_attention_item_draws_as_its_artifact() {
        let item = json!({"decision_id": "d1", "artifact_id": "a1", "kind": "question", "response_type": "single_choice", "title": "Name", "status": "pending",
            "options": [{"id": "f", "label": "Frankie"}, {"id": "p", "label": "Pebble"}], "question": "What should I be called?"});
        let card = artifact_item(&attention_artifact(&item));
        assert_eq!((card.id.as_str(), card.kind.as_str(), card.pending), ("a1", "question", true));
        let badges: Vec<String> = card.options.iter().map(|o| format!("{} {}", o.badge, o.label)).collect();
        assert_eq!(badges, ["A Frankie", "B Pebble"]);
        assert!(card.allow_custom, "a question takes an own answer unless the agent said no");
    }

    #[test]
    fn a_settled_card_folds_to_what_was_answered() {
        let card = |decision: Value| artifact_item(&json!({"artifact_id": "a", "type": "question", "title": "Trip", "decision": decision}));
        let options = json!([{"id": "o", "label": "Oslo"}, {"id": "t", "label": "Tromsø"}]);
        let chose = card(json!({"status": "answered", "response_type": "single_choice", "options": options, "answer": {"option_id": "t"}}));
        assert_eq!((chose.collapsed, chose.collapsed_kind.as_str(), chose.collapsed_value.as_str()), (true, "chose", "B · Tromsø"));
        let own = card(json!({"status": "answered", "response_type": "single_choice", "options": options, "answer": {"text": "Bergen"}}));
        assert_eq!((own.collapsed_kind.as_str(), own.collapsed_value.as_str()), ("answered", "Bergen"));
        let approved = card(json!({"status": "accepted", "response_type": "approval", "yes_label": "Ship it"}));
        assert_eq!((approved.collapsed_kind.as_str(), approved.collapsed_value.as_str(), approved.collapsed_ok), ("chose", "Ship it", true));
        let gone = card(json!({"status": "expired", "question": "Deploy now?"}));
        assert_eq!((gone.collapsed_kind.as_str(), gone.collapsed_value.as_str(), gone.collapsed_ok), ("expired", "Deploy now?", false));
        let open = card(json!({"status": "pending", "response_type": "single_choice", "options": options, "allow_custom_text": false}));
        assert!(!open.collapsed && !open.allow_custom, "no own answer where the agent said no");
        let text_request = card(json!({"status": "pending", "response_type": "text_input", "question": "Which branch?"}));
        assert!(text_request.allow_custom && text_request.options.row_count() == 0 && text_request.meta.is_empty());
    }
}
