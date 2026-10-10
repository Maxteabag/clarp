//! `--check artifact-keys --out DIR`: every artifact in the chat is used
//! from the keyboard alone. A chat of every type (and images in messages)
//! is driven only by key presses: J/K reach every card, scrolling it into
//! view; each card's keys do what its hints say; viewers scroll and close
//! back to their card; typing in the composer never reaches a card. One
//! stage per group clicks a hint, which must still act like its key.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, headless, posts, report, rows, run_stages, shot};
use crate::ArtifactItem;

/// Every type, as in the artifacts check, each id ending in "-k".
const ALL_TYPES: &[&str] = &["countdown", "decision", "question", "plan", "document", "research", "code_change", "data", "audio", "video", "file", "release", "directory", "workflow_run", "html_form"];

fn card(id: &str) -> Option<ArtifactItem> {
    rows(&crate::window()?).iter().flat_map(|row| row.artifacts.iter().collect::<Vec<_>>()).find(|a| a.id == id)
}

/// What the keyboard can reach in the open chat, top to bottom.
fn reachable(app: &crate::App) -> Vec<String> {
    crate::artifacts_view::selectables(app)
}

fn cursor(app: &crate::App) -> String {
    app.artifact_cursor.borrow().clone()
}

/// The card the keyboard is on, once it is on screen.
fn on(app: &crate::App) -> String {
    crate::artifacts_view::selected(app).unwrap_or_default()
}

/// Lines the app recorded instead of opening them (`CLARP_TEST_OPEN_URL`).
fn opened() -> Vec<String> {
    std::env::var_os("CLARP_TEST_OPEN_URL").and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default().lines().map(str::to_owned).collect()
}

/// The shortcut bar's hints as "keys label".
fn bar(window: &crate::AppWindow) -> Vec<String> {
    window.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label)).collect()
}

/// Stages that walk the keyboard to `id` with J and K alone, one press
/// once the last has landed on screen (a long way takes a few stages).
fn reach(id: &'static str) -> Vec<Stage> {
    let mut stages: Vec<Stage> = Vec::new();
    for _ in 0..6 {
        stages.push((Box::leak(format!("towards {id}").into_boxed_str()), Box::new(move |app, _, elapsed| {
            if on(app) == id || elapsed > Duration::from_secs(12) {
                return true;
            }
            let ids = reachable(app);
            let (Some(target), current) = (ids.iter().position(|i| i == id), ids.iter().position(|i| *i == cursor(app))) else { return false };
            if !cursor(app).is_empty() && on(app).is_empty() {
                return false;
            }
            headless::press(if current.is_some_and(|at| at < target) { "j" } else { "k" });
            false
        })));
    }
    stages.push((Box::leak(format!("reached {id}").into_boxed_str()), Box::new(move |app, _, elapsed| {
        if on(app) != id && elapsed < Duration::from_secs(2) {
            return false;
        }
        check(on(app) == id, &format!("J/K reach {id}: on {:?}, cursor {:?}, on screen {:?}", on(app), cursor(app), crate::artifacts_view::on_screen(app)));
        true
    })));
    stages
}

/// A stage that presses `key` (J or K), one press once the last has
/// landed on screen, until the first (`up`) or last card is reached, for
/// at most 12 s (a walk over every card takes several of these).
fn walk(name: &'static str, key: &'static str, up: bool) -> Stage {
    (name, Box::new(move |app, _, elapsed| {
        let ids = reachable(app);
        let end = if up { ids.first() } else { ids.last() }.cloned().unwrap_or_default();
        if on(app) == end || elapsed > Duration::from_secs(12) {
            return true;
        }
        if !on(app).is_empty() {
            headless::press(key);
        }
        false
    }))
}

/// Waits for `ready`, then checks `what`; past `limit` it fails `what`.
fn wait_for(name: &'static str, limit: Duration, mut ready: impl FnMut(&crate::App, &crate::AppWindow) -> bool + 'static, what: &'static str) -> Stage {
    (name, Box::new(move |app, window, elapsed| {
        let done = ready(app, window);
        if !done && elapsed < limit {
            return false;
        }
        check(done, what);
        true
    }))
}

fn fail_next_log() -> bool {
    control("/__control/fail", &json!({"path": "/log", "status": 504, "count": 1})).is_ok()
}

// ---- the chat

