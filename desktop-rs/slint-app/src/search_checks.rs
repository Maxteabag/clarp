//! `--check message-search`: Ctrl+F finds a message's words in another
//! chat (one loaded in this run), says it searched only this computer's
//! chats, and Enter opens that chat with the message marked and in view
//! (it is near the top, so the chat scrolls up to it). Ctrl+K lists
//! messages too: one in a chat only the transcript cache holds opens the
//! same way. Ctrl+F inside Ctrl+K's switcher turns it into message search
//! with the words already typed, and what is typed next arrives once. With
//! no match it says it searched the messages loaded on this computer.
//! Ctrl+F while the chat list has the keyboard takes what is typed right
//! after it into its field, and a '/' search leaves the window's scale and
//! size as they were.
//!
//! `--check host-search`: on a Host that searches messages itself
//! (`message_search`), the rail's magnifier opens message search; the words
//! typed are asked once, after the typing pauses; hits from chats never
//! loaded here show newest first with their matches marked, with a quiet
//! note while the Host still indexes older history; "Load more" brings the
//! next page; Enter opens a chat never loaded at the hit, loading the page
//! that holds it.
//!
//! `--check find-in-chat`: Ctrl+F in an open chat opens the find bar at
//! the top right of the chat; the words typed show the newest match, marked
//! and in view, with "4/4"; Enter and Up go to older matches and Shift+Enter
//! to newer ones, reading older pages (500 rows each) to reach one not
//! loaded, with no wrap at the ends; the matching rows have their words
//! marked; words found nowhere say 0/0; Escape closes it, takes the marks
//! away and leaves the chat where it is; the chat's ⋯ menu opens it too
//! (Search in chat); a hit chosen in message search
//! (Ctrl+Shift+F) opens its chat with the bar filled in at that match.
//!
//! `--check mention`: `@` in the composer lists the agents, the arrows move
//! through them, Tab completes the name, the route chip says where Enter
//! sends, the message goes to that agent's chat (`/send` to its session),
//! which opens with the sent row saying so; Escape closes the list and
//! leaves the composer typing.

use std::time::Duration;

use serde_json::{Value, json};
use slint::Model;

use super::{Stage, app_now, check, check_window_size_free, control, rows, run_stages, sends, shot, view};
use crate::headless;

/// Where the drawn element `id` is: (top, bottom).
fn drawn(id: &str) -> Option<(f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let (window, id) = (crate::window()?, id.to_owned());
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|i| i == id.as_str()) && e.size().height > 0.0)
        .find_first()
        .map(|e| (e.absolute_position().y, e.absolute_position().y + e.size().height))
}

/// Row `id` is drawn wholly inside the active chat.
fn in_view(id: &str) -> bool {
    let chat = drawn(&format!("chat:{}", app_now().active_id()));
    match (drawn(&format!("row:{id}")), chat) {
        (Some((top, bottom)), Some((chat_top, chat_bottom))) => top >= chat_top - 1.0 && bottom <= chat_bottom + 1.0,
        _ => false,
    }
}

fn first(window: &crate::AppWindow) -> (String, String) {
    window.get_switcher_rows().row_data(0).map(|r| (r.kind.to_string(), r.label.to_string())).unwrap_or_default()
}

fn cache_root() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var_os("XDG_CACHE_HOME").expect("the check runs with a scratch cache")).join("clarp").join("transcripts")
}

