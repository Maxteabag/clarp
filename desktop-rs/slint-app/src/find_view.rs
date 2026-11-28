//! The find bar: words found in the open chat, at the top right of its
//! pane as an editor's find widget. Ctrl+F in a chat opens it (Ctrl+Shift+F
//! or the rail's magnifier searches every chat); a message chosen in that
//! search opens its chat with the bar filled in.
//!
//! The bar shows the words, "N/M" (M messages of the chat hold them all,
//! N the one shown, 1 the oldest), up for the older match and down for the
//! newer, and closes on × or Escape. Enter goes up (older), Shift+Enter
//! down; at either end the arrow is off (no wrap). Typing asks again a
//! moment after the last key and shows the newest match. Every matching
//! message has the words marked; the one shown also has its ring. Closing
//! takes the marks away and leaves the chat where it is.
//!
//! The Host holding the chat lists its matching messages (`/search` with
//! `session`); a Host without message search has the loaded messages
//! searched here.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use clarp_engine::Change;
use slint::ComponentHandle;

use crate::search_view::JumpState;
use crate::{App, AppWindow, FindBridge, pump_now};

/// The words are asked this long after the last key typed.
pub const DEBOUNCE: Duration = Duration::from_millis(250);
#[derive(Debug, Clone, Default)]
struct Find {
    pane: String,
    session: String,
    /// The words asked (trimmed) and as matched.
    asked: String,
    terms: Vec<String>,
    /// The match shown (a message id).
    current: Option<String>,
    /// Typed words: the newest match is shown once they are answered.
    newest: bool,
    /// The pane showed the chat (it may still be opening).
    seen: bool,
}

thread_local! {
    static FIND: RefCell<Option<Find>> = const { RefCell::new(None) };
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    /// The marks' colour, from the theme when the bar opened.
    static COLOR: RefCell<String> = const { RefCell::new(String::new()) };
    /// Words typed and not asked yet (the debounce runs).
    static PENDING: RefCell<Option<String>> = const { RefCell::new(None) };
    /// Which rows hold the words, by row id: its revision and text length
    /// when it was looked at (for the words in `MATCHED_TERMS`).
    static MATCHED: RefCell<std::collections::HashMap<String, (i64, usize, bool)>> = RefCell::new(std::collections::HashMap::new());
    static MATCHED_TERMS: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
}

/// Whether `row` holds every word of `terms`, remembered per row while
/// the words and the row are unchanged (the chat is presented again on
/// every change while the bar is open).
pub fn row_matches(row: &clarp_core::protocol::Message, terms: &[String]) -> bool {
    if MATCHED_TERMS.with(|t| t.borrow().as_slice() != terms) {
        MATCHED_TERMS.with(|t| *t.borrow_mut() = terms.to_vec());
        MATCHED.with(|m| m.borrow_mut().clear());
    }
    let Some(text) = clarp_core::message_search::searchable(row) else { return false };
    let stamp = (row.revision, text.len());
    if let Some(&(revision, len, found)) = MATCHED.with(|m| m.borrow().get(&row.id).copied()).as_ref()
        && (revision, len) == stamp
    {
        return found;
    }
    let found = clarp_core::chat_find::matches(text, terms);
    MATCHED.with(|m| m.borrow_mut().insert(row.id.clone(), (stamp.0, stamp.1, found)));
    found
}

fn hex(color: slint::Color) -> String {
    format!("#{:02x}{:02x}{:02x}", color.red(), color.green(), color.blue())
}

/// The words the bar marks in pane `pane` on `session`, if it shows there.
pub fn highlight(pane: &str, session: &str) -> Option<crate::view::Highlight> {
    let terms = FIND.with(|f| f.borrow().as_ref().filter(|f| f.pane == pane && f.session == session && !f.terms.is_empty()).map(|f| f.terms.clone()))?;
    let color = COLOR.with(|c| c.borrow().clone());
    let open = if color.is_empty() { "<u>".to_owned() } else { format!("<font color=\"{color}\"><u>") };
    let close = if color.is_empty() { "</u>".to_owned() } else { "</u></font>".to_owned() };
    Some(crate::view::Highlight { terms, open, close })
}

/// The bar shows (and in which pane, on which chat).
pub fn shown() -> Option<(String, String)> {
    FIND.with(|f| f.borrow().as_ref().map(|f| (f.pane.clone(), f.session.clone())))
}

fn set_color(window: &AppWindow) {
    let color = hex(window.global::<crate::Palette>().get_warning());
    COLOR.with(|c| *c.borrow_mut() = color);
}

/// The chat's "⋯" menu: the bar in pane `pane`, which becomes the active one.
pub fn open_in(app: &Rc<App>, window: &AppWindow, pane: &str) {
    if app.engine.borrow().panes().active_pane_id() != pane {
        app.engine.borrow_mut().with_panes(|p| p.focus_pane(pane));
        pump_now(app);
    }
    open(app, window);
}

