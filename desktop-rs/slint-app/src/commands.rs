//! What the keyboard map's actions do (Main.qml `runCommand`), and where
//! the keyboard is, which picks the map's state.

use std::rc::Rc;

use clarp_core::tr;
use clarp_engine::Change;
use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::keymap::{self, Facts};
use crate::{App, AppWindow, Focus, Hint, SwitcherRow, pump_now, switcher};

/// The keyboard map's state for where the keyboard is now.
pub fn context(app: &App, window: &AppWindow) -> &'static str {
    // Link hints take every key while they show.
    if crate::link_hints::active() {
        return "hints";
    }
    // The key bindings editor takes every key (it says which in the bar).
    if !app.switcher.borrow().open && *app.overlay.borrow() == "keymap" {
        return "keymap";
    }
    // The font picker takes its keys; letters type into its search.
    if !app.switcher.borrow().open && *app.overlay.borrow() == crate::font_view::OVERLAY {
        return "fonts";
    }
    // The processes panel moves, opens and stops with its own keys.
    if !app.switcher.borrow().open && *app.overlay.borrow() == crate::processes_view::OVERLAY {
        return "processes";
    }
    // An agent's publications move, filter and open with their own keys.
    if !app.switcher.borrow().open && *app.overlay.borrow() == crate::publications_view::OVERLAY {
        return "publications";
    }
    if app.switcher.borrow().open || !app.overlay.borrow().is_empty() {
        // ---- launch dialogs: the hub has its own keyboard state.
        if !app.switcher.borrow().open && *app.overlay.borrow() == crate::launch_view::HUB {
            return "launch";
        }
        if !app.switcher.borrow().open && crate::profile_view::transit_open(app, window) {
            return "transit";
        }
        // The agent profile: its rows, sections and layers by key.
        if !app.switcher.borrow().open && crate::profile_view::keyboard(app, window) {
            return "profile";
        }
        return "modal";
    }
    match window.get_surface().as_str() {
        "updates" => return "updates",
        "teams" => return "teams",
        "settings" if window.get_settings_search_focused() => return "settings-search",
        "settings" => return "settings",
        _ => {}
    }
    if window.get_search_focused() {
        "search"
    } else if crate::find_view::focused(window) && window.get_surface() == "chats" {
        // The find bar's field: its own keys, then the composer's.
        "find"
    } else if app.active_report().composer_focused || window.global::<crate::ArtifactBridge>().get_editing() || typing_in_chats(window) {
        // A card's answer field types like the composer.
        "composer"
    } else if window.get_sidebar_focused() {
        "sidebar"
    } else {
        "pane"
    }
}

/// A text field in the chats has the keyboard, known the moment it does: a
/// pane reports its composer's focus a frame later, and keys typed straight
/// after a click on the composer (in Normal mode) would otherwise go to
/// Normal mode instead of the draft.
fn typing_in_chats(window: &AppWindow) -> bool {
    window.get_text_input_focused() && window.get_surface() == "chats" && !window.get_sidebar_focused()
}

fn facts(app: &App, window: &AppWindow) -> Facts {
    let engine = app.engine.borrow();
    let selected = engine.selected_session();
    Facts {
        attention: !engine.next_attention_session().is_empty(),
        agent: !selected.is_empty() && !selected.starts_with("pair:"),
        rows: window.get_chats().row_count() > 0,
        can_send: engine.can_send(selected),
        // The Host's word, or its live items' while they say it works.
        busy: engine.roster().find(selected).is_some_and(|a| matches!(a.latest_state.as_str(), "thinking" | "tool" | "compacting"))
            || crate::live_view::status(&engine, selected).1,
        playing: crate::platform::audio::with(|audio| audio.playing()).unwrap_or(false),
        behind: !app.active_report().at_end,
        folds: window.get_chats().iter().any(|c| c.fold_count > 0),
        artifacts: crate::artifacts_view::has_cards(app),
        artifact: crate::artifacts_view::selected(app).is_some(),
        layout_warning: !window.get_save_warning().is_empty(),
        work: engine.roster().find(selected).is_some_and(|a| engine.roster().running_work(a) > 0),
        queued: crate::queue_strip::first_waiting(&engine, selected).is_some(),
    }
}

/// Whether vim mode is on (the setting, on unless turned off).
pub fn vim_on(app: &App) -> bool {
    app.engine.borrow().settings().boolean(crate::vim_view::SETTING, true)
}

/// The user's own key bindings, from settings.
pub fn overrides(app: &App) -> keymap::Overrides {
    overrides_of(&app.engine.borrow())
}

pub fn overrides_of(engine: &clarp_engine::Engine) -> keymap::Overrides {
    engine.settings().get("keymap/bindings").map(keymap::from_settings).unwrap_or_default()
}

/// The region that has the keyboard now, or the one that last had it (a
/// dialog gives the keyboard back to it).
pub fn keyboard_region(app: &App, window: &AppWindow) -> &'static str {
    region(app, window).unwrap_or_else(|| LAST_REGION.with(std::cell::Cell::get))
}

/// The region that has the keyboard, if one does (none while the window
/// is behind another).
fn region(app: &App, window: &AppWindow) -> Option<&'static str> {
    let report = app.active_report();
    if window.get_search_focused() {
        Some("search")
    } else if report.composer_focused {
        Some("composer")
    } else if window.get_sidebar_focused() {
        Some("sidebar")
    } else if report.transcript_focused {
        Some("pane")
    } else {
        None
    }
}

thread_local! {
    /// The region that last had the keyboard.
    static LAST_REGION: std::cell::Cell<&'static str> = const { std::cell::Cell::new("composer") };
}

/// The window is in front again (or a key arrived) and nothing in it has
/// the keyboard: the region that last had it gets it back.
pub fn regain_keyboard(app: &App, window: &AppWindow) {
    // A dialog keeps its own.
    if !app.overlay.borrow().is_empty() {
        return;
    }
    // What has the keyboard must be shown: a key for something hidden (the
    // chats' composer behind Updates) goes nowhere, not even to the
    // window's own keys (Ctrl+F did nothing in Updates, #846).
    if i_slint_core::window::WindowInner::from_pub(window.window()).focus_item.borrow().upgrade().is_some_and(|item| item.is_visible()) {
        return;
    }
    if app.switcher.borrow().open {
        eprintln!("clarp-slint: keyboard: nothing shown had the keyboard; giving it back to the switcher");
        window.invoke_focus_switcher();
        return;
    }
    if surface_takes_keyboard(window) {
        eprintln!("clarp-slint: keyboard: nothing shown had the keyboard; giving it back to {}", window.get_surface());
        return;
    }
    let region = LAST_REGION.with(std::cell::Cell::get);
    eprintln!("clarp-slint: keyboard: nothing had the keyboard; giving it back to the {region}");
    match region {
        "search" => window.invoke_focus_search(),
        "sidebar" => window.invoke_focus_sidebar(),
        "pane" => app.focus_transcript(),
        _ => app.focus_composer(),
    }
}