fn chat_stages() -> Vec<Stage> {
    let turns = json!([
        {"id": "im-one-k", "role": "assistant", "text": "Here is the chart:\n\n![Sales by region](clarp-media://asset/img-chart)"},
        {"id": "im-gallery-k", "role": "assistant", "text": "And the rest:\n\n```clarp-gallery\n![Q1](clarp-media://asset/img-a)\n![Q2](/media/img-b)\n![Q3](media/img-c)\n```"},
    ]);
    vec![
        ("load", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let loaded = control("/__control/artifact-chat", &json!({"session": "art-keys", "types": ALL_TYPES, "suffix": "-k", "turns": turns}));
            check(loaded.is_ok(), &format!("the Host takes a chat of every type and two image replies {}", loaded.err().unwrap_or_default()));
            true
        })),
        ("open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("art-keys").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("art-keys");
            crate::pump();
            true
        })),
        ("chat placed", Box::new(|app, _, elapsed| {
            let placed = card("form-stale-k").is_some() && rows(&crate::window().expect("window")).iter().any(|r| r.id == "im-gallery-k");
            if !placed || !report().at_end || elapsed < Duration::from_millis(1200) {
                return false;
            }
            check(report().composer_focused, "the chat opens with the keyboard in the composer");
            let _ = app;
            // The keyboard leaves the composer for the chat.
            headless::press(Key::Escape);
            true
        })),
        wait_for("keyboard on the chat", Duration::from_secs(2), |_, _| report().transcript_focused, "Escape hands the keyboard to the chat"),
    ]
}

// ---- reaching every card

fn reach_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let held = Rc::new(Cell::new(0.0f32));
    let held2 = held.clone();
    let latest = Rc::new(Cell::new(0.0f32));
    let latest2 = latest.clone();
    let mut stages: Vec<Stage> = vec![
        // Escape from the composer (the stage before) hands the chat the
        // keyboard; its keys go there once it has it.
        ("from the latest, no card", Box::new(|app, _, elapsed| {
            if !report().transcript_focused && elapsed < Duration::from_secs(3) {
                return false;
            }
            if !cursor(app).is_empty() {
                headless::press(Key::Escape);
            }
            headless::press(Key::End);
            true
        })),
        ("at the latest", Box::new(|app, _, elapsed| elapsed > Duration::from_millis(800) && cursor(app).is_empty() && report().transcript_focused)),
        ("the bar says how to reach cards", Box::new(move |_, window, _| {
            check(bar(window).iter().any(|h| h == "J/K Cards"), &format!("the shortcut bar shows J/K Cards in the chat: {:?}", bar(window)));
            latest.set(report().offset);
            headless::press("k");
            true
        })),
        wait_for("K from none", Duration::from_secs(2), |app, _| !on(app).is_empty() && reachable(app).last() == Some(&on(app)),
            "K from the chat selects the latest thing the keyboard can reach"),
    ];
    stages.extend(reach("dec-refused-k"));
    stages.extend::<Vec<Stage>>(vec![
        ("typing in the composer", Box::new(|_, _, _| {
            // I: the composer, where card keys are only letters.
            headless::press("i");
            true
        })),
        ("typed", Box::new(|app, _, elapsed| {
            if !report().composer_focused {
                return elapsed > Duration::from_secs(2) && { check(false, "I puts the keyboard in the composer"); true };
            }
            headless::type_text("2jk1");
            headless::press(Key::Delete);
            let _ = app;
            true
        })),
        ("typing reached no card", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            let shown = card("dec-refused-k");
            check(app.active_draft() == "2jk1", &format!("the letters and digits type into the composer: {:?}", app.active_draft()));
            check(cursor(app) == "dec-refused-k" && shown.as_ref().is_some_and(|c| c.chosen == -1 && c.pending), &format!("and reach no card: cursor {:?}, chosen {:?}", cursor(app), shown.map(|c| c.chosen)));
            check(posts("/decisions/d-refused-k/dismiss").is_empty() && posts("/decisions/d-refused-k/resolve").is_empty(), "Delete in the composer discards nothing");
            app.artifact_drafts.borrow_mut().clear();
            headless::press(Key::Escape);
            true
        })),
        wait_for("back on the chat", Duration::from_secs(2), |_, _| report().transcript_focused, "Escape from the composer is back on the chat"),
        ("the banner's Escape first", Box::new(|app, _, elapsed| {
            if on(app) != "dec-refused-k" && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(fail_next_log(), "the Host's next transcript fetch fails");
            headless::press(Key::F5);
            true
        })),
        ("banner up", Box::new(|_, window, elapsed| {
            if !window.get_error().contains("504") {
                return elapsed > Duration::from_secs(4) && { check(false, "the error banner shows"); true };
            }
            headless::press(Key::Escape);
            true
        })),
        ("banner dismissed", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_error().is_empty() && cursor(app) == "dec-refused-k", &format!("Escape dismisses the banner first; the card stays selected: error {:?}, cursor {:?}", window.get_error(), cursor(app)));
            headless::press(Key::Escape);
            true
        })),
        wait_for("then the card", Duration::from_secs(1), |app, _| cursor(app).is_empty(), "the next Escape leaves the card"),
        ("K from none again", Box::new(|_, _, _| {
            headless::press("k");
            true
        })),
        walk("K up the chat", "k", true),
        walk("K on up the chat", "k", true),
        walk("K further up the chat", "k", true),
        walk("K on up the chat again", "k", true),
        walk("K on up to the top", "k", true),
        ("K up the whole chat", Box::new(move |app, _, _| {
            let first = reachable(app).first().cloned().unwrap_or_default();
            let moved = (report().offset - latest2.get()).abs();
            check(on(app) == first, &format!("K walks up every card to the first in the chat: on {:?}, cursor {:?} of {:?}", on(app), cursor(app), crate::artifacts_view::on_screen(app)));
            check(moved > 100.0 && !report().follows, &format!("the keypresses scroll the chat to it (moved {moved}px, follows {})", report().follows));
            shot(&out, "keys-01-first-card");
            held.set(report().offset);
            // The Host moves every card on while the reader is up there.
            let more = "Updated by the agent a moment ago with a longer explanation of what changed and why it matters now.";
            check(control("/__control/artifact-settle", &json!({"session": "art-keys", "more": more})).is_ok(), "the Host moves the cards on");
            true
        })),
        ("activity does not move the reader", Box::new(move |app, _, elapsed| {
            let settled = card("dec-deploy-k").is_some_and(|c| c.resolved == "Approved") && card("cd-launch-k").is_some_and(|c| c.summary.contains("Updated by the agent"));
            if !settled || elapsed < Duration::from_millis(1500) {
                return elapsed > Duration::from_secs(6) && { check(false, "the cards move on"); true };
            }
            let moved = (report().offset - held2.get()).abs();
            check(moved < 1.0, &format!("cards changing around a selected card move nothing ({moved}px)"));
            check(!on(app).is_empty(), "and the card stays selected");
            shot(&out2, "keys-02-held");
            true
        })),
        walk("J down the chat", "j", false),
        walk("J on down the chat", "j", false),
        walk("J further down the chat", "j", false),
        walk("J on down the chat again", "j", false),
        walk("J on down to the latest", "j", false),
        ("J down the whole chat", Box::new(|app, _, _| {
            let last = reachable(app).last().cloned().unwrap_or_default();
            check(on(app) == last && !last.is_empty(), &format!("J walks down every card to the latest: on {:?} of {:?}", on(app), reachable(app)));
            true
        })),
        ("Escape leaves the card", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!cursor(app).is_empty(), "a card is selected");
            headless::press(Key::Escape);
            true
        })),
        wait_for("card left", Duration::from_secs(1), |app, _| cursor(app).is_empty() && report().transcript_focused,
            "Escape on a card leaves it; the keyboard stays on the chat"),
    ]);
    stages
}

