//! Message search in the switcher (Ctrl+F, or the rail's magnifier; Ctrl+K
//! lists no messages). Each row is its chat, who said it, when, and the
//! words around the match; choosing one opens the chat, loads the page
//! holding the message if it is not loaded yet, brings it into view and
//! marks it.
//!
//! A Host that searches messages itself (`message_search`) is asked, a
//! moment after the typing stops, for every chat it holds, newest first;
//! with chats from all paired Hosts, every Host that can is asked and the
//! hits merge. "Load more" pages back. Until the Hosts answer, and on a
//! Host without the feature, the search runs over what this computer holds
//! (loaded and cached chats), best and newest first, and the switcher says
//! what was searched.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use clarp_core::message_search::Source;
use clarp_engine::Engine;
use clarp_engine::search::SearchResults;

use crate::switcher::Item;
use crate::{App, pump_now};

/// Rows message search lists.
pub const LIMIT: usize = 50;
/// The row that loads the open chat's older messages, to search them too.
pub const LOAD_OLDER: &str = "search-load-older";
/// What message search says when nothing matches: what it looked in.
pub const EMPTY: &str = "No match in the messages loaded on this computer";
/// Ctrl+K points to message search from this many characters typed.
pub const HINT_FROM: usize = 3;
/// The row that asks the Hosts for older matches.
pub const LOAD_MORE: &str = "search-load-more";
/// What the Hosts' search says when nothing matches.
pub const HOST_EMPTY: &str = "No message in any chat has these words";
/// Before a word is typed: what the search does, not an empty result.
pub const HOST_PROMPT: &str = "Type words to search every chat";
/// What it says while the Hosts are asked.
pub const SEARCHING: &str = "Searching every chat…";
/// The Hosts are asked this long after the last key typed.
pub const DEBOUNCE: Duration = Duration::from_millis(250);

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
    let mut items: Vec<Item> = items;
    // The open chat holds older messages than this computer loaded: one
    // row loads them, and the search runs again over them.
    let selected = engine.selected_session().to_owned();
    let more = engine.conversation(&selected).is_some_and(|c| c.has_more());
    if !query.trim().is_empty() && more {
        let name = engine.chat_name(&selected);
        let loading = engine.log_pending(&selected);
        items.push(Item {
            kind: crate::switcher::Kind::Command,
            target: LOAD_OLDER.to_owned(),
            label: if loading { format!("Loading older messages of {name}…") } else { format!("Load older messages of {name}") },
            detail: "Search them too: this chat has more than is loaded here".to_owned(),
            key: String::new(),
            group: "message",
            description: String::new(),
            aliases: &[],
            value: String::new(),
            entry: "",
        });
    }
    (items, results)
}