pub fn message_search_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3, out4, out5, out6) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            // Mike's chat: the words to find near its top, under a screenful.
            let mut turns = Vec::new();
            for i in 0..30 {
                let role = if i % 2 == 0 { "user" } else { "assistant" };
                let text = if i == 2 { "The quarterly pelican report is attached, with the totals.".to_owned() } else { format!("Row {i}: a routine note about the build and its tests.") };
                turns.push(json!({"id": if i == 2 { "m-target".to_owned() } else { format!("m-{i}") }, "role": role, "text": text}));
            }
            check(control("/__control/turns", &json!({"session": "mike", "turns": turns})).is_ok(), "the Host holds Mike's long chat");
            // Dana: an agent this run never opened, whose chat the transcript
            // cache holds from an earlier one.
            let walrus = json!({"id": "d-walrus", "role": "assistant", "text": "Here is the walrus migration plan, step by step.", "timestamp": "2026-09-20T10:00:00Z"});
            check(control("/__control/add-agent", &json!({"session": "dana", "persona": "Dana"})).is_ok(), "the Host adds Dana");
            check(control("/__control/turns", &json!({"session": "dana", "turns": [walrus]})).is_ok(), "the Host holds Dana's chat");
            let snapshot = json!({"conversation_id": "c-dana", "latest_revision": 1, "has_more": false,
                "turns": [{"revision": 1, "id": "d-walrus", "role": "assistant", "text": "Here is the walrus migration plan, step by step.", "timestamp": "2026-09-20T10:00:00Z"}]});
            let saved = clarp_core::transcript_cache::TranscriptCache::new(cache_root()).save(app.engine.borrow().base_url(), "dana", snapshot.as_object().expect("an object"));
            check(saved.is_ok(), &format!("Dana's chat is in the transcript cache {saved:?}"));
            true
        })),
        ("mike loaded", Box::new(|app, window, _| {
            if app.engine.borrow().roster().find("dana").is_none() {
                return false;
            }
            check_window_size_free(window, "before another chat opens");
            window.invoke_chat_chosen("mike".into());
            true
        })),
        ("back to rachel", Box::new(|app, window, _| {
            let loaded = app.engine.borrow().conversation("mike").is_some_and(|c| c.index_of("m-target").is_some() && !c.loading());
            if !loaded {
                return false;
            }
            check_window_size_free(window, "with Mike's long chat open");
            window.invoke_chat_chosen("rachel".into());
            true
        })),
        ("ctrl f", Box::new(|app, _, elapsed| {
            if app.engine.borrow().selected_session() != "rachel" || !super::report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        ("search open", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_switcher_placeholder() == crate::search_view::placeholder(), "Ctrl+F opens message search");
            let note = window.get_switcher_note().to_string();
            check(note.is_empty() && window.get_switcher_hint().is_empty(), &format!("the page explains nothing: {note:?} {:?}", window.get_switcher_hint()));
            headless::type_text("pelican report");
            true
        })),
        ("found", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "pelican report" || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(3), &format!("the words typed right after Ctrl+F arrive once: {:?}", window.get_switcher_query()));
                return elapsed >= Duration::from_secs(3);
            }
            let (kind, label) = first(window);
            check(kind == "message" && label.starts_with("Mike · you"), &format!("the message in Mike's chat is found: {kind} {label}"));
            let snippet = window.get_switcher_rows().row_data(0).map(|r| r.detail.to_string()).unwrap_or_default();
            check(snippet.contains("**pelican** **report**"), &format!("the snippet marks the match: {snippet}"));
            shot(&out1, "message-search-01-results");
            headless::press(Key::Return);
            true
        })),
        ("jumped", Box::new(move |app, window, _| {
            if window.get_switcher_open() || app.engine.borrow().selected_session() != "mike" || app_now().marked_row() != "m-target" || !in_view("m-target") {
                return false;
            }
            check(true, "Enter opens Mike's chat with the message marked and in view");
            check(!super::report().at_end, "the chat stayed on the message, not the latest");
            shot(&out2, "message-search-02-jumped");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("ctrl k", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("walrus");
            true
        })),
        ("no messages in ctrl k", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "walrus" || elapsed < Duration::from_millis(600) {
                return false;
            }
            let kinds: Vec<String> = window.get_switcher_rows().iter().map(|r| r.kind.to_string()).collect();
            check(!kinds.iter().any(|k| k == "message"), &format!("Ctrl+K lists no messages: {kinds:?}"));
            check(window.get_switcher_note().contains("Ctrl+Shift+F"), "and points to message search");
            shot(&out3, "message-search-03-ctrl-k");
            // Ctrl+F there, as its note says: message search, same words.
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        ("ctrl f in ctrl k", Box::new(|_, window, elapsed| {
            if window.get_switcher_placeholder() != crate::search_view::placeholder() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_switcher_open(), "Ctrl+F in the switcher keeps it open, as message search");
            check(window.get_switcher_query() == "walrus", &format!("the words typed carry over, once: {:?}", window.get_switcher_query()));
            headless::type_text(" plan");
            true
        })),
        ("typed on", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_switcher_query() == "walrus plan", &format!("what is typed next arrives once, after them: {:?}", window.get_switcher_query()));
            headless::press(Key::Escape);
            true
        })),
        ("ctrl f again", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        ("walrus", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("walrus");
            true
        })),
        ("cached found", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "walrus" || elapsed < Duration::from_millis(300) || first(window).0 != "message" {
                return false;
            }
            let (_, label) = first(window);
            check(label.starts_with("Dana · agent") && label.ends_with("· cached"), &format!("message search finds a message only the cache holds, and says so: {label}"));
            headless::press(Key::Return);
            true
        })),
        ("cached jump", Box::new(|app, window, _| {
            if window.get_switcher_open() || app.engine.borrow().selected_session() != "dana" || app_now().marked_row() != "d-walrus" || !in_view("d-walrus") {
                return false;
            }
            check(true, "the cached chat opens with its message marked");
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        // Nothing matches: the switcher says what it looked in.
        ("no match", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("zyzzyva");
            true
        })),
        ("honest empty", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "zyzzyva" || elapsed < Duration::from_millis(400) {
                return false;
            }
            let messages = window.get_switcher_rows().iter().filter(|r| r.kind == "message").count();
            check(messages == 0, "nothing matches");
            check(window.get_switcher_empty() == crate::search_view::no_results(), &format!("it says what it searched: {:?}", window.get_switcher_empty()));
            shot(&out4, "message-search-04-no-match");
            headless::press(Key::Escape);
            true
        })),
        // Ctrl+F from Updates, after the switcher was opened and closed
        // there (#846): it opens message search, and what is typed next
        // goes into its field.
        ("updates", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press_with(&[Key::Control], "2");
            true
        })),
        ("updates switcher", Box::new(|_, window, elapsed| {
            if window.get_surface() != "updates" || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("updates switcher closed", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("ctrl f in updates", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        ("search over updates", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_switcher_open() && window.get_switcher_placeholder() == crate::search_view::placeholder(), "Ctrl+F opens message search over Updates, after the switcher closed there");
            if !window.get_switcher_open() {
                return true;
            }
            headless::type_text("pelican");
            true
        })),
        ("typed over updates", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "pelican" && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_switcher_query() == "pelican", &format!("what is typed after Ctrl+F goes into its field: {:?}", window.get_switcher_query()));
            headless::press(Key::Escape);
            true
        })),
        ("back to the chats", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_surface() == "updates", "Escape leaves Updates showing");
            headless::press_with(&[Key::Control], "1");
            true
        })),
        ("ctrl f in the chats", Box::new(|_, window, elapsed| {
            if window.get_surface() != "chats" || elapsed < Duration::from_millis(500) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        ("search over the chats", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_switcher_open(), "back in the chats Ctrl+F opens message search");
            if !window.get_switcher_open() {
                return true;
            }
            headless::type_text("walrus");
            true
        })),
        ("typed over the chats", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "walrus" && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_switcher_query() == "walrus", &format!("and what is typed goes into its field: {:?}", window.get_switcher_query()));
            shot(&out5, "message-search-05-ctrl-f-after-updates");
            headless::press(Key::Escape);
            true
        })),
        // Ctrl+F while the chat list has the keyboard, the words typed in
        // one go right after it: they land in message search's field (#852).
        ("explorer", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press_with(&[Key::Control], "e");
            true
        })),
        ("ctrl f from the explorer", Box::new(|_, window, elapsed| {
            if !window.get_sidebar_focused() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_sidebar_focused(), "Ctrl+E gives the chat list the keyboard");
            headless::press_with(&[Key::Control, Key::Shift], "f");
            headless::type_text("walrus");
            true
        })),
        ("typed from the explorer", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "walrus" && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_switcher_open(), "Ctrl+F from the chat list opens message search");
            check(window.get_switcher_query() == "walrus", &format!("and what is typed right after goes into its field: {:?}", window.get_switcher_query()));
            shot(&out6, "message-search-06-ctrl-f-from-the-explorer");
            headless::press(Key::Escape);
            true
        })),
        // '/' searches the chat; Enter leaves the window's scale and size
        // as they were (#852).
        ("chat has the keyboard", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(300) {
                return false;
            }
            app_now().focus_transcript();
            true
        })),
        ("slash search", Box::new(|_, _, elapsed| {
            if !super::report().transcript_focused && elapsed < Duration::from_secs(2) {
                return false;
            }
            headless::press("/");
            headless::type_text("walrus");
            headless::press(Key::Return);
            true
        })),
        ("searched", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check_window_size_free(window, "after a '/' search and Enter");
            true
        })),
    ];
    run_stages(stages);
}