/// A card's hints as "key label".
fn hints(id: &str) -> Vec<String> {
    card(id).map(|c| c.hints.iter().map(|h| format!("{} {}", h.key, h.label).trim().to_owned()).collect()).unwrap_or_default()
}

/// The fake Host's last request body to `path`.
fn last_post(path: &str) -> Value {
    posts(path).last().map(|e| e["body"].clone()).unwrap_or(Value::Null)
}

/// A stage that presses `keys` in turn (characters or keys).
fn keys(name: &'static str, keys: Vec<slint::SharedString>) -> Stage {
    (name, Box::new(move |_, _, elapsed| {
        if elapsed < Duration::from_millis(150) {
            return false;
        }
        for key in &keys {
            headless::press(key.clone());
        }
        true
    }))
}

fn k(key: Key) -> slint::SharedString {
    key.into()
}

fn c(text: &str) -> slint::SharedString {
    text.into()
}

// ---- honest hints, and cards in link hint mode

/// The card key hints drawn on screen: (key and label, x, y).
fn drawn_key_hints() -> Vec<(String, f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return Vec::new() };
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_id().is_some_and(|id| id == "key-hint"))
        .find_all()
        .into_iter()
        .filter(|e| e.size().width > 0.0)
        .filter_map(|e| {
            let label = e.accessible_label()?.to_string();
            // A hint without a key (an unselected decision's button) is no key hint.
            (!label.starts_with(' ')).then(|| (label, e.absolute_position().x, e.absolute_position().y))
        })
        .collect()
}

/// The link hint badge on card `id`, if numbered.
fn card_badge(id: &str) -> Option<String> {
    let window = crate::window()?;
    window.global::<crate::LinkHints>().get_badges().iter().find(|b| b.card == id).map(|b| b.label.to_string())
}