/// Ctrl+F in a chat: the bar in the active pane, its words selected to
/// type over. False when no chat is open (message search goes instead).
pub fn open(app: &Rc<App>, window: &AppWindow) -> bool {
    let session = app.active_session();
    if session.is_empty() {
        return false;
    }
    let pane = app.active_id().to_string();
    crate::search_view::forget_failed_jump();
    let same = FIND.with(|f| f.borrow().as_ref().is_some_and(|f| f.pane == pane && f.session == session));
    if !same {
        close_quietly(app, None);
        set_color(window);
        FIND.with(|f| *f.borrow_mut() = Some(Find { pane: pane.clone(), session, seen: true, ..Find::default() }));
        let bridge = window.global::<FindBridge>();
        bridge.set_text("".into());
        bridge.set_text_set(bridge.get_text_set() + 1);
    }
    let bridge = window.global::<FindBridge>();
    bridge.set_focus_on_open(true);
    FOCUSED.with(|f| f.set(true));
    bridge.set_pane(pane.into());
    bridge.set_open(true);
    bridge.set_focus_request(bridge.get_focus_request() + 1);
    show(app, window);
    true
}

/// A message chosen in message search: its chat shows the bar with the
/// words that found it, that message the current match. The chat keeps
/// the keyboard (Ctrl+F reaches the bar).
pub fn open_at(app: &Rc<App>, window: &AppWindow, query: &str, session: &str, id: &str) {
    let pane = app.active_id().to_string();
    // The jump has just marked the hit in this pane: that mark stays.
    close_quietly(app, Some(&pane));
    FOCUSED.with(|f| f.set(false));
    set_color(window);
    let asked = query.trim().to_owned();
    let terms = clarp_core::chat_find::terms(&asked);
    let seen = app.active_session() == session;
    FIND.with(|f| {
        *f.borrow_mut() = Some(Find { pane: pane.clone(), session: session.to_owned(), asked: asked.clone(), terms, current: Some(id.to_owned()), newest: false, seen });
    });
    let bridge = window.global::<FindBridge>();
    bridge.set_text(asked.clone().into());
    bridge.set_text_set(bridge.get_text_set() + 1);
    bridge.set_focus_on_open(false);
    bridge.set_pane(pane.into());
    bridge.set_open(true);
    app.engine.borrow_mut().find_in_chat(session, &asked);
    pump_now(app);
    rows_again(app, session);
    show(app, window);
}

/// The chat's rows are presented again: their marks follow the words.
fn rows_again(app: &App, session: &str) {
    app.refresh(&[Change::Conversation(session.to_owned())]);
}

/// The words in the field changed: asked once the typing pauses.
fn edited(text: &str) {
    // The field's words stay on the bridge: a bar drawn again shows them.
    if let Some(window) = crate::window() {
        window.global::<FindBridge>().set_text(text.into());
    }
    let text = text.trim().to_owned();
    PENDING.with(|p| *p.borrow_mut() = Some(text.clone()));
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::SingleShot, DEBOUNCE, move || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        PENDING.with(|p| p.borrow_mut().take());
        ask(&app, &window, &text);
    });
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

/// Words typed but not asked yet are asked now: true when there were some.
fn ask_pending(app: &Rc<App>, window: &AppWindow) -> bool {
    let Some(text) = PENDING.with(|p| p.borrow_mut().take()) else { return false };
    TIMER.with(|t| t.borrow_mut().take());
    ask(app, window, &text);
    true
}

fn ask(app: &Rc<App>, window: &AppWindow, words: &str) {
    let Some(session) = FIND.with(|f| {
        let mut find = f.borrow_mut();
        let find = find.as_mut()?;
        if find.asked == words {
            return None;
        }
        find.asked = words.to_owned();
        find.terms = clarp_core::chat_find::terms(words);
        find.current = None;
        find.newest = !words.is_empty();
        Some(find.session.clone())
    }) else {
        return;
    };
    let pane = FIND.with(|f| f.borrow().as_ref().map(|f| f.pane.clone())).unwrap_or_default();
    app.clear_marked(&pane);
    app.engine.borrow_mut().find_in_chat(&session, words);
    pump_now(app);
    rows_again(app, &session);
    after_answer(app);
    show(app, window);
}

/// What the Host (or the loaded rows) answered for the bar's words.
fn results(app: &App, find: &Find) -> clarp_engine::search::ChatFindResults {
    app.engine.borrow().chat_find_results(&find.session, &find.asked)
}