/// Updates the shortcut bar for where the keyboard is.
pub fn show_hints(app: &App, window: &AppWindow) {
    if let Some(region) = region(app, window) {
        LAST_REGION.with(|r| r.set(region));
    }
    let state = context(app, window);
    let overrides = overrides(app);
    let card_keys = if state == "pane" { crate::artifacts_view::selected_hints(app) } else { None };
    // Runs on every scroll report: the defaults are spelled without
    // resolving the map again.
    let shown = |action: &str, default: &str| keymap::shown_or(state, action, &overrides, default);
    let bound: Vec<keymap::Binding> = keymap::hints(state, &overrides, facts(app, window))
        .into_iter()
        // The keyboard's card says what its keys do (below).
        .filter(|b| card_keys.is_none() || !b.action.starts_with("artifact-") || b.action == "artifact-previous")
        .filter(|b| b.action != "artifact-open")
        .collect();
    // Each hint's action, beside it (the labels are in the app's language).
    let mut actions: Vec<&str> = bound.iter().map(|b| b.action).collect();
    let mut hints: Vec<Hint> = bound
        .iter()
        .map(|b| Hint {
            // J and K both walk the cards; the hints' digits are their numbers.
            keys: if b.action == "artifact-previous" {
                format!("{}/{}", shown("artifact-next", "J"), shown("artifact-previous", "K")).trim_matches('/').into()
            } else if b.action == "hint-digit" {
                match crate::link_hints::count() {
                    1 => "1".into(),
                    n => format!("1-{n}").into(),
                }
            } else {
                // The user's own key leads.
                b.keys.first().map(|k| keymap::display(k)).unwrap_or_default().into()
            },
            label: if b.action == "toggle-preview" {
                if app.engine.borrow().settings().boolean("explorer/livePreview", false) {
                    // i18n: shortcut bar, the explorer's live preview key while the preview is on
                    tr!("Preview: on").into()
                } else {
                    // i18n: shortcut bar, the explorer's live preview key while the preview is off
                    tr!("Preview: off").into()
                }
            } else {
                b.label.as_str().into()
            },
        })
        .collect();
    if let Some(keys) = card_keys {
        let at = actions.iter().position(|a| *a == "artifact-previous").map_or(0, |i| i + 1);
        for (offset, (keys, label)) in keys.into_iter().enumerate() {
            hints.insert(at + offset, Hint { keys: keys.into(), label: label.into() });
            actions.insert(at + offset, "");
        }
    }
    // From the composer, the way onto the chat's cards.
    if state == "composer" && crate::artifacts_view::has_cards(app) && !window.global::<crate::ArtifactBridge>().get_editing() {
        let keys = format!("{} {}", shown("escape", "Esc"), keymap::shown_or("pane", "artifact-previous", &overrides, "K"));
        // i18n: shortcut bar in the composer, the keys that move the keyboard onto the chat's cards (noun: the artifact cards)
        hints.push(Hint { keys: keys.trim().into(), label: tr!("Cards").into() });
        actions.push("artifact-previous");
    }
    if state == "keymap" {
        hints = crate::keymap_view::hints(app, window).into_iter().map(|(keys, label)| Hint { keys: keys.into(), label: label.into() }).collect();
        actions.clear();
    }
    if state == "processes" {
        hints = crate::processes_view::hints();
        actions.clear();
    }
    if state == "find" {
        hints = crate::find_view::hints();
    }
    if state == "publications" {
        hints = crate::publications_view::hints(window);
        actions.clear();
    }
    if state == "fonts" {
        hints = crate::font_view::hints();
        actions.clear();
    }
    if state == "profile" {
        hints = crate::profile_view::hints(app, window);
        actions.clear();
    }
    if state == "hints" && crate::link_hints::count() == 0 {
        // i18n: shortcut bar while link numbers show, when there is no link on screen to number
        hints = vec![Hint { keys: "".into(), label: tr!("No links on screen").into() }];
        actions.clear();
    }
    // Vim mode's own keys lead; Space is its leader, the switcher two of them.
    if vim_on(app) {
        for (hint, action) in hints.iter_mut().zip(actions.iter()) {
            match *action {
                "switcher" if matches!(state, "pane" | "sidebar") => hint.keys = "Space Space".into(),
                "artifact-previous" => hint.keys = "Shift+J/K".into(),
                _ => {}
            }
        }
        let mut lead = crate::vim_view::hints(state);
        lead.append(&mut hints);
        hints = lead;
    }
    crate::vim_view::show(app, window, state);
    let mode = match state {
        // i18n: shortcut bar, the keyboard mode badge in capitals while typing in the composer (as vim's insert mode)
        "composer" => tr!("INSERT"),
        // i18n: shortcut bar, the keyboard mode badge in capitals while the keyboard is on the explorer (the list of chats)
        "sidebar" => tr!("EXPLORER"),
        // i18n: shortcut bar, the keyboard mode badge in capitals while the keyboard is on the chat
        "pane" => tr!("CHAT"),
        // i18n: shortcut bar, the keyboard mode badge in capitals while the key-binding editor is open
        "keymap" => tr!("KEYS"),
        // i18n: shortcut bar, the keyboard mode badge in capitals while the font picker is open
        "fonts" => tr!("FONT"),
        other => other.to_uppercase(),
    };
    window.set_keyboard_mode(mode.into());
    // The one focus ring follows the same state.
    window.global::<Focus>().set_mode(state.into());
    // Every scroll report comes here: a new model for the same hints would
    // rebuild the shortcut bar and repaint it with the chat's every frame
    // (merged with the chat's repaint into a box over most of the window).
    let current = window.get_hints();
    if current.row_count() != hints.len() || current.iter().zip(hints.iter()).any(|(a, b)| a != *b) {
        window.set_hints(ModelRc::new(VecModel::from(hints)));
    }
}

thread_local! {
    static PRESSES: std::cell::RefCell<keymap::Presses> = std::cell::RefCell::new(keymap::Presses::default());
}

/// A key the window saw before any control: true when a binding ran.
pub fn shortcut(text: &str, control: bool, alt: bool, shift: bool, meta: bool, repeat: bool) -> bool {
    use crate::platform::keyboard;
    let used = run_shortcut(text, control, alt, shift, meta, repeat);
    if keyboard::tracing() {
        let held = keyboard::Modifiers { control, alt, shift, meta };
        let chord = keymap::chord(text, held.shortcut_control(), alt, shift).unwrap_or_default();
        let (state, focus) = match (crate::app(), crate::window()) {
            (Some(app), Some(window)) => (context(&app, &window).to_string(), keyboard::focus_described(window.window())),
            _ => (String::new(), String::new()),
        };
        eprintln!("key-trace: pressed {} {held:?} repeat={repeat} chord={chord:?} state={state} -> {} (keyboard: {focus})", keyboard::code_points(text), if used { "taken" } else { "passed on" });
    }
    used
}

/// A key released, for `CLARP_KEY_TRACE`.
pub fn key_released(text: &str, control: bool, alt: bool, shift: bool, meta: bool) {
    use crate::platform::keyboard;
    if keyboard::tracing() {
        eprintln!("key-trace: released {} {:?}", keyboard::code_points(text), keyboard::Modifiers { control, alt, shift, meta });
    }
}