/// The fake Host's `GET /search` requests: the words of each.
fn searches() -> Vec<String> {
    super::requests("GET", "/search").iter().map(|r| r["query"]["q"].as_str().unwrap_or_default().to_owned()).collect()
}

/// The rail's magnifier, where it is drawn: its middle.
fn magnifier() -> Option<(f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_label().is_some_and(|l| l == "Search messages (Ctrl+Shift+F)") && e.size().width > 0.0)
        .find_first()
        .map(|e| (e.absolute_position().x + e.size().width / 2.0, e.absolute_position().y + e.size().height / 2.0))
}

pub fn host_search_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3) = (out.clone(), out.clone(), out);
    let row = |id: &str, role: &str, text: &str, at: &str| json!({"id": id, "role": role, "text": text, "timestamp": at});
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            // Oscar: never opened here, a long chat whose match is far older
            // than its last page.
            let mut turns = Vec::new();
            for i in 0..160 {
                let role = if i % 2 == 0 { "user" } else { "assistant" };
                let at = format!("2026-11-20T{:02}:{:02}:00Z", 8 + i / 60, i % 60);
                turns.push(if i == 2 { row("o-target", role, "The heron survey covers the northern lakes.", &at) } else { row(&format!("o-{i}"), role, &format!("Row {i}: a routine note about the build."), &at) });
            }
            check(control("/__control/add-agent", &json!({"session": "oscar", "persona": "Oscar"})).is_ok(), "the Host adds Oscar");
            check(control("/__control/turns", &json!({"session": "oscar", "turns": turns})).is_ok(), "the Host holds Oscar's long chat");
            check(control("/__control/add-agent", &json!({"session": "pia", "persona": "Pia"})).is_ok(), "the Host adds Pia");
            check(control("/__control/turns", &json!({"session": "pia", "turns": [row("p-heron", "assistant", "A heron nests by the pier.", "2026-11-15T12:00:00Z")]})).is_ok(), "the Host holds Pia's chat");
            let mike = json!([row("m1", "assistant", "Mike here", "2026-09-29T09:00:00Z"), row("m-heron", "user", "Did you see the herons?", "2026-11-01T12:00:00Z")]);
            check(control("/__control/turns", &json!({"session": "mike", "turns": mike})).is_ok(), "Mike's chat holds an older match");
            // The Host searches, still indexing older history, two hits a page.
            check(control("/__control/message-search", &json!({"on": true, "partial": true, "page": 2})).is_ok(), "the Host offers message_search");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("magnifier", Box::new(|app, window, elapsed| {
            let ready = app.engine.borrow().host_search_offered() && app.engine.borrow().roster().find("pia").is_some();
            if !ready || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(app.engine.borrow().conversation("oscar").is_none_or(|c| c.rows().is_empty()), "Oscar's chat was never loaded here");
            let Some((x, y)) = magnifier() else {
                check(false, "the rail draws the magnifier");
                return true;
            };
            super::click_at(window, x, y);
            true
        })),
        ("opened", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(window.get_switcher_open() && window.get_switcher_placeholder() == crate::search_view::placeholder(), "the rail's magnifier opens message search");
            headless::type_text("heron");
            true
        })),
        ("found", Box::new(move |_, window, elapsed| {
            let messages: Vec<String> = window.get_switcher_rows().iter().filter(|r| r.kind == "message").map(|r| r.label.to_string()).collect();
            let done = messages.first().is_some_and(|l| l.starts_with("Oscar"));
            if !done {
                check(elapsed < Duration::from_secs(5), &format!("the Host's hits show: {messages:?} {:?}", window.get_switcher_note()));
                return elapsed >= Duration::from_secs(5);
            }
            check(searches() == ["heron"], &format!("the words are asked once, after the typing paused: {:?}", searches()));
            check(messages.len() == 2 && messages[0].starts_with("Oscar · you") && messages[1].starts_with("Pia · agent"), &format!("hits from chats never loaded, newest first: {messages:?}"));
            let snippet = window.get_switcher_rows().row_data(0).map(|r| r.detail.to_string()).unwrap_or_default();
            check(snippet.contains("**heron**"), &format!("the snippet marks the match: {snippet}"));
            let note = window.get_switcher_note().to_string();
            check(note == crate::search_view::indexing(), &format!("it says, quietly, that older history is still being indexed: {note}"));
            let tabs: Vec<String> = window.get_switcher_tabs().iter().map(|t| t.to_string()).collect();
            check(tabs == ["2", "0", "2"] && window.get_switcher_tabs_more(), &format!("the tabs count what they hold, more to load: {tabs:?}"));
            let last = window.get_switcher_rows().iter().last().map(|r| r.label.to_string()).unwrap_or_default();
            check(last == "Load more messages", &format!("a row loads more: {last}"));
            shot(&out1, "host-search-01-results");
            let index = window.get_switcher_rows().row_count() as i32 - 1;
            window.invoke_switcher_chosen(index);
            true
        })),
        ("more", Box::new(move |_, window, elapsed| {
            let messages: Vec<String> = window.get_switcher_rows().iter().filter(|r| r.kind == "message").map(|r| r.label.to_string()).collect();
            if messages.len() < 3 {
                check(elapsed < Duration::from_secs(5), &format!("Load more brings the next page: {messages:?}"));
                return elapsed >= Duration::from_secs(5);
            }
            check(window.get_switcher_open(), "the switcher stays open on Load more");
            check(messages.len() == 3 && messages[2].starts_with("Mike · you"), &format!("the older match comes last: {messages:?}"));
            let pages: Vec<Value> = super::requests("GET", "/search");
            check(pages.last().is_some_and(|r| r["query"]["before"] == "2"), &format!("the next page is asked with the cursor: {pages:?}"));
            let more = window.get_switcher_rows().iter().any(|r| r.label.starts_with("Load more"));
            check(!more, "no more rows to load after the last page");
            shot(&out2, "host-search-02-more");
            window.invoke_switcher_chosen(0);
            true
        })),
        ("jumped", Box::new(move |app, window, elapsed| {
            let shown = !window.get_switcher_open() && app.engine.borrow().selected_session() == "oscar" && app_now().marked_row() == "o-target" && in_view("o-target");
            if !shown {
                check(elapsed < Duration::from_secs(15), "Enter opens Oscar's chat at the hit");
                return elapsed >= Duration::from_secs(15);
            }
            check(true, "Enter opens a chat never loaded at the hit, its older page loaded, the message marked and in view");
            shot(&out3, "host-search-03-jumped");
            true
        })),
    ];
    run_stages(stages);
}