fn honest_stages(out: &str) -> Vec<Stage> {
    let (out, out2, out3) = (out.to_owned(), out.to_owned(), out.to_owned());
    let sent = Rc::new(Cell::new(0usize));
    let sent2 = sent.clone();
    let seen = Rc::new(Cell::new(0usize));
    let seen2 = seen.clone();
    vec![
        ("no card selected", Box::new(move |app, _, elapsed| {
            if crate::artifacts_view::on_screen(app).is_empty() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(cursor(app).is_empty(), "no card is selected yet");
            let drawn = drawn_key_hints();
            check(drawn.is_empty(), &format!("cards nobody selected draw no key hints: {drawn:?} (on screen {:?})", crate::artifacts_view::on_screen(app)));
            shot(&out, "keys-00-no-selection");
            headless::press("i");
            true
        })),
        ("composer", Box::new(|app, window, elapsed| {
            if !report().composer_focused {
                return elapsed > Duration::from_secs(2) && { check(false, "I puts the keyboard in the composer"); true };
            }
            check(!crate::artifacts_view::on_screen(app).is_empty() && drawn_key_hints().is_empty(), "with the composer's keyboard, cards on screen draw no key hints");
            check(!bar(window).iter().any(|h| h.starts_with("O ") || h.starts_with("Enter")), &format!("and the bar offers no card key: {:?}", bar(window)));
            headless::press_with(&[Key::Control], "l");
            true
        })),
        ("cards numbered", Box::new(move |app, window, elapsed| {
            if !window.global::<crate::LinkHints>().get_active() || elapsed < Duration::from_millis(300) {
                return elapsed > Duration::from_secs(2) && { check(false, "Ctrl+L from the composer shows the hints"); true };
            }
            let shown = crate::artifacts_view::on_screen(app);
            let numbered: Vec<String> = shown.iter().filter(|id| card_badge(id).is_some()).cloned().collect();
            check(card_badge(IMAGE).is_some(), &format!("the cards on screen get numbers: {numbered:?} of {shown:?}"));
            shot(&out2, "keys-01-cards-in-hint-mode");
            if let Some(label) = card_badge(IMAGE) {
                headless::type_text(&label);
            }
            true
        })),
        ("card opened from the composer", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "image" {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("a card's number opens its viewer (an image enlarged), from the composer: overlay {:?}", window.get_overlay())); true };
            }
            check(true, "a card's number opens its viewer (an image enlarged), from the composer");
            headless::press(Key::Escape);
            true
        })),
        ("viewer closed", Box::new(move |app, window, elapsed| {
            if !window.get_overlay().is_empty() || elapsed < Duration::from_millis(400) {
                return elapsed > Duration::from_secs(2) && { check(false, "Escape closes the viewer"); true };
            }
            // On the card now, with the chat's keyboard: its keys show, on it alone.
            let keys = drawn_key_hints();
            let rect = crate::artifacts_view::card_rect(app, IMAGE);
            let inside = rect.is_some_and(|(x, y, w, h)| keys.iter().all(|(_, kx, ky)| *kx >= x && *kx < x + w && *ky >= y && *ky < y + h));
            check(on(app) == IMAGE && report().transcript_focused, &format!("closing it leaves the keyboard on the card in the chat: on {:?}", on(app)));
            check(!keys.is_empty() && inside, &format!("only the selected card draws its key hints: {keys:?} in {rect:?}"));
            check(bar(window).iter().any(|h| h == "O Enlarge"), &format!("and the bar shows them: {:?}", bar(window)));
            shot(&out3, "keys-02-selected-card");
            headless::press("i");
            true
        })),
        // The card stays selected; the composer has the keyboard.
        ("composer Enter", Box::new(move |app, window, elapsed| {
            if !report().composer_focused {
                return elapsed > Duration::from_secs(2) && { check(false, "I puts the keyboard in the composer"); true };
            }
            check(cursor(app) == IMAGE && drawn_key_hints().is_empty(), &format!("a selected card draws no key hints while the composer has the keyboard: {:?}", drawn_key_hints()));
            check(!bar(window).iter().any(|h| h.starts_with("O ")), &format!("nor does the bar offer them: {:?}", bar(window)));
            sent.set(posts("/send").len());
            seen.set(opened().len());
            headless::type_text("hello cards");
            headless::press(Key::Return);
            true
        })),
        ("composer sent", Box::new(move |app, window, elapsed| {
            let done = posts("/send").len() > sent2.get();
            if !done && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(done && app.active_draft().is_empty(), "Enter in the composer with cards on screen sends the message");
            check(opened().len() == seen2.get() && window.get_overlay().is_empty(), &format!("and opens nothing: {:?}, overlay {:?}", opened().last(), window.get_overlay()));
            headless::press(Key::Escape);
            true
        })),
        ("back to the chat", Box::new(|app, _, elapsed| {
            if !report().transcript_focused {
                return elapsed > Duration::from_secs(2) && { check(false, "Escape hands the keyboard to the chat"); true };
            }
            if !cursor(app).is_empty() {
                headless::press(Key::Escape);
            }
            true
        })),
        wait_for("card left", Duration::from_secs(2), |app, _| cursor(app).is_empty() && drawn_key_hints().is_empty(), "Escape leaves the card and no key hints show"),
    ]
}

// ---- decisions and questions