fn run_shortcut(text: &str, control: bool, alt: bool, shift: bool, meta: bool, repeat: bool) -> bool {
    // Every key is someone at this window (headless too, where winit is not).
    crate::platform::desktop::note_input();
    let held = crate::platform::keyboard::Modifiers { control, alt, shift, meta };
    // A modifier whose release the window never saw: the key comes again without it.
    if crate::platform::keyboard::correct(text, held) {
        return true;
    }
    // On macOS Cmd and the Control key are both Ctrl here.
    let control = held.shortcut_control();
    let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return false };
    let Some(chord) = keymap::chord(text, control, alt, shift) else { return false };
    // Cmd+C, Cmd+V, Cmd+Q... on a Mac are the text field's and the system's.
    if crate::platform::keyboard::left_to_the_system(&chord, held) {
        return false;
    }
    let state = context(&app, &window);
    if state == "find" {
        if crate::find_view::key(&app, &window, &chord) {
            show_hints(&app, &window);
            return true;
        }
    }
    // The field's own editing keys (copy, paste, select all, undo) stay
    // the field's; past them it types as the composer does.
    if state == "find" && ["Ctrl+A", "Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+Y"].contains(&chord.as_str()) {
        return false;
    }
    let state = if state == "find" { "composer" } else { state };
    // ? again closes the key help, as Escape does.
    if chord == "?" && *app.overlay.borrow() == crate::help_view::OVERLAY {
        close_overlay(&app, &window);
        return true;
    }
    if state == "keymap" {
        let used = crate::keymap_view::key(&app, &window, text, &chord, !control && !alt);
        return used;
    }
    if state == "fonts" {
        let used = crate::font_view::key(&app, &window, &chord);
        show_hints(&app, &window);
        return used;
    }
    if state == "publications" {
        let used = crate::publications_view::key(&app, &window, &chord);
        show_hints(&app, &window);
        return used;
    }
    if state == "processes" {
        // The key that opened it closes it again.
        let opener = ["workspace", "updates"].iter().any(|s| keymap::action_for(s, &chord, &overrides(&app), facts(&app, &window)) == Some("agent-processes"));
        let used = if opener {
            close_overlay(&app, &window);
            true
        } else {
            crate::processes_view::key(&app, &window, &chord)
        };
        show_hints(&app, &window);
        return used;
    }
    // ---- vim mode: Normal mode's keys and the command line come first.
    if vim_on(&app) {
        if let Some(used) = crate::vim_view::key(&app, &window, state, text, &chord) {
            return used;
        }
    }
    // A double press runs on its second press; the first did its own thing.
    let window_ms = keymap::double_press_window(app.engine.borrow().settings().get("keymap/doublePressMs").and_then(serde_json::Value::as_i64));
    let second = PRESSES.with(|p| p.borrow_mut().press(&chord, std::time::Instant::now(), window_ms, repeat));
    if second && state != "hints" {
        if let Some(action) = keymap::double_action(state, &chord, &overrides(&app), facts(&app, &window)) {
            let ran = run(&app, &window, action);
            show_hints(&app, &window);
            return ran;
        }
    }
    // The search page's tabs: Ctrl+1/2/3, and Tab / Shift+Tab to step.
    if state == "modal" && app.switcher.borrow().open && app.switcher.borrow().messages_only && app.switcher.borrow().picker.is_empty() {
        let tab = app.switcher.borrow().tab;
        let next = match chord.as_str() {
            "Ctrl+1" => Some(0),
            "Ctrl+2" => Some(1),
            "Ctrl+3" => Some(2),
            "Tab" => Some((tab + 1) % 3),
            "Shift+Tab" => Some((tab + 2) % 3),
            _ => None,
        };
        if let Some(next) = next {
            switcher_tab(&app, &window, next);
            return true;
        }
    }
    // The switcher's own key for message search (Ctrl+F, as bound) turns it
    // into message search, with the words already typed: the key is not lost
    // to the text field, and the words are not typed twice.
    if state == "modal" && app.switcher.borrow().open && app.switcher.borrow().picker.is_empty() && !app.switcher.borrow().messages_only {
        if matches!(keymap::action_for("main", &chord, &overrides(&app), facts(&app, &window)), Some("search-messages" | "find-in-chat")) {
            let query = app.switcher.borrow().query.clone();
            run(&app, &window, "search-messages");
            if !query.is_empty() {
                app.switcher.borrow_mut().query = query.clone();
                window.invoke_set_switcher_query(query.into());
                refresh_switcher(&app, &window);
            }
            show_hints(&app, &window);
            return true;
        }
    }
    if state == "hints" {
        let action = keymap::action_for(state, &chord, &overrides(&app), facts(&app, &window));
        let used = crate::link_hints::key(&window, action, &chord);
        show_hints(&app, &window);
        return used;
    }
    let Some(action) = keymap::action_for(state, &chord, &overrides(&app), facts(&app, &window)) else { return false };
    // ---- profile and overview: in the composer Ctrl+Shift+O attaches a
    // file (Composer.qml's own shortcut), elsewhere it opens the overview.
    if action == "overview" && state == "composer" {
        return false;
    }
    // The digit is the answer: 1 is the card's first.
    if action == "artifact-choose" {
        let Some(id) = crate::artifacts_view::selected(&app) else { return false };
        let index = chord.parse::<i32>().map_or(-1, |n| n - 1);
        // The chosen answer's digit again sends it.
        let chosen = crate::artifacts_view::card_item(&app, &id).is_some_and(|c| c.pending && c.chosen == index && !c.editing);
        if chosen {
            crate::artifacts_view::send(&app, &id);
        } else {
            crate::artifacts_view::choose(&app, &id, index);
        }
        show_hints(&app, &window);
        return true;
    }
    // The digit is the profile's section (the cards' titles show which).
    if action == "profile-section" {
        crate::profile_view::key(&window, action, chord.parse().ok());
        show_hints(&app, &window);
        return true;
    }
    let ran = run(&app, &window, action);
    show_hints(&app, &window);
    ran
}

fn sidebar_sessions(window: &AppWindow) -> Vec<String> {
    window.get_chats().iter().map(|row| row.session.to_string()).collect()
}