/// The find bar: open, its count ("4/4", "0/0", "…", ""), its note's
/// code, and whether up and down go on.
fn find_bar(window: &crate::AppWindow) -> (bool, String, String, bool, bool) {
    use slint::ComponentHandle;
    let bridge = window.global::<crate::FindBridge>();
    let count = match bridge.get_phase().as_str() {
        "" => String::new(),
        "answered" => format!("{}/{}", bridge.get_number(), bridge.get_total()),
        _ => "…".to_owned(),
    };
    (bridge.get_open(), count, bridge.get_note().to_string(), bridge.get_can_older(), bridge.get_can_newer())
}

/// Row `id` of the active chat has its words marked (its text differs from
/// the same text built without the find bar's words).
fn marked_words(id: &str) -> Option<bool> {
    let rows = app_now().active_messages()?;
    let row = rows.iter().find(|r| r.id == id)?;
    let block = row.blocks.row_data(0)?;
    let text = app_now().engine.borrow().conversation("oscar")?.rows().iter().find(|m| m.id == id)?.text.clone();
    if block.kind == "code" { return Some(block.code_marks.row_count() > 0); }
    let plain = crate::view::message_block(&clarp_engine::blocks::blocks(&text)[0], row.author == "user").styled;
    Some(block.styled != plain)
}