fn decision_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(reach("dec-deploy-k"));
    stages.extend::<Vec<Stage>>(vec![
        ("approval chosen", Box::new(move |_, _, _| {
            headless::press("2");
            let hold = card("dec-deploy-k").map(|c| c.chosen).unwrap_or(-2);
            headless::press("1");
            let ship = card("dec-deploy-k").map(|c| c.chosen).unwrap_or(-2);
            check(hold == 1 && ship == 0 && posts("/decisions/d-deploy-k/resolve").is_empty(), &format!("2 chooses Hold, 1 Ship it, nothing sent yet: {hold} {ship}"));
            true
        })),
        ("approval shown chosen", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            shot(&out, "keys-10-decision-chosen");
            check(hints("dec-deploy-k").contains(&"1 again Send".to_owned()), &format!("the chosen answer's digit again sends it: {:?}", hints("dec-deploy-k")));
            headless::press(Key::Return);
            true
        })),
        ("Enter sends nothing", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            check(posts("/decisions/d-deploy-k/resolve").is_empty(), "Enter on a decision sends nothing (it is no card key)");
            headless::press("1");
            true
        })),
        wait_for("approval sent", Duration::from_secs(4), |_, _| last_post("/decisions/d-deploy-k/resolve") == json!({"choice": "accepted", "expected_revision": 4}),
            "1 again sends the chosen answer: approved against the revision seen"),
    ]);
    stages.extend(reach("dec-discard-k"));
    stages.push(keys("discard", vec![k(Key::Delete)]));
    stages.push(wait_for("discarded", Duration::from_secs(4), |_, _| last_post("/decisions/d-discard-k/dismiss") == json!({"expected_revision": 4}), "Delete discards a pending decision"));
    stages.extend(reach("q-trip-k"));
    stages.push(keys("question option", vec![c("2"), c("2")]));
    stages.push(wait_for("question answered", Duration::from_secs(4), |_, _| last_post("/decisions/q-trip-k/resolve")["answer"]["option_id"] == "tromso", "2 then 2 again answers a question with its second option"));
    stages.extend(reach("q-custom-k"));
    stages.push(keys("own answer", vec![c("3")]));
    stages.push(("own answer typed", Box::new(|app, window, elapsed| {
        let editing = card("q-custom-k").is_some_and(|c| c.editing) && crate::commands::context(app, window) == "composer";
        if !editing {
            return elapsed > Duration::from_secs(2) && { check(false, "the last number opens the answer of one's own, typing into it"); true };
        }
        headless::type_text("Friday, after lunch");
        headless::press(Key::Return);
        true
    })));
    stages.push(wait_for("own answer sent", Duration::from_secs(4), |_, _| last_post("/decisions/q-custom-k/resolve")["answer"]["text"] == "Friday, after lunch", "Enter in it sends the written answer"));
    stages.extend(reach("q-escape-k"));
    stages.push(keys("own answer kept", vec![c("3")]));
    stages.push(("own answer escaped", Box::new(|_, window, elapsed| {
        if !window.global::<crate::ArtifactBridge>().get_editing() {
            return elapsed > Duration::from_secs(2) && { check(false, "3 opens its field"); true };
        }
        headless::type_text("Somewhere warm");
        headless::press(Key::Escape);
        true
    })));
    stages.push(("back on the question", Box::new(|app, _, elapsed| {
        if elapsed < Duration::from_millis(400) {
            return false;
        }
        check(report().transcript_focused && cursor(app) == "q-escape-k" && card("q-escape-k").is_some_and(|c| c.pending && c.draft == "Somewhere warm"),
            &format!("Escape keeps the draft and the keyboard on the question: cursor {:?}, chat focused {}, card {:?}", cursor(app), report().transcript_focused, card("q-escape-k").map(|c| (c.pending, c.draft.to_string(), c.editing))));
        headless::press(Key::Delete);
        true
    })));
    stages.push(wait_for("question cancelled", Duration::from_secs(4), |_, _| !posts("/decisions/q-escape-k/dismiss").is_empty(), "Delete then cancels the question"));
    stages
}

// ---- viewers