/// Runs `action`; false for one this app does not do yet, so the key
/// reaches the focused control instead.
pub fn run(app: &Rc<App>, window: &AppWindow, action: &str) -> bool {
    // Settings → Confirm stop and release asks first.
    if crate::look::confirm_first(app, window, action) {
        return true;
    }
    // The text size or the window's scale changed by the user: the chats
    // resizing with it are theirs (unasked.rs).
    if action.starts_with("chat-zoom-") || action.starts_with("pref-set:") || matches!(action, "ui-larger" | "ui-smaller" | "ui-reset") {
        crate::scroll_journal::input_all();
    }
    // `:set`, the settings file, reset all, the chat zoom.
    if let Some(ran) = crate::look::run(app, window, action) {
        return ran;
    }
    // ---- launch dialogs
    if let Some(ran) = crate::launch_view::run(app, window, action).or_else(|| crate::agent_dialogs_view::run(app, window, action)) {
        return ran;
    }
    let selected = app.engine.borrow().selected_session().to_owned();
    let mut layout_changed = false;
    // In vim mode a layout changed from Normal mode stays in Normal mode.
    let typing = context(app, window) == "composer";
    match action {
        "shortcut-bar" => {
            let visible = !window.get_shortcuts_visible();
            window.set_shortcuts_visible(visible);
            app.engine.borrow_mut().settings_mut().set("appearance/shortcutsVisible", visible);
        }
        "switcher" => open_switcher(app, window),
        "link-hints" => {
            crate::link_hints::start(app, window);
        }
        // Ctrl+R: the recent agents, newest first; Enter on the first row
        // goes back to the chat before this one.
        "recent-agents" => {
            open_switcher(app, window);
            app.switcher.borrow_mut().recent_only = true;
            // i18n: Ctrl+R recent agents list, placeholder of the search field
            window.set_switcher_placeholder(tr!("Recent agents").into());
            // i18n: Ctrl+R recent agents list, shown when there is no other agent to go back to
            window.set_switcher_empty(tr!("No other agents yet").into());
            // i18n: Ctrl+R recent agents list, the key hint line at the bottom; key names stay, the words say what each key does (verbs)
            window.set_switcher_hint(tr!("↑↓ move · Enter open · Esc close").into());
            refresh_switcher(app, window);
        }
        "escape" if app.switcher.borrow().open && !app.switcher.borrow().picker.is_empty() => close_picker(app, window),
        "escape" if app.switcher.borrow().open => close_switcher(app, window, None),
        // The font picker: Escape puts the font back.
        "escape" if crate::font_view::is_open() => crate::font_view::act(app, window, "cancel"),
        "choose-font" => crate::font_view::open(app, window),
        "reset-font" => {
            let theme = app.engine.borrow().reading_theme();
            app.engine.borrow_mut().set_font_override(&theme, None);
        }
        // The find bar closes on Escape from the chat it shows in.
        "escape" if crate::find_view::shown().is_some_and(|(pane, _)| pane == app.active_id().as_str()) && context(app, window) == "pane" => crate::find_view::close(app, window),
        // The composer's mention list closes before the composer is left.
        "escape" if crate::mention_view::is_open(app) => crate::mention_view::dismiss_active(app),
        // V in the chat: the whole loaded chat as plain text to select from.
        "select-text" => crate::select_text::open_conversation(app, window),
        // W in the chat (#702): the work of the message the keyboard is
        // on, or of the latest turn, opens or folds (Conversation view).
        "show-work" => {
            if !crate::artifacts_view::show_work(app) {
                return false;
            }
        }
        // C in the chat (#702): the other view, as the chat's own choice;
        // nothing on a Host without the views.
        "chat-view" => {
            let session = app.active_session();
            if !app.engine.borrow_mut().toggle_chat_view(&session) {
                return false;
            }
        }
        // Ctrl+F in a chat: the find bar (message search where none is open).
        "find-in-chat" => {
            if window.get_surface() != "chats" || !crate::find_view::open(app, window) {
                return run(app, window, "search-messages");
            }
        }
        // Ctrl+Shift+F (Ctrl+F with no chat open): message search, in the
        // switcher, with the find bar's words when it has the keyboard.
        "search-messages" => {
            let words = if crate::find_view::focused(window) { window.global::<crate::FindBridge>().get_text().to_string() } else { String::new() };
            open_switcher(app, window);
            app.switcher.borrow_mut().messages_only = true;
            app.switcher.borrow_mut().tab = 0;
            window.set_switcher_placeholder(crate::search_view::placeholder().into());
            window.set_switcher_empty("".into());
            window.set_switcher_hint("".into());
            if !words.trim().is_empty() {
                app.switcher.borrow_mut().query = words.clone();
                window.invoke_set_switcher_query(words.into());
            }
            refresh_switcher(app, window);
        }
        // ---- profile and overview
        // The chat's "⋯" menu: Esc steps out of a submenu, then closes it.
        "escape" if !window.global::<crate::ChatMenu>().get_open().is_empty() => {
            let menu = window.global::<crate::ChatMenu>();
            if menu.get_submenu().is_empty() {
                crate::chat_menu::close(window);
            } else {
                menu.set_submenu(slint::SharedString::new());
            }
        }
        "escape" if crate::profile_view::owns(app) => crate::profile_view::escape(app, window),
        "transit-down" => crate::profile_view::transit_move(window, 1),
        "transit-up" => crate::profile_view::transit_move(window, -1),
        "transit-enter" => crate::profile_view::transit_enter(window),
        "profile-down" | "profile-up" | "profile-next-card" | "profile-previous-card" | "profile-enter" | "profile-left" | "profile-right" => {
            crate::profile_view::key(window, action, None);
        }
        "overview" => crate::overview_view::open(app, window),
        // The selected agent's background work, or every job on Updates.
        "agent-processes" => crate::processes_view::toggle(app, window),
        "orchestrator" => crate::orchestrator_view::open(app, window),
        "agent-profile" => crate::profile_view::open(app, window, &selected),
        // The profile's and overview's Relaunch: the Start dialog, replacing
        // the agent (Main.qml opens it the same way).
        "relaunch-agent" if !selected.is_empty() => {
            let name = app.engine.borrow().chat_name(&selected);
            crate::launch_view::open_start_agent(app, window, &selected, &name);
        }
        "manage-queue" if !selected.is_empty() => crate::agent_dialogs_view::open_queue(app, window, &selected),
        "send-queued" => {
            if !crate::queue_strip::send_first(app) {
                return false;
            }
        }
        // The value editor puts the old value back.
        "escape" if matches!(app.overlay.borrow().as_str(), crate::look::EDITOR | crate::look::CONFIRM) => crate::look::cancel_editor(app, window),
        "escape" if !app.overlay.borrow().is_empty() => close_overlay(app, window),
        "connection" => {
            window.set_saved_hosts(crate::saved_host_rows(&app.engine.borrow()));
            open_overlay(app, window, "connection");
            window.invoke_open_connection_page();
        }
        "edit-keymap" => crate::keymap_view::open(app, window),
        "help-keys" => crate::help_view::open(app, window),
        // Escape first dismisses the error banner over the chats.
        "escape" | "dismiss-error" if action == "dismiss-error" || (window.get_surface() == "chats" && !window.get_error().is_empty()) => {
            app.engine.borrow_mut().dismiss_error();
        }
        // Then the layout-conflict bar (another window saved a newer layout).
        "escape" | "dismiss-layout-warning"
            if action == "dismiss-layout-warning" || (window.get_surface() == "chats" && !window.get_save_warning().is_empty()) =>
        {
            app.engine.borrow_mut().with_panes(|p| p.dismiss_workspace_save_warning());
        }
        "keep-layout" => app.engine.borrow_mut().with_panes(|p| p.save_workspace_layout_instead()),
        // Then it leaves the card the keyboard is on (in the chat).
        "escape" if window.get_surface() == "chats" && context(app, window) == "pane" && !app.artifact_cursor.borrow().is_empty() => {
            crate::artifacts_view::leave(app);
            app.focus_transcript();
        }
        // Escape on another surface goes back to the chats.
        "escape" if window.get_surface() != "chats" => {
            window.set_surface("chats".into());
            app.focus_composer();
        }
        "escape" => {
            let state = app.engine.borrow().roster().find(&selected).map(|a| a.latest_state.clone()).unwrap_or_default();
            let context = context(app, window);
            // One thing per press: in the composer Escape only leaves it; a
            // working agent stops from the conversation (or Ctrl+.). Not in
            // vim mode, where Escape is pressed out of habit (Space x stops).
            if context != "composer" && !vim_on(app) && matches!(state.as_str(), "thinking" | "tool" | "compacting") {
                app.stop_session(&selected);
            } else if context == "search" {
                focus_sidebar(app, window);
            } else {
                // From the list or the composer, the conversation keeps the
                // keyboard: arrows and Page keys scroll it.
                app.focus_transcript();
            }
        }
        "focus-sidebar" => focus_sidebar(app, window),
        "artifact-next" => crate::artifacts_view::step(app, 1),
        "artifact-previous" => crate::artifacts_view::step(app, -1),
        "artifact-discard" => match crate::artifacts_view::selected(app) {
            Some(id) => crate::artifacts_view::discard(app, &id),
            None => return false,
        },
        "open-in-browser" => return crate::artifacts_view::open_in_browser_here(app, window),
        "artifact-open" => match crate::artifacts_view::selected(app) {
            Some(id) => crate::artifacts_view::activate(app, window, &id),
            None => return false,
        },
        "artifact-back" | "artifact-forward" => {
            if !crate::artifacts_view::nudge(app, window, if action == "artifact-back" { -1 } else { 1 }) {
                return false;
            }
        }
        "artifact-stop" => {
            if !crate::artifacts_view::stop(app) {
                return false;
            }
        }
        "focus-pane" => app.focus_transcript(),
        "focus-composer" => app.focus_composer(),
        "toggle-focus" => {
            if window.get_sidebar_focused() {
                app.focus_transcript();
            } else {
                focus_sidebar(app, window);
            }
        }
        // The next chat in the explorer's order, from anywhere; the
        // keyboard stays where it was.
        "next-agent" | "previous-agent" => {
            let sessions = sidebar_sessions(window);
            let Some(at) = sessions.iter().position(|s| *s == selected).or_else(|| sessions.len().checked_sub(1)) else { return false };
            let step = if action == "next-agent" { 1 } else { sessions.len() - 1 };
            let next = sessions[(at + step) % sessions.len()].clone();
            let (composer, sidebar) = (app.active_report().composer_focused, window.get_sidebar_focused());
            app.engine.borrow_mut().select(&next);
            pump_now(app);
            if sidebar {
                window.set_sidebar_cursor(next.as_str().into());
            } else if composer {
                app.focus_composer();
            } else {
                app.focus_transcript();
            }
        }
        "agent-next" | "agent-previous" => {
            let sessions = sidebar_sessions(window);
            let cursor = window.get_sidebar_cursor().to_string();
            let at = sessions.iter().position(|s| *s == cursor);
            let next = match (at, action == "agent-next") {
                (None, _) => sessions.first(),
                (Some(i), true) => sessions.get((i + 1).min(sessions.len().saturating_sub(1))),
                (Some(i), false) => sessions.get(i.saturating_sub(1)),
            };
            if let Some(next) = next {
                window.set_sidebar_cursor(next.as_str().into());
                // Live preview: the chat under the cursor opens in the active
                // pane while the keyboard stays in the explorer.
                let preview = app.engine.borrow().settings().boolean("explorer/livePreview", false);
                if preview && !next.starts_with("pair:") && app.engine.borrow().selected_session() != next.as_str() {
                    app.previewing.set(true);
                    app.engine.borrow_mut().select(next);
                    pump_now(app);
                    app.previewing.set(false);
                    window.set_sidebar_cursor(next.as_str().into());
                }
            }
        }
        "toggle-compact" => crate::settings_view::change(app, window, "compact-explorer", 1),
        "toggle-preview" => {
            let on = app.engine.borrow().settings().boolean("explorer/livePreview", false);
            app.engine.borrow_mut().settings_mut().set("explorer/livePreview", !on);
        }
        "agent-open" => {
            let cursor = window.get_sidebar_cursor().to_string();
            if !cursor.is_empty() {
                app.engine.borrow_mut().select(&cursor);
                pump_now(app);
                app.focus_composer();
            }
        }
        "agent-search" => window.invoke_focus_search(),
        // Sub-agents under the cursor's chat; folding from a sub-agent
        // moves the cursor up to its chat and folds that.
        "fold" | "unfold" => {
            let cursor = window.get_sidebar_cursor().to_string();
            let parent = {
                let engine = app.engine.borrow();
                engine
                    .roster()
                    .find(&cursor)
                    .filter(|a| a.is_helpers_chat())
                    .and_then(|a| engine.roster().find_by_agent_id(&a.parent_agent_id))
                    .map(|p| p.session.clone())
            };
            let own = window.get_chats().iter().any(|c| c.session == cursor.as_str() && c.fold_count > 0);
            if action == "unfold" {
                app.fold(&cursor, Some(true));
            } else if own && app.unfolded.borrow().contains(&cursor) {
                app.fold(&cursor, Some(false));
            } else if let Some(parent) = parent {
                window.set_sidebar_cursor(parent.as_str().into());
                app.fold(&parent, Some(false));
            }
        }
        // The sidebar's views (minimal UI hides their chips and switches).
        "list-all" | "list-unread" | "list-working" | "list-needs-you" | "list-done" | "list-snoozed" => {
            window.invoke_show_pairs(false);
            window.invoke_show_archive(false);
            window.invoke_choose_scope(action.trim_start_matches("list-").into());
            window.set_sidebar_visible(true);
        }
        // Snooze: the explorer's row under the keyboard, else the open chat.
        "snooze-chat" | "snooze-chat-hour" | "snooze-chat-morning" | "wake-chat" | "snooze-toggle" => {
            let target = if window.get_sidebar_focused() && !window.get_sidebar_cursor().is_empty() { window.get_sidebar_cursor().to_string() } else { selected.clone() };
            if target.is_empty() || target.starts_with("pair:") {
                return false;
            }
            let how = match action {
                "snooze-chat-hour" => "hour",
                "snooze-chat-morning" => "morning",
                "wake-chat" => "wake",
                "snooze-toggle" if crate::triage::is_snoozed(&app.engine.borrow(), &target) => "wake",
                _ => "message",
            };
            crate::triage::set(app, &target, how);
        }
        "list-rooms" | "list-archive" => {
            if action == "list-rooms" { window.invoke_show_pairs(true) } else { window.invoke_show_archive(true) }
            window.set_sidebar_visible(true);
            focus_sidebar(app, window);
        }
        "sidebar" => {
            // Shown, the explorer takes the keyboard (on the open chat);
            // hidden, the keyboard goes back to the chat.
            let was_in_sidebar = window.get_sidebar_focused() || window.get_search_focused();
            if window.get_sidebar_visible() {
                window.set_sidebar_visible(false);
                if was_in_sidebar {
                    app.focus_transcript();
                }
            } else {
                focus_sidebar(app, window);
            }
        }
        "split-right" | "split-down" => {
            let direction = if action == "split-right" { "vertical" } else { "horizontal" };
            app.engine.borrow_mut().with_panes(|p| p.split_active(direction, &selected));
            layout_changed = true;
        }
        "close-pane" => {
            let active = app.engine.borrow().panes().active_pane_id().to_owned();
            app.engine.borrow_mut().with_panes(|p| p.close_pane(&active));
            layout_changed = true;
        }
        "zoom" => {
            app.engine.borrow_mut().with_panes(|p| p.toggle_zoom());
            layout_changed = true;
        }
        "balance" => app.engine.borrow_mut().with_panes(|p| p.equalize()),
        "next-workspace" | "previous-workspace" => {
            let (workspaces, active) = {
                let engine = app.engine.borrow();
                (engine.panes().workspaces(), engine.panes().active_workspace().to_owned())
            };
            let ids: Vec<String> = workspaces.iter().map(|w| clarp_core::json::string(w, "id")).collect();
            if ids.len() > 1 {
                let index = ids.iter().position(|id| *id == active).unwrap_or(0);
                let step = if action == "next-workspace" { 1 } else { ids.len() - 1 };
                let next = ids[(index + step) % ids.len()].clone();
                app.engine.borrow_mut().with_panes(|p| p.switch_workspace(&next));
                layout_changed = true;
            }
        }
        "new-workspace" => {
            new_workspace(app, window, "");
            return true;
        }
        "close-workspace" => {
            let active = app.engine.borrow().panes().active_workspace().to_owned();
            if app.engine.borrow().panes().workspaces().len() < 2 {
                return false;
            }
            app.engine.borrow_mut().with_panes(|p| p.close_workspace(&active));
            layout_changed = true;
        }
        _ if action.starts_with("move-") => {
            let direction = action.trim_start_matches("move-").to_owned();
            app.engine.borrow_mut().with_panes(|p| p.navigate(&direction));
            layout_changed = true;
        }
        "jump-latest" => app.to_latest(),
        "stop-agent" => app.stop_session(&selected),
        "silence" => {
            crate::platform::audio::with(crate::platform::audio::Audio::silence);
        }
        "preview-versions" => {
            if !crate::preview_view::enabled() {
                return false;
            }
            crate::preview_view::open(app, window);
        }
        "update-preview" => return crate::preview_view::update_or_open(app, window),
        "toggle-queue" => {
            let on = crate::queue_strip::shown(&app.engine.borrow());
            crate::queue_strip::set_shown(app, !on);
        }
        "toggle-explanations" => {
            if !app.engine.borrow().tool_explanation_setting() {
                return false;
            }
            toggle_explanations(app);
        }
        "tool-narration" => {
            let enabled = app.engine.borrow().narrator_enabled();
            app.engine.borrow_mut().set_narrator_enabled(!enabled);
        }
        "talk" => {
            let agent = !selected.is_empty() && !selected.starts_with("pair:");
            let recording = crate::platform::audio::with(|audio| audio.recording()).unwrap_or(false);
            if agent || recording {
                crate::platform::audio::with(|audio| audio.toggle_recording_for_session(&selected));
            }
        }
        "mute" => {
            let muted = app.engine.borrow().muted();
            app.engine.borrow_mut().set_muted(!muted);
        }
        // ---- updates and teams
        "updates" => crate::updates_view::open(app, window),
        "teams" => crate::teams_view::open(app, window),
        "next-attention" => crate::updates_view::next_attention(app, window),
        "refresh" if window.get_surface() == "updates" => app.engine.borrow_mut().load_updates(),
        "refresh" => app.engine.borrow_mut().refresh_session(&selected),
        "refresh-agents" => app.engine.borrow_mut().refresh_agents(),
        "tools" => {
            let mode = app.engine.borrow().activity_mode();
            let always = clarp_core::presentation::ALWAYS_VISIBLE;
            app.engine.borrow_mut().set_activity_mode(if mode == always { 0 } else { always });
        }
        _ if action.starts_with("setting:detail:") => {
            if let Ok(level) = action.trim_start_matches("setting:detail:").parse::<i32>() {
                app.engine.borrow_mut().set_narrator_detail_level(level);
            }
        }
        _ if action.starts_with("settingpick:") => {
            if let Some((id, value)) = action.trim_start_matches("settingpick:").split_once(':') {
                crate::settings_view::pick(app, window, id, value);
            }
        }
        // ---- the settings page's search
        "settings-search" if window.get_surface() == "settings" => window.invoke_focus_settings_search(),
        "settings-reset" if window.get_surface() == "settings" => {
            let current = window.get_setting_rows().row_data(window.get_setting_current().max(0) as usize).map(|r| r.id.to_string()).unwrap_or_default();
            if !current.is_empty() {
                crate::settings_view::reset(app, window, &current, false);
            }
        }
        "settings-results" => crate::settings_view::to_results(app, window),
        "settings-search-cancel" => crate::settings_view::cancel_search(app, window),
        _ if action.starts_with("settingrow:") => {
            let rest = action.trim_start_matches("settingrow:");
            if let Some((id, delta)) = rest.rsplit_once(':') {
                crate::settings_view::change(app, window, id, delta.parse().unwrap_or(1));
            }
        }
        _ if action.starts_with("setting:") => apply_setting(app, window, action),
        "settings" => crate::settings_view::open(app, window),
        // Settings → Interface scale, a step at a time.
        "ui-larger" | "ui-smaller" | "ui-reset" => {
            match action {
                "ui-larger" => crate::look::step_setting(app, window, "ui-scale", 1),
                "ui-smaller" => crate::look::step_setting(app, window, "ui-scale", -1),
                _ => crate::catalogue::setting("ui-scale").is_some_and(|scale| scale.reset(app, window)),
            };
        }
        "chats" => {
            window.set_surface("chats".into());
            app.focus_composer();
        }
        _ => return false,
    }
    pump_now(app);
    if layout_changed {
        app.refresh(&[Change::Panes]);
        if vim_on(app) && !typing { app.focus_transcript() } else { app.focus_composer() }
    }
    true
}