const CODE: &str = concat!(
    "heron! {\n",
    "    heron(\"<Héron> & **lake**\");\n",
    "\n",
    "    // heron: Walk the northern lakes in survey order, counting the birds on each shore before crossing to the next inlet. ",
    "Record the observation beside its station, preserve the route in the field notes, and finish the pass at the station\n",
    "}",
);

fn fenced_message() -> String {
    format!("```rust\n{CODE}\n```")
}

/// Check the actual code rectangle in the frame, excluding the find bar and the row ring.
fn fenced_frame(out: &str, name: &str, window: &crate::AppWindow) {
    use i_slint_backend_testing::ElementQuery;
    use slint::ComponentHandle;

    shot(out, name);
    let row = ElementQuery::from_root(window).match_predicate(|e| e.accessible_id().as_deref() == Some("row:o-300")).find_first();
    let rect = row.and_then(|e| e.query_descendants().match_id("Block::code-text").find_first());
    let Some(rect) = rect else {
        check(false, "the active fenced code is drawn");
        return;
    };
    let pos = rect.absolute_position();
    let size = rect.size();
    let scale = window.window().scale_factor();
    let mark = window.global::<crate::Palette>().get_warning();
    let Ok(frame) = image::open(format!("{out}/{name}.png")) else {
        check(false, "the fenced-code screenshot can be read");
        return;
    };
    let frame = frame.to_rgb8();
    let x = (pos.x * scale).round() as u32;
    let y = (pos.y * scale).round() as u32;
    let width = ((size.width * scale).round() as u32).min(frame.width().saturating_sub(x));
    let height = ((size.height * scale).round() as u32).min(frame.height().saturating_sub(y));
    if width == 0 || height == 0 {
        check(false, "the active code is inside the frame");
        return;
    }
    let crop = image::imageops::crop_imm(&frame, x, y, width, height).to_image();
    // The underline spans the word in the mark colour. The first two
    // physical lines start with identical words, four monospace columns apart.
    let ink = [mark.red(), mark.green(), mark.blue()];
    let mut underlines = Vec::new();
    let mut previous_y = None;
    for (y, row) in crop.rows().enumerate() {
        let mut run = 0;
        for (x, pixel) in row.enumerate() {
            run = if pixel.0 == ink { run + 1 } else { 0 };
            if run == 12 {
                if previous_y != Some(y.saturating_sub(1)) {
                    underlines.push((x + 1 - run, y));
                }
                previous_y = Some(y);
                break;
            }
        }
    }
    if let [(first_x, first_y), (indented_x, second_y), (_, last_y)] = underlines.as_slice() {
        // The second line's first underline is displaced by four character
        // cells. Measure one five-letter word from the first underline.
        let word_width = (*first_x..crop.width() as usize).take_while(|x| crop.get_pixel(*x as u32, *first_y as u32).0 == ink).count();
        let indent = indented_x.saturating_sub(*first_x);
        check((5 * indent).abs_diff(4 * word_width) <= 10, &format!("the rendered indent is four monospace columns: indent={indent}px, five-letter word={word_width}px, marks={underlines:?}"));
        check((last_y - first_y).abs_diff(3 * (second_y - first_y)) <= 3, "the blank line and unwrapped source lines keep their spacing");
    } else {
        check(false, &format!("expected find underlines on three separate code lines, found {underlines:?}"));
    }
    let zoom = image::imageops::resize(&crop, width * 2, height * 2, image::imageops::FilterType::Nearest);
    check(zoom.save(format!("{out}/{name}-code.png")).is_ok(), "saved the enlarged code and its active highlight");
    let block = app_now().active_messages().and_then(|rows| rows.iter().find(|r| r.id == "o-300")?.blocks.row_data(0));
    check(block.is_some_and(|b| b.text.as_str() == CODE), "the displayed code keeps its literal text and spacing");
}

