//! Message search in the switcher (Ctrl+Shift+F, or the rail's magnifier; Ctrl+K
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
//! (loaded and cached chats), best and newest first.
//!
//! The page is the field, its tabs (All, Chats, Messages, each with its
//! count) and the results; it says "No results" when nothing matches and
//! "Still indexing…" while a Host reads older history, nothing more.

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
/// The search page's field (made where it is drawn, in the language in use).
pub fn placeholder() -> String {
    // i18n: search page (Ctrl+Shift+F), placeholder of the search field (a verb: type here to search)
    clarp_core::tr!("search.title" => "Search")
}

/// What the page says when nothing matches.
pub fn no_results() -> String {
    // i18n: search page and find bar, the line shown when no message or chat matches the words
    clarp_core::tr!("search.no_results" => "No results")
}
/// Ctrl+K points to message search from this many characters typed.
pub const HINT_FROM: usize = 3;
/// The row that asks the Hosts for older matches.
pub const LOAD_MORE: &str = "search-load-more";
/// What it says while the Hosts are asked.
pub fn searching() -> String {
    // i18n: search page, the line shown while the Hosts are asked for the words typed
    clarp_core::tr!("search.searching" => "Searching…")
}

/// While a Host still reads older history into its index.
pub fn indexing() -> String {
    // i18n: search page, quiet note under the results while the Host still indexes older messages (some are not found yet)
    clarp_core::tr!("search.indexing" => "Still indexing…")
}
/// The Hosts are asked this long after the last key typed.
pub const DEBOUNCE: Duration = Duration::from_millis(250);
/// The search page's tabs: All, Chats, Messages.
pub const TABS: usize = 3;
/// All shows this many chats; a row then opens the Chats tab.
pub const ALL_CHATS: usize = 3;
/// That row.
pub const SEE_ALL_CHATS: &str = "search-see-all-chats";

/// What a tab of the search page counts, for its label (switcher.slint
/// words it: "All 3", "Messages 20+").
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Counts {
    pub all: usize,
    pub chats: usize,
    pub messages: usize,
    /// The Hosts have more pages of messages.
    pub more: bool,
}

/// The rows of tab `tab` (0 All, 1 Chats, 2 Messages) and what each counts.
pub fn tabbed(tab: usize, chats: Vec<Item>, messages: Vec<Item>) -> (Vec<Item>, Counts) {
    let found = messages.iter().filter(|i| i.kind == crate::switcher::Kind::Message).count();
    let more = messages.iter().any(|i| i.target == LOAD_MORE);
    let labels = Counts { all: chats.len() + found, chats: chats.len(), messages: found, more };
    let items = match tab {
        1 => chats,
        2 => messages,
        _ => {
            let total = chats.len();
            let mut items: Vec<Item> = chats.into_iter().take(ALL_CHATS).collect();
            if total > ALL_CHATS {
                // i18n: search page, All tab: the row under the first chats that opens the Chats tab; {n} is how many chats match
                items.push(command(SEE_ALL_CHATS, clarp_core::tr!("search.see_all_chats" => "See all {n} chat" | "See all {n} chats" % total)));
            }
            items.extend(messages);
            items
        }
    };
    (items, labels)
}