/// A new tab (workspace) named `name` ("Workspace N" when empty), opened.
pub fn new_workspace(app: &Rc<App>, window: &AppWindow, name: &str) {
    let typing = context(app, window) == "composer";
    let name = if name.trim().is_empty() {
        // i18n: the name a new workspace tab gets when the user gives none (noun); {} is its number
        tr!("Workspace {}", app.engine.borrow().panes().workspaces().len() + 1)
    } else {
        name.trim().to_owned()
    };
    app.engine.borrow_mut().with_panes(|p| p.create_workspace(&name));
    pump_now(app);
    app.refresh(&[Change::Panes]);
    if vim_on(app) && !typing { app.focus_transcript() } else { app.focus_composer() }
}

fn focus_sidebar(app: &App, window: &AppWindow) {
    window.set_sidebar_visible(true);
    let selected = app.engine.borrow().selected_session().to_owned();
    window.set_sidebar_cursor(selected.into());
    window.invoke_focus_sidebar();
}

// ---- the quick switcher ------------------------------------------------------

fn toggles(app: &App) -> switcher::Toggles {
    switcher::Toggles { preview_versions: crate::preview_view::enabled(), queue_shown: crate::queue_strip::shown(&app.engine.borrow()) }
}

/// Ctrl+Shift+X, its command and its setting: the Host's tool explanations
/// on or off; the transcripts present again.
pub fn toggle_explanations(app: &App) {
    let on = {
        let engine = app.engine.borrow();
        engine.tool_explanations_enabled(engine.selected_session())
    };
    app.engine.borrow_mut().set_tool_explanations_enabled(!on);
    if let Some(app) = crate::app() {
        crate::pump_now(&app);
    }
}