/// Typed words answered: the newest match is shown.
fn after_answer(app: &Rc<App>) {
    let Some(find) = FIND.with(|f| f.borrow().clone()) else { return };
    if !find.newest {
        return;
    }
    let results = results(app, &find);
    if !results.answered || results.pending || results.query != find.asked {
        return;
    }
    let newest = results.ids.last().cloned();
    FIND.with(|f| {
        if let Some(find) = f.borrow_mut().as_mut() {
            find.newest = false;
            find.current = newest.clone();
        }
    });
    if let Some(id) = newest {
        crate::search_view::reveal(app, &find.session, &id);
    }
}

/// Where the current match is among the results: (its index, the number
/// shown for it, how many match).
fn place(find: &Find, results: &clarp_engine::search::ChatFindResults) -> (Option<usize>, Option<usize>, usize) {
    let index = find.current.as_ref().and_then(|id| results.ids.iter().position(|i| i == id));
    // Only the newest are listed: the first listed is not the oldest.
    let before = results.total.saturating_sub(results.ids.len());
    (index, index.map(|i| before + i + 1), results.total.max(results.ids.len()))
}

/// The bar's count, arrows and note as they are now.
fn show(app: &App, window: &AppWindow) {
    let bridge = window.global::<FindBridge>();
    let Some(find) = FIND.with(|f| f.borrow().clone()) else {
        bridge.set_open(false);
        return;
    };
    let results = results(app, &find);
    let (index, number, total) = place(&find, &results);
    let words = !find.asked.is_empty();
    let answered = words && results.answered && !results.pending && results.query == find.asked;
    // The words are the bar's own (find_bar.slint): codes and numbers here.
    let phase = if !words { "" } else if answered { "answered" } else { "asking" };
    let jump = crate::search_view::jump_state();
    let note = if answered && !results.error.is_empty() {
        "error"
    } else if answered && total == 0 {
        "no-results"
    } else if let JumpState::Loading { .. } = jump {
        "loading"
    } else if let JumpState::Failed = jump {
        "unreachable"
    } else if answered && results.local {
        "loaded-only"
    } else {
        ""
    };
    let (older, newer) = match index {
        Some(i) => (i > 0, i + 1 < results.ids.len()),
        None => (answered && !results.ids.is_empty(), false),
    };
    bridge.set_phase(phase.into());
    bridge.set_number(number.unwrap_or(0) as i32);
    bridge.set_total(total as i32);
    bridge.set_note(note.into());
    bridge.set_error(results.error.into());
    bridge.set_can_older(older);
    bridge.set_can_newer(newer);
}

/// Moves to the older (`step` -1) or newer (+1) match; none past the ends.
fn go(app: &Rc<App>, window: &AppWindow, step: isize) {
    let Some(find) = FIND.with(|f| f.borrow().clone()) else { return };
    let results = results(app, &find);
    if results.ids.is_empty() || results.query != find.asked {
        return;
    }
    let (index, ..) = place(&find, &results);
    let next = match index {
        Some(i) => i.checked_add_signed(step).filter(|n| *n < results.ids.len()),
        // Not among them (yet): up starts from the newest.
        None if step < 0 => Some(results.ids.len() - 1),
        None => None,
    };
    let Some(next) = next else { return };
    let id = results.ids[next].clone();
    FIND.with(|f| {
        if let Some(find) = f.borrow_mut().as_mut() {
            find.current = Some(id.clone());
            find.newest = false;
        }
    });
    crate::search_view::reveal(app, &find.session, &id);
    show(app, window);
}

/// The older match (up, Enter).
pub fn older(app: &Rc<App>, window: &AppWindow) {
    go(app, window, -1);
}

/// The newer match (down, Shift+Enter).
pub fn newer(app: &Rc<App>, window: &AppWindow) {
    go(app, window, 1);
}

/// Takes the bar and its marks away; the chat stays where it is.
/// `keep`: a pane whose mark stays (a hit was just marked there).
fn close_quietly(app: &App, keep: Option<&str>) {
    TIMER.with(|t| t.borrow_mut().take());
    PENDING.with(|p| p.borrow_mut().take());
    FOCUSED.with(|f| f.set(false));
    MATCHED.with(|m| m.borrow_mut().clear());
    let Some(find) = FIND.with(|f| f.borrow_mut().take()) else { return };
    if keep != Some(find.pane.as_str()) {
        app.clear_marked(&find.pane);
    }
    app.engine.borrow_mut().find_in_chat(&find.session, "");
    rows_again(app, &find.session);
}

/// × or Escape: the bar closes and the chat has the keyboard.
pub fn close(app: &Rc<App>, window: &AppWindow) {
    let had = shown().is_some();
    close_quietly(app, None);
    crate::search_view::forget_failed_jump();
    let bridge = window.global::<FindBridge>();
    bridge.set_open(false);
    bridge.set_phase("".into());
    bridge.set_note("".into());
    if had {
        app.focus_transcript();
    }
    crate::commands::show_hints(app, window);
}