fn viewer_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let mut stages: Vec<Stage> = Vec::new();
    for (id, kind) in [("plan-ship-k", "PLAN"), ("doc-huge-k", ""), ("res-market-k", "RESEARCH"), ("cc-big-k", "CODE CHANGE"), ("data-sales-k", "DATA"), ("rel-ready-k", "RELEASE")] {
        stages.extend(reach(id));
        stages.push(keys("open the viewer", vec![c("o")]));
        stages.push((Box::leak(format!("{id} viewer").into_boxed_str()), Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" {
                return elapsed > Duration::from_secs(3) && { check(false, &format!("O opens {id} in its viewer")); true };
            }
            check(kind.is_empty() || window.get_report_kind() == kind, &format!("O opens {id} in its viewer: {:?}", window.get_report_kind()));
            true
        })));
        if id == "doc-huge-k" {
            let out = out.clone();
            let chat = Rc::new(Cell::new(0.0f32));
            let (chat2, chat3) = (chat.clone(), chat.clone());
            let top = Rc::new(Cell::new(0.0f32));
            let (top2, top3) = (top.clone(), top.clone());
            stages.push(("scroll the viewer", Box::new(move |_, window, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                chat.set(report().offset);
                top.set(window.get_report_offset());
                headless::press(Key::DownArrow);
                headless::press(Key::DownArrow);
                true
            })));
            stages.push(("viewer arrows", Box::new(move |_, window, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let moved = top2.get() - window.get_report_offset();
                check(moved > 30.0, &format!("Down scrolls the viewer ({moved}px)"));
                top2.set(window.get_report_offset());
                headless::press(Key::PageDown);
                true
            })));
            stages.push(("viewer page", Box::new(move |_, window, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let moved = top3.get() - window.get_report_offset();
                check(moved > 200.0, &format!("Page Down pages it ({moved}px)"));
                headless::press(Key::End);
                true
            })));
            stages.push(("viewer end", Box::new(move |_, window, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                let at = window.get_report_offset();
                check(at < -2000.0, &format!("End goes to its end ({at}px)"));
                shot(&out, "keys-20-viewer-end");
                headless::press(Key::PageUp);
                headless::press(Key::UpArrow);
                headless::press(Key::Home);
                true
            })));
            stages.push(("viewer home", Box::new(move |_, window, elapsed| {
                if elapsed < Duration::from_millis(300) {
                    return false;
                }
                check(window.get_report_offset().abs() < 1.0, &format!("Home goes back to its top ({}px)", window.get_report_offset()));
                check((report().offset - chat2.get()).abs() < 1.0, &format!("and none of these keys scrolled the chat under it ({} then {})", chat3.get(), report().offset));
                true
            })));
        }
        if id == "rel-ready-k" {
            stages.push(("open the release's link", Box::new(|_, _, _| {
                headless::press("1");
                true
            })));
            stages.push(wait_for("release link opened", Duration::from_secs(2), |_, _| opened().iter().any(|u| u == "https://github.com/example/clarp/releases/2.4.0"),
                "1 in the viewer opens its first link (the release's source)"));
        }
        stages.push(keys("close the viewer", vec![k(Key::Escape)]));
        stages.push((Box::leak(format!("{id} closed").into_boxed_str()), Box::new(move |app, window, elapsed| {
            let back = window.get_overlay().is_empty() && report().transcript_focused && on(app) == id;
            if !back && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(back, &format!("Escape closes it, back on {id} in the chat: overlay {:?}, chat has the keyboard {}, on {:?}", window.get_overlay(), report().transcript_focused, on(app)));
            true
        })));
    }
    stages
}

// ---- audio

fn audio_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(reach("aud-brief-k"));
    stages.push(keys("play", vec![c("o")]));
    stages.push(("playing", Box::new(|_, _, elapsed| {
        if !card("aud-brief-k").is_some_and(|c| c.media_state == "playing") {
            return elapsed > Duration::from_secs(4) && { check(false, "O plays the clip"); true };
        }
        check(true, "O plays the clip");
        headless::press(Key::RightArrow);
        headless::press(Key::RightArrow);
        true
    })));
    stages.push(("seeked forward", Box::new(move |_, _, elapsed| {
        let played = card("aud-brief-k").map(|c| c.media_played).unwrap_or(0);
        if played < 20 && elapsed < Duration::from_secs(2) {
            return false;
        }
        check(played >= 20, &format!("Right twice seeks forward 20 s ({played}s)"));
        shot(&out, "keys-30-audio-seek");
        headless::press(Key::LeftArrow);
        true
    })));
    stages.push(("seeked back", Box::new(|_, _, elapsed| {
        let played = card("aud-brief-k").map(|c| c.media_played).unwrap_or(0);
        if played >= 20 && elapsed < Duration::from_secs(2) {
            return false;
        }
        check((10..20).contains(&played), &format!("Left seeks back 10 s ({played}s)"));
        headless::press("s");
        true
    })));
    stages.push(wait_for("stopped", Duration::from_secs(2), |_, _| card("aud-brief-k").is_some_and(|c| c.media_state == "idle" && c.action == "Play"), "S stops it"));
    stages.push(keys("play again", vec![c("o")]));
    stages.push(("playing again", Box::new(|_, _, elapsed| {
        if !card("aud-brief-k").is_some_and(|c| c.media_state == "playing") {
            return elapsed > Duration::from_secs(4) && { check(false, "O plays it again"); true };
        }
        headless::press("o");
        true
    })));
    stages.push(wait_for("paused", Duration::from_secs(2), |_, _| card("aud-brief-k").is_some_and(|c| c.media_state == "paused"), "O pauses it"));
    stages.push(keys("stop for good", vec![c("s")]));
    stages
}

// ---- cards that open something