pub fn open_switcher(app: &App, window: &AppWindow) {
    {
        let mut state = app.switcher.borrow_mut();
        state.open = true;
        state.query.clear();
        state.selected.clear();
        state.restore_composer = app.active_report().composer_focused;
        state.contacts_only = false;
        state.recent_only = false;
        state.picker.clear();
        state.messages_only = false;
        state.tab = 0;
    }
    crate::search_view::forget_asked();
    window.set_switcher_tabs(ModelRc::default());
    window.set_switcher_note("".into());
    // i18n: Ctrl+K command palette, placeholder of the search field: what it finds
    window.set_switcher_placeholder(tr!("Agent, contact, setting or command").into());
    // i18n: Ctrl+K command palette, shown when nothing matches the search
    window.set_switcher_empty(tr!("No matching agent or contact").into());
    window.set_switcher_hint(
        // i18n: Ctrl+K command palette, the key hint line at the bottom; key names stay, the words say what each key does
        tr!("↑↓ move · Enter runs a command, switches a setting or lists its choices · Esc close").into(),
    );
    window.set_switcher_open(true);
    window.invoke_show_switcher();
    refresh_switcher(app, window);
}

/// Enter on a setting with choices: the switcher lists its options, the
/// current one marked; Escape goes back to everything.
pub fn open_picker(app: &App, window: &AppWindow, id: &str) {
    let label = crate::catalogue::setting(id).map_or_else(|| crate::catalogue::find(id).map_or_else(|| id.to_owned(), |e| e.label_text()), |s| s.label(app));
    {
        let mut state = app.switcher.borrow_mut();
        state.open = true;
        state.query.clear();
        state.selected.clear();
        state.picker = id.to_owned();
    }
    // i18n: Ctrl+K, placeholder while a setting's options are listed; {} is the setting's name
    window.set_switcher_placeholder(tr!("{}: choose one", label).into());
    // i18n: Ctrl+K, shown when none of a setting's options matches the search
    window.set_switcher_empty(tr!("No matching choice").into());
    // i18n: Ctrl+K, the key hint line while a setting's options are listed; key names stay, the words say what each key does
    window.set_switcher_hint(tr!("↑↓ move · Enter chooses · Esc back to everything").into());
    window.set_switcher_open(true);
    window.invoke_show_switcher();
    refresh_switcher(app, window);
    // The current option is where the keyboard starts.
    // The pill's word, in English or (once the switcher translates it) the app's language.
    // i18n: quick switcher, value pill on a row: this is the one in use now (adjective); the same word as the pill drawn by switcher.slint
    let current_word = tr!("Current");
    let current = app.switcher.borrow().items.iter().position(|i| i.value == "Current" || i.value == current_word.as_str());
    if let Some(index) = current {
        switcher_moved(app, window, index as i32);
    }
}

/// Escape in a picker: back to the whole list.
fn close_picker(app: &App, window: &AppWindow) {
    app.switcher.borrow_mut().picker.clear();
    // i18n: Ctrl+K command palette, placeholder of the search field: what it finds
    window.set_switcher_placeholder(tr!("Agent, contact, setting or command").into());
    // i18n: Ctrl+K command palette, shown when nothing matches the search
    window.set_switcher_empty(tr!("No matching agent or contact").into());
    app.switcher.borrow_mut().query.clear();
    window.set_switcher_hint(
        // i18n: Ctrl+K command palette, the key hint line at the bottom; key names stay, the words say what each key does
        tr!("↑↓ move · Enter runs a command, switches a setting or lists its choices · Esc close").into(),
    );
    window.invoke_show_switcher();
    refresh_switcher(app, window);
}

/// The explorer's and the switcher's note while the agent list is stale,
/// and why (both empty while it is current).
pub fn roster_note(engine: &clarp_engine::Engine) -> (String, String) {
    let state = engine.roster_freshness();
    let (note, detail) = (state.note(clarp_engine::roster_freshness::local_clock), state.detail());
    // Paired Hosts that cannot be reached: their rows stay from cache.
    let unreachable = engine.unreachable_hosts_note();
    match (note.is_empty(), unreachable.is_empty()) {
        (_, true) => (note, detail),
        // i18n: explorer and Ctrl+K, the detail under the note that a paired Host cannot be reached: its chats are shown from the last read, and the app keeps trying
        (true, false) => (unreachable, tr!("Its chats show as last read; the desktop keeps trying.")),
        (false, false) => (format!("{note} · {unreachable}"), detail),
    }
}