pub fn find_in_chat_check(out: String) {
    use slint::platform::Key;
    let stepped = out.clone();
    let (out1, out2, out3, out3b, out4, out5) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out);
    // Oscar's long chat: four rows hold "heron", the oldest far above what
    // opening the chat loads.
    const MATCHES: [usize; 4] = [5, 300, 640, 690];
    let id = |i: usize| format!("o-{i}");
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            let turns: Vec<Value> = (0..700)
                .map(|i| {
                    let role = if i != 300 && i % 2 == 0 { "user" } else { "assistant" };
                    let at = format!("2020-03-20T{:02}:{:02}:{:02}Z", 8 + i / 3600, (i / 60) % 60, i % 60);
                    let text = if i == 300 { fenced_message() } else if MATCHES.contains(&i) { format!("Row {i}: the heron survey of the northern lakes.") } else { format!("Row {i}: a routine note about the build.") };
                    json!({"id": id(i), "role": role, "text": text, "timestamp": at})
                })
                .collect();
            check(control("/__control/add-agent", &json!({"session": "oscar", "persona": "Oscar"})).is_ok(), "the Host adds Oscar");
            check(control("/__control/turns", &json!({"session": "oscar", "turns": turns})).is_ok(), "the Host holds Oscar's long chat");
            check(control("/__control/message-search", &json!({"on": true, "positions": true})).is_ok(), "the Host offers message_search and search_positions");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("open oscar", Box::new(|app, window, elapsed| {
            let ready = app.engine.borrow().host_search_offered() && app.engine.borrow().roster().find("oscar").is_some();
            if !ready || elapsed < Duration::from_millis(300) {
                return false;
            }
            window.invoke_chat_chosen("oscar".into());
            true
        })),
        ("ctrl f", Box::new(move |app, _, elapsed| {
            let loaded = app.engine.borrow().conversation("oscar").is_some_and(|c| !c.loading() && c.index_of("o-699").is_some());
            if !loaded || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(app.engine.borrow().conversation("oscar").is_some_and(|c| c.index_of("o-5").is_none()), "the oldest match is not loaded yet");
            headless::press_with(&[Key::Control], "f");
            true
        })),
        ("bar open", Box::new(|_, window, elapsed| {
            let (open, ..) = find_bar(window);
            if !open || elapsed < Duration::from_millis(200) {
                check(elapsed < Duration::from_secs(3), "Ctrl+F in a chat opens the find bar");
                return elapsed >= Duration::from_secs(3);
            }
            check(!window.get_switcher_open(), "not message search");
            let (bar, chat) = (drawn("find-bar"), drawn(&format!("chat:{}", app_now().active_id())));
            check(bar.zip(chat).is_some_and(|(b, c)| b.0 >= c.0 && b.1 < c.0 + (c.1 - c.0) / 3.0), &format!("the bar sits at the top of the chat: {bar:?} {chat:?}"));
            headless::type_text("heron");
            true
        })),
        ("newest", Box::new(move |_, window, elapsed| {
            let (_, count, ..) = find_bar(window);
            let done = count == "4/4" && app_now().marked_row() == "o-690" && in_view("o-690");
            if !done {
                check(elapsed < Duration::from_secs(5), &format!("the newest match shows, 4/4: {count:?} {:?}", app_now().marked_row()));
                return elapsed >= Duration::from_secs(5);
            }
            let (_, _, _, older, newer) = find_bar(window);
            check(older && !newer, "up goes on, down is off at the newest");
            check(marked_words("o-690") == Some(true) && marked_words("o-640") == Some(true), "the matching rows have their words marked");
            check(marked_words("o-689") == Some(false), "a row without them is as it was");
            check(searches().iter().filter(|q| *q == "heron").count() == 1, &format!("asked once, after the typing paused: {:?}", searches()));
            let asked = super::requests("GET", "/search");
            check(asked.last().is_some_and(|r| r["query"]["positions"] == "1" && r["query"]["session"] == "oscar"), &format!("the chat's positions are asked: {asked:?}"));
            shot(&out1, "find-in-chat-01-newest");
            headless::press(Key::Return);
            true
        })),
        ("older", Box::new(|_, window, elapsed| {
            let (_, count, ..) = find_bar(window);
            if !(count == "3/4" && app_now().marked_row() == "o-640" && in_view("o-640")) {
                check(elapsed < Duration::from_secs(5), &format!("Enter goes to the older match, 3/4: {count:?}"));
                return elapsed >= Duration::from_secs(5);
            }
            headless::press(Key::UpArrow);
            headless::press(Key::UpArrow);
            true
        })),
        ("oldest", Box::new(move |_, window, elapsed| {
            let (_, count, note, older, newer) = find_bar(window);
            if !(count == "1/4" && app_now().marked_row() == "o-5" && in_view("o-5")) {
                check(elapsed < Duration::from_secs(20), &format!("up reaches the oldest match, not loaded before: {count:?} {note:?}"));
                return elapsed >= Duration::from_secs(20);
            }
            check(!older && newer, "at the oldest, up is off");
            let pages = super::requests("GET", "/log");
            check(pages.iter().any(|r| r["query"]["limit"] == "500" && r["query"]["before"].is_string()), "older pages of 500 rows were read");
            check(note.is_empty(), &format!("no note once there: {note:?}"));
            shot(&out2, "find-in-chat-02-oldest");
            headless::press(Key::Return);
            true
        })),
        ("no wrap", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            let (_, count, ..) = find_bar(window);
            check(count == "1/4" && app_now().marked_row() == "o-5", &format!("no wrap past the oldest: {count:?}"));
            headless::press_with(&[Key::Shift], Key::Return);
            true
        })),
        ("newer", Box::new(move |_, window, elapsed| {
            let (_, count, ..) = find_bar(window);
            if !(count == "2/4" && app_now().marked_row() == "o-300" && in_view("o-300") && elapsed >= Duration::from_millis(300)) {
                check(elapsed < Duration::from_secs(5), &format!("Shift+Enter goes to the newer match, 2/4: {count:?}"));
                return elapsed >= Duration::from_secs(5);
            }
            fenced_frame(&stepped, "find-in-chat-02b-fenced-step", window);
            headless::type_text(" zebra");
            true
        })),
        ("none", Box::new(move |_, window, elapsed| {
            let (_, count, note, older, newer) = find_bar(window);
            if count != "0/0" {
                check(elapsed < Duration::from_secs(5), &format!("words found nowhere: 0/0 {count:?}"));
                return elapsed >= Duration::from_secs(5);
            }
            check(note == "no-results" && !older && !newer, &format!("it says so, both arrows off: {note:?}"));
            shot(&out3, "find-in-chat-03-none");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, elapsed| {
            let (open, ..) = find_bar(window);
            if open || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(3), "Escape closes the bar");
                return elapsed >= Duration::from_secs(3);
            }
            check(app_now().marked_row().is_empty(), "no row is marked any more");
            check(marked_words("o-300") == Some(false), "and no words");
            check(!super::report().at_end, "the chat stays where it was");
            use slint::ComponentHandle;
            window.global::<crate::ChatMenu>().invoke_opened(app_now().active_id());
            true
        })),
        ("chat menu", Box::new(move |_, window, elapsed| {
            use slint::ComponentHandle;
            if drawn("chat-menu-search").is_none() || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(3), "the chat's ⋯ menu lists Search in chat");
                return elapsed >= Duration::from_secs(3);
            }
            shot(&out3b, "find-in-chat-03b-chat-menu");
            window.global::<crate::ChatMenu>().invoke_search_in_chat();
            true
        })),
        ("from the menu", Box::new(|_, window, elapsed| {
            use slint::ComponentHandle;
            let (open, ..) = find_bar(window);
            if !open || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(3), "Search in chat opens the find bar");
                return elapsed >= Duration::from_secs(3);
            }
            check(window.global::<crate::ChatMenu>().get_open().is_empty(), "and the menu closes");
            check(crate::find_view::focused(window), "the bar's field has the keyboard");
            headless::press(Key::Escape);
            true
        })),
        ("closed again", Box::new(|_, window, elapsed| {
            let (open, ..) = find_bar(window);
            if open && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(!open, "Escape closes it");
            headless::press_with(&[Key::Control, Key::Shift], "f");
            true
        })),
        ("search every chat", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                check(elapsed < Duration::from_secs(3), "Ctrl+Shift+F opens message search");
                return elapsed >= Duration::from_secs(3);
            }
            check(window.get_switcher_placeholder() == crate::search_view::placeholder(), "Ctrl+Shift+F searches every chat");
            headless::type_text("osc");
            true
        })),
        ("chats tab", Box::new(move |_, window, elapsed| {
            let tabs: Vec<String> = window.get_switcher_tabs().iter().map(|t| t.to_string()).collect();
            if tabs.get(1).is_none_or(|t| t != "1") || elapsed < Duration::from_millis(400) {
                check(elapsed < Duration::from_secs(5), &format!("a chat whose name matches counts under Chats: {tabs:?}"));
                return elapsed >= Duration::from_secs(5);
            }
            let first = window.get_switcher_rows().row_data(0).map(|r| (r.kind.to_string(), r.label.to_string())).unwrap_or_default();
            check(first == ("agent".to_owned(), "Oscar".to_owned()), &format!("All shows the chat first: {first:?}"));
            headless::press_with(&[Key::Control], "2");
            true
        })),
        ("chats only", Box::new(|_, window, elapsed| {
            if window.get_switcher_tab() != 1 || elapsed < Duration::from_millis(200) {
                check(elapsed < Duration::from_secs(3), "Ctrl+2 shows the Chats tab");
                return elapsed >= Duration::from_secs(3);
            }
            check(window.get_switcher_rows().iter().all(|r| r.kind == "agent"), "the Chats tab lists chats only");
            for _ in 0..3 {
                headless::press(Key::Backspace);
            }
            headless::type_text("heron");
            true
        })),
        ("hits", Box::new(move |_, window, elapsed| {
            let tabs: Vec<String> = window.get_switcher_tabs().iter().map(|t| t.to_string()).collect();
            if tabs.get(2).is_none_or(|t| t != "4") {
                check(elapsed < Duration::from_secs(5), &format!("the Host's hits are counted: {tabs:?}"));
                return elapsed >= Duration::from_secs(5);
            }
            check(window.get_switcher_tab() == 1 && window.get_switcher_rows().row_count() == 0, "the tab chosen stays while the page is open");
            headless::press(Key::Tab);
            true
        })),
        ("messages tab", Box::new(move |_, window, elapsed| {
            let rows: Vec<String> = window.get_switcher_rows().iter().filter(|r| r.kind == "message").map(|r| r.label.to_string()).collect();
            if window.get_switcher_tab() != 2 || rows.len() < 4 || elapsed < Duration::from_millis(300) {
                check(elapsed < Duration::from_secs(3), &format!("Tab steps to the Messages tab: {rows:?}"));
                return elapsed >= Duration::from_secs(3);
            }
            check(window.get_switcher_note().is_empty() && window.get_switcher_hint().is_empty(), "the page says nothing more");
            let app = app_now();
            let original = crate::language::stored(app.engine.borrow().settings());
            // The fixture timestamp of o-300, converted to the device's calendar day.
            let day = chrono::DateTime::parse_from_rfc3339("2020-03-20T08:05:00Z").unwrap().with_timezone(&chrono::Local).date_naive();
            let expected = |tag| clarp_core::i18n::format::date_short(tag, day);
            let (english, french) = (expected("en"), expected("fr"));
            check(english != french, "the fixture date distinguishes the two selected locales");
            for (tag, stamp) in [("en", english), ("fr", french)] {
                crate::language::choose(&app, window, tag);
                let has_stamp = |label: &str| label.split(" · ").any(|field| field == stamp);
                let hits: Vec<_> = window.get_switcher_rows().iter().filter(|r| r.kind == "message").collect();
                check(hits.len() == 4 && hits.iter().all(|r| has_stamp(&r.label)), &format!("the four displayed Host hits use the selected {tag} date {stamp}"));
                let local = crate::search_view::items(&mut app.engine.borrow_mut(), "heron", 50).0;
                check(local.iter().any(|r| crate::search_view::split(&r.target) == Some(("oscar", "o-300")) && has_stamp(&r.label)), &format!("the loaded fenced hit uses the selected {tag} date {stamp}"));
            }
            crate::language::choose(&app, window, &original);
            shot(&out4, "find-in-chat-04-search-page");
            // Newest first: the third is o-300.
            window.invoke_switcher_chosen(2);
            true
        })),
        ("opened at the hit", Box::new(move |_, window, elapsed| {
            use slint::ComponentHandle;
            let (open, count, ..) = find_bar(window);
            let done = open && count == "2/4" && app_now().marked_row() == "o-300" && in_view("o-300") && elapsed >= Duration::from_millis(300);
            if !done {
                check(elapsed < Duration::from_secs(8), &format!("the hit opens with the bar at it: {open} {count:?} {:?}", app_now().marked_row()));
                return elapsed >= Duration::from_secs(8);
            }
            check(window.global::<crate::FindBridge>().get_text() == "heron", "the bar holds the words searched");
            check(marked_words("o-300") == Some(true), "and the words are marked");
            fenced_frame(&out5, "find-in-chat-05-from-search", window);
            if let Some(file) = std::env::var_os("CLARP_TEST_CLIPBOARD") {
                window.invoke_copy_message(app_now().active_id(), "o-300".into());
                check(std::fs::read(file).ok().as_deref() == Some(fenced_message().as_bytes()), "copy keeps the original fences, whitespace and markup byte for byte");
            } else {
                check(false, "the isolated check clipboard is configured");
            }
            true
        })),
    ];
    run_stages(stages);
}