fn open_stages(out: &str) -> Vec<Stage> {
    let out = out.to_owned();
    let mut stages: Vec<Stage> = Vec::new();
    let opens: [(&'static str, &'static str, fn(&str) -> bool); 5] = [
        ("form-trip-k", "O Open form", |u| u.contains("/form/")),
        ("vid-demo-k", "O Play video", |u| u.starts_with("file://") && u.ends_with("cards-demo.mp4")),
        ("file-pdf-k", "O Open file", |u| u.starts_with("file://") && u.ends_with("contract-2026-signed.pdf")),
        ("wf-ci-k", "O Open in GitHub", |u| u == "https://github.com/example/clarp/actions/runs/901"),
        // A report opens in the browser too, on its own loopback page.
        ("form-report-k", "O Open report", |u| u.contains("/form/")),
    ];
    for (id, hint, wanted) in opens {
        let before = std::rc::Rc::new(std::cell::Cell::new(0usize));
        let before2 = before.clone();
        stages.extend(reach(id));
        stages.push((Box::leak(format!("{id} hint").into_boxed_str()), Box::new(move |_, window, _| {
            check(bar(window).iter().any(|h| h == hint), &format!("on {id} the shortcut bar shows {hint:?}: {:?}", bar(window)));
            before.set(opened().len());
            headless::press("o");
            true
        })));
        stages.push((Box::leak(format!("{id} opened").into_boxed_str()), Box::new(move |_, window, elapsed| {
            let done = opened().iter().skip(before2.get()).any(|u| wanted(u));
            if !done && elapsed < Duration::from_secs(8) {
                return false;
            }
            check(done, &format!("O on {id} opens it: {:?}", opened().last()));
            if window.get_overlay() == "report" {
                headless::press(Key::Escape);
            }
            true
        })));
    }
    stages.extend(reach("dir-out-k"));
    stages.push(keys("open the folder", vec![c("o")]));
    stages.push(wait_for("folder", Duration::from_secs(3), |_, _| card("dir-out-k").is_some_and(|c| c.status_text.starts_with("On the Host")), "O on a folder the desktop cannot open says where it is"));
    stages.extend(reach("cd-launch-k"));
    stages.push(("countdown", Box::new(move |_, window, _| {
        let before = (opened().len(), window.get_overlay().to_string());
        headless::press("o");
        check((opened().len(), window.get_overlay().to_string()) == before, "O on a countdown does nothing (it has no action)");
        check(hints("cd-launch-k").is_empty() && !bar(window).iter().any(|h| h.starts_with("O ")), &format!("and it shows no O hint: {:?} {:?}", hints("cd-launch-k"), bar(window)));
        shot(&out, "keys-40-countdown-selected");
        true
    })));
    stages
}

// ---- images and galleries

const GALLERY: &str = "img:im-gallery-k:1";
const IMAGE: &str = "img:im-one-k:1";

fn image_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(reach(GALLERY));
    stages.push(("next tile", Box::new(|_, window, _| {
        check(window.global::<crate::ArtifactBridge>().get_tile() == 0, "a gallery is reached on its first tile");
        headless::press(Key::RightArrow);
        true
    })));
    stages.push(wait_for("on the second tile", Duration::from_secs(1), |_, window| window.global::<crate::ArtifactBridge>().get_tile() == 1, "Right moves to the next tile"));
    stages.push(("enlarge", Box::new(move |_, window, _| {
        shot(&out, "keys-50-gallery-tile");
        check(bar(window).iter().any(|h| h == "O Enlarge"), &format!("the bar says O enlarges it: {:?}", bar(window)));
        headless::press("o");
        true
    })));
    stages.push(("enlarged", Box::new(move |_, window, elapsed| {
        if window.get_overlay() != "image" {
            return elapsed > Duration::from_secs(2) && { check(false, &format!("O enlarges the tile: overlay {:?}", window.get_overlay())); true };
        }
        check(window.get_image_view_index() == 1 && window.get_image_view_count() == 3, &format!("O enlarges the tile it is on: {} of {}", window.get_image_view_index(), window.get_image_view_count()));
        shot(&out2, "keys-51-gallery-enlarged");
        headless::press(Key::RightArrow);
        headless::press(Key::RightArrow);
        true
    })));
    stages.push(("moved in the viewer", Box::new(|_, window, elapsed| {
        if elapsed < Duration::from_millis(200) {
            return false;
        }
        check(window.get_image_view_index() == 2, &format!("Right moves to the next picture, and stops at the last: {}", window.get_image_view_index()));
        headless::press(Key::LeftArrow);
        headless::press(Key::Escape);
        true
    })));
    stages.push(wait_for("back on the gallery", Duration::from_secs(2), |app, window| {
        window.get_overlay().is_empty() && report().transcript_focused && on(app) == GALLERY && window.global::<crate::ArtifactBridge>().get_tile() == 1
    }, "Left moves back; Escape closes it, back on the gallery's tile"));
    stages.extend(reach(IMAGE));
    stages.push(keys("enlarge the image", vec![c("o")]));
    stages.push(wait_for("image enlarged", Duration::from_secs(2), |_, window| window.get_overlay() == "image" && window.get_image_view_count() == 1, "O enlarges a single image"));
    stages.push(keys("close the image", vec![k(Key::Escape)]));
    stages.push(wait_for("image closed", Duration::from_secs(2), |app, window| window.get_overlay().is_empty() && on(app) == IMAGE, "Escape closes it, back on the image"));
    stages
}

// ---- hints in place of buttons