/// Rebuilds the results (a query typed, or the roster changed while open).
pub fn refresh_switcher(app: &App, window: &AppWindow) {
    if !app.switcher.borrow().open {
        return;
    }
    let toggles = toggles(app);
    let (query, contacts_only, picker) = {
        let state = app.switcher.borrow();
        (state.query.clone(), state.contacts_only, state.picker.clone())
    };
    let settings: Vec<switcher::SettingRow> = crate::settings_view::rows(app)
        .into_iter()
        .map(|r| (r.kind.to_string(), r.id.to_string(), r.label.to_string(), r.detail.to_string(), r.on))
        .collect();
    let (recent_only, messages_only) = (app.switcher.borrow().recent_only, app.switcher.borrow().messages_only);
    let mut note = String::new();
    let items = if !picker.is_empty() {
        switcher::picker(&picker, &crate::settings_view::choices(app, &picker), &query)
    } else if messages_only {
        // Ctrl+F: messages, and what was searched. A Host that searches
        // is asked once the typing pauses.
        crate::search_view::ask(app, &query);
        let found = crate::search_view::found(&mut app.engine.borrow_mut(), &query);
        note = found.note;
        window.set_switcher_empty(found.empty.into());
        // Its tabs: chats whose name matches, and the messages.
        let chats = if query.trim().is_empty() { Vec::new() } else { switcher::agents(&app.engine.borrow(), &query) };
        let tab = app.switcher.borrow().tab;
        let (items, counts) = crate::search_view::tabbed(tab, chats, found.items);
        window.set_switcher_tabs_more(counts.more);
        let language = clarp_core::i18n::current();
        let counts: Vec<slint::SharedString> = [counts.all, counts.chats, counts.messages].into_iter().map(|n| clarp_core::i18n::format::number(language, n as i64).into()).collect();
        window.set_switcher_tabs(ModelRc::new(VecModel::from(counts)));
        window.set_switcher_tab(tab as i32);
        items
    } else if recent_only {
        switcher::recent(&app.engine.borrow(), &app.recent.borrow(), &query)
    } else {
        // Ctrl+K lists agents, contacts, commands and settings, never
        // messages: those are Ctrl+F's.
        if !contacts_only && query.trim().chars().count() >= crate::search_view::HINT_FROM {
            // i18n: command palette (Ctrl+K), note under the results pointing to message search; "Ctrl+Shift+F" is a key name
            note = clarp_core::tr!("Ctrl+Shift+F searches the messages of every chat.");
        }
        switcher::results(&app.engine.borrow(), &query, toggles, contacts_only, switcher::settings(&settings))
    };
    // Agents and contacts come from the roster: say when it is stale.
    if picker.is_empty() && !messages_only {
        let (stale, _) = roster_note(&app.engine.borrow());
        if !stale.is_empty() {
            note = if note.is_empty() { stale } else { format!("{stale}. {note}") };
        }
    }
    window.set_switcher_note(note.into());
    // The commands' keys as the user bound them.
    let overrides = overrides(app);
    let items: Vec<switcher::Item> = items
        .into_iter()
        .map(|mut item| {
            if item.kind == switcher::Kind::Command && overrides.contains_key(&item.target) {
                item.key = ["pane", "main"].iter().find_map(|s| keymap::shown(s, &item.target, &overrides)).unwrap_or_default();
            }
            // A setting shows the keys of the actions that change it.
            if item.target.starts_with("settingrow:") || item.target.starts_with("settingpicker:") {
                let actions = crate::catalogue::find(item.entry).map_or(&[][..], |e| e.actions);
                item.key = actions.iter().find_map(|a| ["pane", "sidebar"].iter().find_map(|s| keymap::shown(s, a, &overrides))).unwrap_or_default();
            }
            item
        })
        .collect();
    let mut state = app.switcher.borrow_mut();
    let current = switcher::keep_selection(&items, &state.selected);
    state.selected = usize::try_from(current).ok().and_then(|i| items.get(i)).map(switcher::Item::key_of).unwrap_or_default();
    let rows: Vec<SwitcherRow> = items
        .iter()
        .map(|item| SwitcherRow {
            kind: match item.kind {
                switcher::Kind::Agent => "agent".into(),
                switcher::Kind::Contact => "contact".into(),
                switcher::Kind::Command => "command".into(),
                switcher::Kind::Message => "message".into(),
                switcher::Kind::Host => "host".into(),
            },
            snippet: if item.kind == switcher::Kind::Message { crate::view::styled(&item.detail, false) } else { slint::StyledText::default() },
            label: item.label.clone().into(),
            // Under the label: an agent's state, else what it does.
            detail: if item.detail.is_empty() { item.description.clone() } else { item.detail.clone() }.into(),
            key: item.key.clone().into(),
            group: item.group.into(),
            value: item.value.clone().into(),
            // A setting with choices (a number's steps too): + and - step it.
            adjustable: item.target.starts_with("settingpicker:"),
        })
        .collect();
    state.items = items;
    drop(state);
    window.set_switcher_rows(ModelRc::new(VecModel::from(rows)));
    window.set_switcher_current(current);
}

/// The search page shows tab `tab` (0 All, 1 Chats, 2 Messages).
pub fn switcher_tab(app: &App, window: &AppWindow, tab: usize) {
    if !app.switcher.borrow().messages_only {
        return;
    }
    app.switcher.borrow_mut().tab = tab.min(crate::search_view::TABS - 1);
    app.switcher.borrow_mut().selected.clear();
    refresh_switcher(app, window);
}

pub fn switcher_moved(app: &App, window: &AppWindow, index: i32) {
    let mut state = app.switcher.borrow_mut();
    if let Some(item) = usize::try_from(index).ok().and_then(|i| state.items.get(i)) {
        state.selected = item.key_of();
        drop(state);
        window.set_switcher_current(index);
    }
}

/// Closes the switcher; the composer gets the keyboard back when it had it
/// (or `restore` says so).
pub fn close_switcher(app: &App, window: &AppWindow, restore: Option<bool>) {
    let restore = {
        let mut state = app.switcher.borrow_mut();
        state.open = false;
        restore.unwrap_or(state.restore_composer)
    };
    window.set_switcher_open(false);
    // Over Updates, Teams or Settings the keyboard goes back to that page:
    // the chats' composer is hidden there, and a key for it goes nowhere.
    if !surface_takes_keyboard(window) {
        if restore {
            app.focus_composer();
        } else {
            app.focus_transcript();
        }
    }
    show_hints(app, window);
}

/// Gives the keyboard to the page showing in place of the chats (Updates,
/// Teams, Settings); false while the chats show.
fn surface_takes_keyboard(window: &AppWindow) -> bool {
    match window.get_surface().as_str() {
        "updates" => window.invoke_focus_updates(),
        "teams" => window.invoke_focus_teams(),
        "settings" => window.invoke_focus_settings(),
        _ => return false,
    }
    true
}

/// + or - on a setting in the switcher: it steps, and the switcher stays
/// open on it with its new value.
pub fn switcher_adjusted(app: &Rc<App>, window: &AppWindow, index: i32, delta: i32) {
    let Some(item) = usize::try_from(index).ok().and_then(|i| app.switcher.borrow().items.get(i).cloned()) else { return };
    let Some(id) = item.target.strip_prefix("settingpicker:") else { return };
    // A number steps from where it is; a choice to its next option.
    crate::settings_view::change(app, window, id, delta);
    refresh_switcher(app, window);
}

