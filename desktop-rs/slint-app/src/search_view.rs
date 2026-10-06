//! Message search in the switcher (Ctrl+F alone, Ctrl+K among the rest):
//! the text of every chat this computer holds, best and newest first, each
//! row its chat, when, and the words around the match. Choosing one opens
//! the chat, brings the message into view and marks it. The Host has no
//! search, so the switcher says what was searched.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use clarp_core::message_search::Source;
use clarp_engine::Engine;
use clarp_engine::search::SearchResults;

use crate::switcher::Item;
use crate::{App, pump_now};

/// Rows Ctrl+F lists, and the best few Ctrl+K adds to its own.
pub const LIMIT: usize = 50;
pub const MIXED: usize = 5;
/// Ctrl+K searches messages from this many characters typed.
pub const MIXED_FROM: usize = 3;

const SEPARATOR: char = '\u{1f}';

/// Escapes Markdown's punctuation so a snippet shows as typed.
fn escape(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        if "\\`*_{}[]()<>#+-.!|~".contains(c) {
            escaped.push('\\');
        }
        escaped.push(c);
    }
    escaped
}

/// The results of `query` as switcher rows (their detail is the snippet,
/// as Markdown with the matches in bold).
pub fn items(engine: &mut Engine, query: &str, limit: usize) -> (Vec<Item>, SearchResults) {
    let results = engine.search_messages(query, limit);
    let now = chrono::Local::now();
    let items = results
        .hits
        .iter()
        .map(|hit| {
            let snippet: String = hit.snippet.iter().map(|s| if s.mark { format!("**{}**", escape(&s.text)) } else { escape(&s.text) }).collect();
            let when = clarp_core::time_format::chat_stamp(hit.epoch_ms, &now);
            let who = if hit.role == "user" { "you" } else { "agent" };
            let cached = if hit.source == Source::Cache { " · cached" } else { "" };
            Item::message(format!("{}{SEPARATOR}{}", hit.session, hit.id), format!("{} · {who} · {when}{cached}", engine.chat_name(&hit.session)), snippet)
        })
        .collect();
    (items, results)
}

/// What was searched, said plainly: only what this computer holds.
pub fn note(results: &SearchResults) -> String {
    let chats = |n: usize| if n == 1 { "1 chat".to_owned() } else { format!("{n} chats") };
    let mut note = format!(
        "Searched {} loaded and {} cached on this computer. Messages never loaded here are not searched: the Host has no message search yet.",
        chats(results.loaded_chats),
        chats(results.cached_chats)
    );
    if results.cache_reading {
        note.insert_str(0, "Reading cached chats… ");
    }
    if !results.cache_error.is_empty() {
        note.push_str(&format!(" The cache could not be read: {}", results.cache_error));
    }
    note
}

/// A chosen message, until the chat shows it.
#[derive(Debug, Clone)]
struct Jump {
    session: String,
    id: String,
    started: Instant,
    pages: u32,
}

thread_local! {
    static JUMP: RefCell<Option<Jump>> = const { RefCell::new(None) };
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

/// Opens the chat of a chosen row (`target`: session and message id) and
/// brings the message into view once it shows.
pub fn jump(app: &Rc<App>, target: &str) {
    let Some((session, id)) = target.split_once(SEPARATOR) else { return };
    app.engine.borrow_mut().select(session);
    pump_now(app);
    JUMP.with(|j| *j.borrow_mut() = Some(Jump { session: session.to_owned(), id: id.to_owned(), started: Instant::now(), pages: 0 }));
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(80), || {
        let Some(app) = crate::app() else { return };
        if !step(&app) {
            JUMP.with(|j| j.borrow_mut().take());
            TIMER.with(|t| t.borrow_mut().take());
        }
    });
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

/// One look at the jump: false once it is done (or given up).
fn step(app: &Rc<App>) -> bool {
    const PAGES: u32 = 40;
    let Some(jump) = JUMP.with(|j| j.borrow().clone()) else { return false };
    if jump.started.elapsed() > Duration::from_secs(20) {
        eprintln!("clarp-slint: message search: {} in {} did not show in time", jump.id, jump.session);
        return false;
    }
    if app.active_session() != jump.session {
        return true;
    }
    let (loading, held, more) = {
        let engine = app.engine.borrow();
        let conversation = engine.conversation(&jump.session);
        (
            engine.log_pending(&jump.session) || conversation.is_none_or(|c| c.loading()),
            conversation.is_some_and(|c| c.index_of(&jump.id).is_some()),
            conversation.is_some_and(|c| c.has_more()),
        )
    };
    if loading {
        return true;
    }
    if app.reveal_message(&jump.session, &jump.id) {
        return false;
    }
    if held {
        // In the chat but not drawn as a row of its own yet.
        return true;
    }
    // Older than what is loaded: the page before, until it is there.
    if more && jump.pages < PAGES {
        app.engine.borrow_mut().load_older(&jump.session);
        JUMP.with(|j| {
            if let Some(jump) = j.borrow_mut().as_mut() {
                jump.pages += 1;
            }
        });
        pump_now(app);
        return true;
    }
    eprintln!("clarp-slint: message search: {} is no longer in {}", jump.id, jump.session);
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_snippet_shows_markdown_punctuation_as_typed() {
        assert_eq!(escape("run `cargo test` *now* [x](y) #1"), "run \\`cargo test\\` \\*now\\* \\[x\\]\\(y\\) \\#1");
    }

    #[test]
    fn the_note_says_what_was_searched() {
        let results = SearchResults { loaded_chats: 1, cached_chats: 3, cache_reading: true, ..SearchResults::default() };
        let note = note(&results);
        assert!(note.starts_with("Reading cached chats… Searched 1 chat loaded and 3 chats cached on this computer"), "{note}");
        assert!(note.contains("the Host has no message search yet"), "{note}");
        let failed = SearchResults { cache_error: "denied".into(), ..SearchResults::default() };
        assert!(super::note(&failed).ends_with("The cache could not be read: denied"));
    }
}