fn hint_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let mut stages: Vec<Stage> = Vec::new();
    stages.push(("every card's hints", Box::new(|app, _, _| {
        let ids: Vec<String> = reachable(app).into_iter().filter(|i| !i.starts_with("img:")).collect();
        let bare: Vec<String> = ids.iter().filter(|id| card(id).is_some_and(|c| !c.action.is_empty() && c.hints.row_count() == 0)).cloned().collect();
        check(!ids.is_empty() && bare.is_empty(), &format!("every card with an action shows its keys as hints: without {bare:?}"));
        check(hints("dec-click-k") == ["1 Turn on", "2 Not now", "Send", "Del Discard"], &format!("an approval (Send has no key before a choice): {:?}", hints("dec-click-k")));
        check(hints("q-click-k") == ["Send", "Del Discard"], &format!("a question (its options are numbered): {:?}", hints("q-click-k")));
        check(hints("plan-ship-k") == ["O Open plan"], &format!("a plan: {:?}", hints("plan-ship-k")));
        check(hints("aud-gone-k") == ["O Play", "←/→ Seek", "S Stop"], &format!("audio: {:?}", hints("aud-gone-k")));
        check(hints("vid-none-k").is_empty() && hints("cd-past-k").is_empty(), "a card with nothing to do has none");
        true
    })));
    stages.extend(reach("dec-click-k"));
    stages.push(("selected decision", Box::new(move |_, window, elapsed| {
        if elapsed < Duration::from_millis(400) {
            return false;
        }
        for hint in ["1 Turn on", "2 Not now", "Del Discard"] {
            check(bar(window).iter().any(|h| h == hint), &format!("the shortcut bar shows the card's {hint:?}: {:?}", bar(window)));
        }
        shot(&out, "keys-60-decision-hints");
        true
    })));
    stages.push(("click a hint", Box::new(|app, window, elapsed| {
        // Its place, once it has reported it since the keyboard landed.
        if crate::artifacts_view::card_rect(app, "dec-click-k").is_none() && elapsed < Duration::from_secs(3) {
            return false;
        }
        // A real click on the card's first hint, "1 Turn on", at the
        // left of its bottom row.
        let Some((x, y, _, height)) = crate::artifacts_view::card_rect(app, "dec-click-k") else {
            check(false, "the decision is on screen");
            return true;
        };
        super::click_at(window, x + 24.0, y + height - 19.0);
        true
    })));
    stages.push(wait_for("hint clicked", Duration::from_secs(3), |_, _| last_post("/decisions/d-click-k/resolve") == json!({"choice": "accepted", "expected_revision": 4}),
        "a click on a hint does what its key does: 1 Turn on approves"));
    stages.extend(reach("plan-ship-k"));
    stages.push(("click open", Box::new(|app, window, elapsed| {
        // Its place, once it has reported it since the keyboard landed.
        if crate::artifacts_view::card_rect(app, "plan-ship-k").is_none() && elapsed < Duration::from_secs(3) {
            return false;
        }
        let Some((x, y, _, height)) = crate::artifacts_view::card_rect(app, "plan-ship-k") else {
            check(false, "the plan is on screen");
            return true;
        };
        super::click_at(window, x + 24.0, y + height - 19.0);
        true
    })));
    stages.push(wait_for("plan clicked", Duration::from_secs(3), |_, window| window.get_overlay() == "report", "a click on O Open plan opens the plan"));
    stages.push(keys("close the plan", vec![k(Key::Escape)]));
    // A narrow pane: the hints fit.
    stages.extend(reach("dec-deploy-k"));
    stages.push(("split the chat", Box::new(|_, _, _| {
        headless::press_with(&[Key::Alt], "v");
        true
    })));
    stages.push(("narrow pane", Box::new(move |_, _, elapsed| {
        if elapsed < Duration::from_millis(1500) {
            return false;
        }
        shot(&out2, "keys-61-narrow-pane");
        // The new pane's composer has the keyboard: the chat closes it.
        headless::press(Key::Escape);
        true
    })));
    stages.push(("close the split", Box::new(|_, _, elapsed| {
        if !report().transcript_focused {
            return elapsed > Duration::from_secs(2) && { check(false, "Escape hands the new pane's keyboard to its chat"); true };
        }
        headless::press_with(&[Key::Alt], "x");
        true
    })));
    stages.push(("one pane again", Box::new(|app, _, elapsed| {
        if elapsed < Duration::from_millis(800) {
            return false;
        }
        check(app.pane_drafts().len() == 1, &format!("Alt+X closes the split: {} panes", app.pane_drafts().len()));
        headless::press(Key::Escape);
        true
    })));
    stages
}

pub(super) fn artifact_keys_check(out: String) {
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(chat_stages());
    stages.extend(honest_stages(&out));
    stages.extend(decision_stages(&out));
    stages.extend(viewer_stages(&out));
    stages.extend(audio_stages(&out));
    stages.extend(open_stages(&out));
    stages.extend(image_stages(&out));
    stages.extend(hint_stages(&out));
    stages.extend(reach_stages(&out));
    let _ = (Value::Null, RefCell::new(()), app_now);
    run_stages(stages);
}