/// Enter on a row (QuickSwitcher.qml `choose`).
pub fn switcher_chosen(app: &Rc<App>, window: &AppWindow, index: i32) {
    let Some(item) = usize::try_from(index).ok().and_then(|i| app.switcher.borrow().items.get(i).cloned()) else { return };
    let mut restore = app.switcher.borrow().restore_composer;
    // Closed without moving the keyboard: the choice decides where it goes.
    app.switcher.borrow_mut().open = false;
    window.set_switcher_open(false);
    match item.kind {
        switcher::Kind::Agent => {
            app.engine.borrow_mut().select(&item.target);
            pump_now(app);
            restore = true;
        }
        // ---- launch dialogs: a contact row starts that contact.
        switcher::Kind::Contact => {
            window.set_surface("chats".into());
            app.engine.borrow_mut().quick_start_contact(&item.target, "", "", "");
            pump_now(app);
            restore = true;
        }
        // Message search stays open on its words while older messages load.
        switcher::Kind::Command if item.target == crate::search_view::LOAD_OLDER => {
            app.switcher.borrow_mut().open = true;
            window.set_switcher_open(true);
            crate::search_view::load_older(app);
            refresh_switcher(app, window);
            return;
        }
        // All's "See all chats": the Chats tab.
        switcher::Kind::Command if item.target == crate::search_view::SEE_ALL_CHATS => {
            app.switcher.borrow_mut().open = true;
            window.set_switcher_open(true);
            switcher_tab(app, window, 1);
            return;
        }
        // So does "Load more": the Hosts' next page comes in under it.
        switcher::Kind::Command if item.target == crate::search_view::LOAD_MORE => {
            app.switcher.borrow_mut().open = true;
            window.set_switcher_open(true);
            crate::search_view::load_more(app);
            refresh_switcher(app, window);
            return;
        }
        // The picker keeps the keyboard in its search.
        switcher::Kind::Command if item.target == "choose-font" => {
            crate::font_view::open(app, window);
            return;
        }
        switcher::Kind::Command if item.target.starts_with("settingpicker:") => {
            open_picker(app, window, item.target.trim_start_matches("settingpicker:"));
            return;
        }
        // A saved Host: connect to it with its device token from the keyring
        // (or the local token for a Host on this machine).
        switcher::Kind::Host => {
            app.engine.borrow_mut().connect_to_server(&item.target, "");
            pump_now(app);
            restore = false;
        }
        // A message: its chat, scrolled to it, the transcript keeping the
        // keyboard to read around it.
        // The find bar shows there with the words that found it.
        switcher::Kind::Message => {
            let query = app.switcher.borrow().query.clone();
            crate::search_view::jump(app, &item.target);
            if let Some((session, id)) = crate::search_view::split(&item.target) {
                crate::find_view::open_at(app, window, &query, session, id);
            }
            restore = false;
        }
        // The panel keeps the keyboard.
        switcher::Kind::Command if item.target == "agent-processes" => {
            run(app, window, &item.target);
            crate::processes_view::set_origin(restore);
            return;
        }
        // The profile keeps the keyboard; closing it gives the keyboard
        // back where it was before the switcher.
        switcher::Kind::Command if item.target == "agent-profile" => {
            run(app, window, &item.target);
            crate::profile_view::set_origin(if restore { "composer" } else { "pane" });
            show_hints(app, window);
            return;
        }
        switcher::Kind::Command if item.target == "new-contact" => {
            crate::launch_view::open_contacts(app, window, Some(restore));
            return;
        }
        switcher::Kind::Command => {
            let leaves = ["quick-new-agent", "rename-agent", "new", "overview", "connection", "orchestrator", "updates", "teams", "settings"];
            if leaves.contains(&item.target.as_str()) {
                restore = false;
            }
            if !run(app, window, &item.target) {
                eprintln!("clarp-slint: {} is not available yet", item.target);
            }
        }
    }
    if restore {
        app.focus_composer();
    } else if !window.get_switcher_open() {
        app.focus_transcript();
    }
    show_hints(app, window);
}

fn apply_setting(app: &App, window: &AppWindow, action: &str) {
    let setting = action.trim_start_matches("setting:");
    if let Some(mode) = setting.strip_prefix("activity:").and_then(|m| m.parse::<i32>().ok()) {
        app.engine.borrow_mut().set_activity_mode(mode);
    } else if let Some(theme) = setting.strip_prefix("reading:") {
        app.engine.borrow_mut().set_reading_theme(theme);
    } else {
        match setting {
            "showWhenReady" => {
                let value = app.engine.borrow().show_when_ready();
                app.engine.borrow_mut().set_show_when_ready(!value);
            }
            "sharedFilesystem" => {
                let value = app.engine.borrow().shared_filesystem();
                app.engine.borrow_mut().set_shared_filesystem(!value);
            }
            "timestampsVisible" | "workspaceBarVisible" => {
                let (key, value) = {
                    let mut prefs = app.prefs.borrow_mut();
                    if setting == "timestampsVisible" {
                        prefs.timestamps = !prefs.timestamps;
                        ("conversation/timestampsVisible", prefs.timestamps)
                    } else {
                        prefs.workspace_bar = !prefs.workspace_bar;
                        ("appearance/workspaceBar", prefs.workspace_bar)
                    }
                };
                app.engine.borrow_mut().settings_mut().set(key, value);
                app.rebuild_transcripts();
                app.refresh(&[Change::Panes, Change::Preferences]);
            }
            other => eprintln!("clarp-slint: unknown setting {other}"),
        }
    }
    let _ = window;
}

// ---- dialogs -----------------------------------------------------------------

pub fn open_overlay(app: &App, window: &AppWindow, name: &str) {
    let previous = std::mem::replace(&mut *app.overlay.borrow_mut(), name.to_owned());
    if previous == "report" && name != "report" {
        app.engine.borrow_mut().set_artifact_in_view(None);
    }
    window.set_overlay(name.into());
    show_hints(app, window);
}

/// Closes the dialog; the surface under it (the active pane's composer on
/// the chats) gets the keyboard back.
pub fn close_overlay(app: &App, window: &AppWindow) {
    let closed = std::mem::take(&mut *app.overlay.borrow_mut());
    window.set_overlay("".into());
    if closed == "report" {
        app.engine.borrow_mut().set_artifact_in_view(None);
    }
    // A viewer opened from an agent's publications goes back to them.
    if crate::publications_view::give_back(app, window, &closed) {
        show_hints(app, window);
        return;
    }
    if closed == crate::help_view::OVERLAY && crate::help_view::give_back(app, window) {
        show_hints(app, window);
        return;
    }
    if closed == crate::processes_view::OVERLAY && crate::processes_view::give_back(app, window) {
        show_hints(app, window);
        return;
    }
    // The profile gives the keyboard back where it was when it opened.
    if closed == "profile" && crate::profile_view::give_back(app, window) {
        show_hints(app, window);
        return;
    }
    // A viewer opened from a card gives the keyboard back to the chat, on
    // that card.
    if crate::artifacts_view::take_return() && window.get_surface() == "chats" {
        app.focus_transcript();
        show_hints(app, window);
        return;
    }
    // ---- updates and teams
    if !surface_takes_keyboard(window) {
        app.focus_composer();
    }
    show_hints(app, window);
}

pub fn save_overrides(app: &App, overrides: &keymap::Overrides) {
    app.engine.borrow_mut().settings_mut().set("keymap/bindings", keymap::to_settings(overrides));
}

pub fn keymap_import(app: &App, window: &AppWindow, text: &str) {
    match keymap::import(text) {
        Ok(next) => {
            save_overrides(app, &next);
            window.set_keymap_error("".into());
            window.set_keymap_profile(keymap::export(&next).into());
            crate::keymap_view::imported(app, window);
        }
        Err(error) => window.set_keymap_error(error.into()),
    }
}

/// The keys spelled outside the shortcut bar (the banners'), as the user
/// bound them.
pub fn refresh_keys(app: &App, window: &AppWindow) {
    let overrides = overrides(app);
    let shown = |actions: &[&str]| actions.iter().find_map(|a| keymap::shown("pane", a, &overrides)).unwrap_or_default();
    window.set_error_dismiss_key(shown(&["dismiss-error", "escape"]).into());
    window.set_save_warning_dismiss_key(shown(&["dismiss-layout-warning", "escape"]).into());
    window.set_save_warning_keep_key(shown(&["keep-layout"]).into());
    // The composer's Stop and jump-to-latest buttons name their keys.
    let keys = window.global::<crate::ComposerKeys>();
    keys.set_stop(shown(&["stop-agent"]).into());
    keys.set_latest(shown(&["jump-latest"]).into());
    keys.set_send_now(shown(&["send-queued"]).into());
}

pub fn keymap_export(app: &App, window: &AppWindow) {
    window.set_keymap_profile(keymap::export(&overrides(app)).into());
}