pub fn mention_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3) = (out.clone(), out.clone(), out);
    let mentions = || view().mentions.iter().map(|m| m.name.to_string()).collect::<Vec<_>>();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, elapsed| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open || !super::report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::type_text("@mi");
            true
        })),
        ("listed", Box::new(move |_, _, elapsed| {
            if mentions().is_empty() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(mentions().first().is_some_and(|m| m == "Mike"), &format!("@mi lists Mike first: {:?}", mentions()));
            shot(&out1, "mention-01-list");
            headless::press(Key::Tab);
            true
        })),
        ("completed", Box::new(|_, _, _| {
            if app_now().active_draft() != "@Mike " {
                return false;
            }
            check(view().mentions.row_count() == 0, "Tab completes the name and closes the list");
            check(view().route == "Mike", &format!("the route chip names Mike: {:?}", view().route));
            check(super::report().composer_focused, "the composer keeps the keyboard");
            headless::type_text("status please");
            true
        })),
        ("routed", Box::new(move |_, _, elapsed| {
            if app_now().active_draft() != "@Mike status please" || elapsed < Duration::from_millis(200) {
                return false;
            }
            shot(&out2, "mention-02-route");
            headless::press(Key::Return);
            true
        })),
        ("sent", Box::new(move |app, window, _| {
            let sent = sends().into_iter().find(|s| s["body"]["text"] == "@Mike status please");
            let Some(sent) = sent else { return false };
            let marked = rows(window).iter().any(|r| r.author == "user" && r.meta == "→ Mike");
            if app.engine.borrow().selected_session() != "mike" || !marked {
                return false;
            }
            check(sent["body"]["session"] == "mike", &format!("the message went to Mike's session: {}", sent["body"]));
            check(true, "Mike's chat opens with the sent row saying where it went");
            check(app.engine.borrow().draft("rachel").is_empty(), "Rachel's draft is cleared");
            shot(&out3, "mention-03-sent");
            headless::type_text("@");
            true
        })),
        ("all agents", Box::new(move |_, _, elapsed| {
            if mentions().len() < 2 || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(view().mention_current == 0, "the first agent is chosen");
            headless::press(Key::DownArrow);
            true
        })),
        ("down", Box::new(|_, _, _| {
            if view().mention_current != 1 {
                return false;
            }
            headless::press(Key::UpArrow);
            true
        })),
        ("up", Box::new(|_, _, _| {
            if view().mention_current != 0 {
                return false;
            }
            check(true, "Down and Up move through the agents");
            headless::press(Key::Escape);
            true
        })),
        ("escaped", Box::new(|_, _, elapsed| {
            if view().mentions.row_count() != 0 || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(super::report().composer_focused && app_now().active_draft() == "@", "Escape closes the list and the composer keeps typing");
            true
        })),
    ];
    run_stages(stages);
}