/// The older-messages row was chosen: the open chat's page before is fetched
/// and the switcher stays open, searching again as it arrives.
pub fn load_older(app: &Rc<App>) {
    let selected = app.engine.borrow().selected_session().to_owned();
    app.engine.borrow_mut().load_older(&selected);
    pump_now(app);
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

thread_local! {
    /// The words the Hosts were last asked (or are about to be).
    static ASKED: RefCell<String> = const { RefCell::new(String::new()) };
    static ASK_TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

/// The switcher opened: the next words are asked afresh.
pub fn forget_asked() {
    ASKED.with(|a| a.borrow_mut().clear());
    ASK_TIMER.with(|t| t.borrow_mut().take());
}

/// Asks the Hosts for `query` once the typing pauses (`DEBOUNCE`); empty
/// words clear their results at once. Nothing on Hosts without the feature.
pub fn ask(app: &App, query: &str) {
    if !app.engine.borrow().host_search_offered() {
        return;
    }
    let query = query.trim().to_owned();
    if ASKED.with(|a| *a.borrow() == query) {
        return;
    }
    ASKED.with(|a| *a.borrow_mut() = query.clone());
    if query.is_empty() {
        ASK_TIMER.with(|t| t.borrow_mut().take());
        app.engine.borrow_mut().search_host("", "");
        return;
    }
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::SingleShot, DEBOUNCE, move || {
        let Some(app) = crate::app() else { return };
        // Still the words in the field: ask.
        if ASKED.with(|a| *a.borrow() == query) {
            app.engine.borrow_mut().search_host(&query, "");
            pump_now(&app);
        }
    });
    ASK_TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

/// "Load more" was chosen: the next page from every Host that has one;
/// the switcher stays open.
pub fn load_more(app: &Rc<App>) {
    app.engine.borrow_mut().search_host_more();
    pump_now(app);
}

/// Names as a list: "A", "A and B", "A, B and C".
fn listed(names: &[String]) -> String {
    match names {
        [] => String::new(),
        [one] => one.clone(),
        [rest @ .., last] => format!("{} and {last}", rest.join(", ")),
    }
}

/// What message search lists, says, and says when it finds nothing.
pub struct Found {
    pub items: Vec<Item>,
    pub note: String,
    pub empty: &'static str,
}

/// Message search's rows for `query`: the Hosts' hits where they search,
/// else (and until they answer) what this computer holds.
pub fn found(engine: &mut Engine, query: &str) -> Found {
    if !engine.host_search_offered() {
        let (items, results) = items(engine, query, LIMIT);
        return Found { items, note: note(&results), empty: EMPTY };
    }
    let results = engine.host_search_results();
    let words = query.trim();
    if words.is_empty() {
        return Found { items: Vec::new(), note: String::new(), empty: HOST_PROMPT };
    }
    let answered = results.query == words && results.answered;
    // The primary cannot search but a paired Host can: its chats are
    // searched as this computer holds them.
    let primary_local = !engine.host_search_offered_for("");
    let local = |engine: &mut Engine| -> Vec<Item> { items(engine, query, LIMIT).0.into_iter().filter(|i| i.target != LOAD_OLDER).collect() };
    let where_ = if results.hosts > 1 { format!("on {} Hosts", results.hosts) } else { "on the Host".to_owned() };
    if !answered {
        // Until the Hosts answer the words: what this computer holds.
        return Found { items: local(engine), note: format!("Searching every chat {where_}…"), empty: SEARCHING };
    }
    let now = chrono::Local::now();
    let mut items: Vec<Item> = results
        .hits
        .iter()
        .map(|hit| {
            let snippet: String = hit.snippet.iter().map(|s| if s.mark { format!("**{}**", escape(&s.text)) } else { escape(&s.text) }).collect();
            let when = clarp_core::time_format::chat_stamp(hit.epoch_ms, &now);
            let who = if hit.role == "user" { "you" } else { "agent" };
            let known = engine.chat_name(&hit.session);
            let name = if (known == hit.session || known.is_empty()) && !hit.name.is_empty() { hit.name.clone() } else { known };
            let badge = if results.hosts > 1 { engine.host_badge(&hit.session) } else { String::new() };
            let badge = if badge.is_empty() { badge } else { format!(" · {badge}") };
            Item::message(format!("{}{SEPARATOR}{}", hit.session, hit.message_id), format!("{name} · {who} · {when}{badge}"), snippet)
        })
        .collect();
    if results.more {
        let loading = results.pending;
        items.push(Item {
            kind: crate::switcher::Kind::Command,
            target: LOAD_MORE.to_owned(),
            label: if loading { "Loading more messages…".to_owned() } else { "Load more messages".to_owned() },
            detail: "Older matches, from every chat".to_owned(),
            key: String::new(),
            group: "message",
            description: String::new(),
            aliases: &[],
            value: String::new(),
            entry: "",
        });
    }
    if primary_local {
        items.extend(local(engine));
    }
    let mut said: Vec<String> = Vec::new();
    said.push(if results.hosts > 1 { format!("Searched every chat on {} Hosts.", results.hosts) } else { "Searched every chat on the Host.".to_owned() });
    if !results.partial.is_empty() {
        // Once, quietly: some older rows may not be found yet.
        said.push(format!("Older history is still being indexed on {}: some older messages are not found yet.", listed(&results.partial)));
    }
    for error in &results.errors {
        said.push(format!("Not searched: {error}."));
    }
    if !results.without.is_empty() {
        said.push(format!("{} cannot search messages: its chats are not searched.", listed(&results.without)));
    }
    if primary_local {
        said.push("This Host cannot search messages: its chats are searched as loaded on this computer.".to_owned());
    }
    Found { items, note: said.join(" "), empty: HOST_EMPTY }
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
    fn names_read_as_a_list() {
        let names = |n: &[&str]| listed(&n.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>());
        assert_eq!(names(&["Laptop"]), "Laptop");
        assert_eq!(names(&["Laptop", "Office"]), "Laptop and Office");
        assert_eq!(names(&["A", "B", "C"]), "A, B and C");
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