/// A row of the search page that does something rather than open a hit.
fn command(target: &str, label: String) -> Item {
    Item {
        kind: crate::switcher::Kind::Command,
        target: target.to_owned(),
        label,
        detail: String::new(),
        key: String::new(),
        group: "message",
        description: String::new(),
        aliases: &[],
        value: String::new(),
        entry: "",
    }
}

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
            let who = who(&hit.role);
            // i18n: search page, a found message's line: marks a message found only in this computer's saved copy of the chat (adjective)
            let cached = if hit.source == Source::Cache { format!(" · {}", clarp_core::tr!("search.hit_cached" => "cached")) } else { String::new() };
            Item::message(format!("{}{SEPARATOR}{}", hit.session, hit.id), format!("{} · {who} · {when}{cached}", engine.chat_name(&hit.session)), snippet)
        })
        .collect();
    let mut items: Vec<Item> = items;
    // The open chat holds older messages than this computer loaded: one
    // row loads them, and the search runs again over them.
    let selected = engine.selected_session().to_owned();
    let more = engine.conversation(&selected).is_some_and(|c| c.has_more());
    if !query.trim().is_empty() && more {
        let loading = engine.log_pending(&selected);
        let name = engine.chat_name(&selected);
        let label = if loading {
            // i18n: search page, row while the open chat's older messages load (to search them too); {} is the chat's name
            clarp_core::tr!("search.loading_older_of" => "Loading older messages of {}…", name)
        } else {
            // i18n: search page, row (an action) that loads the open chat's older messages to search them too; {} is the chat's name
            clarp_core::tr!("search.load_older_of" => "Load older messages of {}", name)
        };
        items.push(command(LOAD_OLDER, label));
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

/// What the page says under its rows when only this computer's chats
/// are searched: nothing, unless their cache could not be read.
pub fn note(results: &SearchResults) -> String {
    if results.cache_error.is_empty() {
        return String::new();
    }
    // i18n: search page, note under the results when this computer's saved chats could not be read; {} is the system's reason
    clarp_core::tr!("search.cache_unreadable" => "Cached chats could not be read: {}", results.cache_error)
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

/// What message search lists, says, and says when it finds nothing.
pub struct Found {
    pub items: Vec<Item>,
    pub note: String,
    pub empty: String,
}

/// Who wrote a found message, as its line says it.
fn who(role: &str) -> String {
    if role == "user" {
        // i18n: search page, a found message's line: written by the user (pronoun, "you" as in "you wrote it")
        clarp_core::tr!("search.hit_you" => "you")
    } else {
        // i18n: search page, a found message's line: written by the agent (noun)
        clarp_core::tr!("search.hit_agent" => "agent")
    }
}

/// Message search's rows for `query`: the Hosts' hits where they search,
/// else (and until they answer) what this computer holds.
pub fn found(engine: &mut Engine, query: &str) -> Found {
    if !engine.host_search_offered() {
        let (items, results) = items(engine, query, LIMIT);
        return Found { items, note: note(&results), empty: no_results() };
    }
    let results = engine.host_search_results();
    let words = query.trim();
    if words.is_empty() {
        return Found { items: Vec::new(), note: String::new(), empty: String::new() };
    }
    let answered = results.query == words && results.answered;
    // The primary cannot search but a paired Host can: its chats are
    // searched as this computer holds them.
    let primary_local = !engine.host_search_offered_for("");
    let local = |engine: &mut Engine| -> Vec<Item> { items(engine, query, LIMIT).0.into_iter().filter(|i| i.target != LOAD_OLDER).collect() };
    if !answered {
        // Until the Hosts answer the words: what this computer holds.
        return Found { items: local(engine), note: String::new(), empty: searching() };
    }
    let now = chrono::Local::now();
    let mut items: Vec<Item> = results
        .hits
        .iter()
        .map(|hit| {
            let snippet: String = hit.snippet.iter().map(|s| if s.mark { format!("**{}**", escape(&s.text)) } else { escape(&s.text) }).collect();
            let when = clarp_core::time_format::chat_stamp(hit.epoch_ms, &now);
            let who = who(&hit.role);
            let known = engine.chat_name(&hit.session);
            let name = if (known == hit.session || known.is_empty()) && !hit.name.is_empty() { hit.name.clone() } else { known };
            let badge = if results.hosts > 1 { engine.host_badge(&hit.session) } else { String::new() };
            let badge = if badge.is_empty() { badge } else { format!(" · {badge}") };
            Item::message(format!("{}{SEPARATOR}{}", hit.session, hit.message_id), format!("{name} · {who} · {when}{badge}"), snippet)
        })
        .collect();
    if results.more {
        let label = if results.pending {
            // i18n: search page, last row while older matches from every chat load
            clarp_core::tr!("search.loading_more" => "Loading more messages…")
        } else {
            // i18n: search page, last row (an action): loads older matches from every chat
            clarp_core::tr!("search.load_more" => "Load more messages")
        };
        items.push(command(LOAD_MORE, label));
    }
    if primary_local {
        items.extend(local(engine));
    }
    // Only what the reader needs: older history still being indexed, and
    // a Host whose search failed.
    let mut said: Vec<String> = Vec::new();
    if !results.partial.is_empty() {
        said.push(indexing());
    }
    for error in &results.errors {
        // i18n: search page, note under the results: a Host whose search failed; {0} is "<Host>: why"
        said.push(clarp_core::tr!("search.not_searched" => "Not searched: {0}", error));
    }
    Found { items, note: said.join(" "), empty: no_results() }
}

/// A chosen message, until the chat shows it.
#[derive(Debug, Clone)]
struct Jump {
    session: String,
    id: String,
    started: Instant,
    pages: u32,
}

/// How a jump to a message is going, for the find bar.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JumpState {
    /// Nothing under way (or it arrived).
    Idle,
    /// Older pages are being read to reach it.
    Loading { pages: u32 },
    /// It could not be reached.
    Failed,
}

/// Pages of the chat a jump reads at most to reach a message, and their size.
pub const JUMP_PAGES: u32 = 20;
pub const JUMP_PAGE: usize = 500;

thread_local! {
    static JUMP: RefCell<Option<Jump>> = const { RefCell::new(None) };
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    static JUMP_STATE: RefCell<JumpState> = const { RefCell::new(JumpState::Idle) };
}

/// A jump that gave up is no longer said (the find bar opens or closes).
pub fn forget_failed_jump() {
    JUMP_STATE.with(|s| {
        if *s.borrow() == JumpState::Failed {
            *s.borrow_mut() = JumpState::Idle;
        }
    });
}

/// How the latest jump is going.
pub fn jump_state() -> JumpState {
    JUMP_STATE.with(|s| s.borrow().clone())
}

fn set_jump_state(app: &Rc<App>, state: JumpState) {
    if JUMP_STATE.with(|s| *s.borrow() != state) {
        JUMP_STATE.with(|s| *s.borrow_mut() = state);
        crate::find_view::jump_changed(app);
    }
}

/// Opens the chat of a chosen row (`target`: session and message id) and
/// brings the message into view once it shows.
pub fn jump(app: &Rc<App>, target: &str) {
    let Some((session, id)) = split(target) else { return };
    open_message(app, session, id);
}

/// Opens `session` and brings message `id` into view once it shows; the
/// chat alone when no message is named.
pub fn open_message(app: &Rc<App>, session: &str, id: &str) {
    if id.is_empty() {
        app.engine.borrow_mut().select(session);
        pump_now(app);
        return;
    }
    app.engine.borrow_mut().select(session);
    pump_now(app);
    reveal(app, session, id);
}

/// A chosen row's chat and message id.
pub fn split(target: &str) -> Option<(&str, &str)> {
    target.split_once(SEPARATOR)
}

/// Brings message `id` of the open chat `session` into view and marks it,
/// reading older pages until it is loaded.
pub fn reveal(app: &Rc<App>, session: &str, id: &str) {
    JUMP.with(|j| *j.borrow_mut() = Some(Jump { session: session.to_owned(), id: id.to_owned(), started: Instant::now(), pages: 0 }));
    set_jump_state(app, JumpState::Idle);
    if !step(app) {
        JUMP.with(|j| j.borrow_mut().take());
        TIMER.with(|t| t.borrow_mut().take());
        return;
    }
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
    let Some(jump) = JUMP.with(|j| j.borrow().clone()) else { return false };
    if jump.started.elapsed() > Duration::from_secs(60) {
        eprintln!("clarp-slint: message search: {} in {} did not show in time", jump.id, jump.session);
        set_jump_state(app, JumpState::Failed);
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
        set_jump_state(app, JumpState::Idle);
        return false;
    }
    if held {
        // In the chat but not drawn as a row of its own yet.
        return true;
    }
    // Older than what is loaded: the page before, until it is there.
    if more && jump.pages < JUMP_PAGES {
        app.engine.borrow_mut().load_older_page(&jump.session, JUMP_PAGE);
        JUMP.with(|j| {
            if let Some(jump) = j.borrow_mut().as_mut() {
                jump.pages += 1;
            }
        });
        set_jump_state(app, JumpState::Loading { pages: jump.pages + 1 });
        pump_now(app);
        return true;
    }
    eprintln!("clarp-slint: message search: {} is no longer in {}", jump.id, jump.session);
    set_jump_state(app, JumpState::Failed);
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
    fn the_note_says_only_what_went_wrong() {
        let results = SearchResults { loaded_chats: 1, cached_chats: 3, cache_reading: true, ..SearchResults::default() };
        assert_eq!(note(&results), "");
        let failed = SearchResults { cache_error: "denied".into(), ..SearchResults::default() };
        assert_eq!(super::note(&failed), "Cached chats could not be read: denied");
    }

    #[test]
    fn tabs_count_and_all_shows_a_few_chats_first() {
        let chat = |s: &str| Item { kind: crate::switcher::Kind::Agent, ..command(s, s.to_owned()) };
        let hit = |s: &str| Item::message(s.to_owned(), s.to_owned(), String::new());
        let chats: Vec<Item> = ["a", "b", "c", "d"].iter().map(|s| chat(s)).collect();
        let messages = vec![hit("m1"), hit("m2"), command(LOAD_MORE, "Load more messages".into())];
        let (all, labels) = tabbed(0, chats.clone(), messages.clone());
        assert_eq!(labels, Counts { all: 6, chats: 4, messages: 2, more: true });
        let targets: Vec<&str> = all.iter().map(|i| i.target.as_str()).collect();
        assert_eq!(targets, ["a", "b", "c", SEE_ALL_CHATS, "m1", "m2", LOAD_MORE]);
        assert_eq!(tabbed(1, chats.clone(), messages.clone()).0.len(), 4);
        assert_eq!(tabbed(2, chats, messages).0.len(), 3);
        let (none, labels) = tabbed(0, Vec::new(), Vec::new());
        assert!(none.is_empty());
        assert_eq!(labels, Counts { all: 0, chats: 0, messages: 0, more: false });
    }
}