/// The window's state changed: the bar follows its chat's answers, and
/// closes when its pane shows another chat.
pub fn refresh(app: &Rc<App>, window: &AppWindow, changes: &[Change]) {
    let Some(find) = FIND.with(|f| f.borrow().clone()) else { return };
    let pane_session = app.pane_session(&find.pane);
    match pane_session {
        None => return close(app, window),
        Some(s) if s == find.session => {
            if !find.seen {
                FIND.with(|f| f.borrow_mut().as_mut().map(|f| f.seen = true));
            }
        }
        Some(_) if find.seen => return close(app, window),
        Some(_) => {}
    }
    if changes.iter().any(|c| matches!(c, Change::Search) || matches!(c, Change::Conversation(s) if *s == find.session)) {
        after_answer(app);
        show(app, window);
    }
}

/// A jump to a match moved on (loading older pages, failed, arrived).
pub fn jump_changed(app: &Rc<App>) {
    if let Some(window) = crate::window() {
        show(app, &window);
    }
}

/// The find field has the keyboard.
pub fn focused(window: &AppWindow) -> bool {
    window.global::<FindBridge>().get_open() && FOCUSED.with(std::cell::Cell::get)
}

thread_local! {
    static FOCUSED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// The bar's own keys while its field has the keyboard; false for a key
/// it leaves to the field or the window.
pub fn key(app: &Rc<App>, window: &AppWindow, chord: &str) -> bool {
    match chord {
        "Escape" => close(app, window),
        // Words still waiting for the typing to pause: asked now, and
        // the newest match shows once they are answered.
        "Return" | "Enter" | "Up" | "Shift+Return" | "Shift+Enter" | "Down" if ask_pending(app, window) => {}
        "Return" | "Enter" | "Up" => older(app, window),
        "Shift+Return" | "Shift+Enter" | "Down" => newer(app, window),
        "Ctrl+F" => {
            let bridge = window.global::<FindBridge>();
            bridge.set_focus_request(bridge.get_focus_request() + 1);
        }
        _ => return false,
    }
    true
}

/// The bar's keys, for the shortcut bar.
pub fn hints() -> Vec<crate::Hint> {
    [
        // i18n: shortcut bar while the find bar has the keyboard: Enter goes to the older match (adjective, "older one")
        ("Enter", clarp_core::tr!("search.hint.older" => "Older")),
        // i18n: shortcut bar while the find bar has the keyboard: Shift+Enter goes to the newer match (adjective, "newer one")
        ("Shift+Enter", clarp_core::tr!("search.hint.newer" => "Newer")),
        // i18n: shortcut bar while the find bar has the keyboard: Esc closes the bar (verb)
        ("Esc", clarp_core::tr!("search.hint.close" => "Close")),
        // i18n: shortcut bar while the find bar has the keyboard: Ctrl+Shift+F searches every chat instead (noun phrase)
        ("Ctrl+Shift+F", clarp_core::tr!("search.hint.all_chats" => "All chats")),
    ]
    .into_iter()
    .map(|(keys, label)| crate::Hint { keys: keys.into(), label: label.into() })
    .collect()
}

/// Connects the bar's controls.
pub fn wire(window: &AppWindow) {
    let bridge = window.global::<FindBridge>();
    bridge.on_edited(|text| edited(&text));
    let with = |run: fn(&Rc<App>, &AppWindow)| {
        move || {
            if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
                run(&app, &window);
            }
        }
    };
    bridge.on_older(with(older));
    bridge.on_newer(with(newer));
    bridge.on_close(with(close));
    bridge.on_focus_changed(|focused| {
        FOCUSED.with(|f| f.set(focused));
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            crate::commands::show_hints(&app, &window);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn results(ids: &[&str], total: usize) -> clarp_engine::search::ChatFindResults {
        clarp_engine::search::ChatFindResults { ids: ids.iter().map(|s| (*s).to_owned()).collect(), total, answered: true, ..Default::default() }
    }

    #[test]
    fn the_count_numbers_matches_oldest_first_and_counts_those_not_listed() {
        let find = Find { current: Some("b".into()), ..Find::default() };
        assert_eq!(place(&find, &results(&["a", "b", "c"], 3)), (Some(1), Some(2), 3));
        // Only the newest two of five listed: b is the fourth.
        assert_eq!(place(&find, &results(&["b", "c"], 5)), (Some(0), Some(4), 5));
        let elsewhere = Find { current: Some("x".into()), ..Find::default() };
        assert_eq!(place(&elsewhere, &results(&["a"], 1)), (None, None, 1));
    }
}
