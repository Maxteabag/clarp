//! What the keyboard map's actions do (Main.qml `runCommand`), and where
//! the keyboard is, which picks the map's state.

use std::rc::Rc;

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
    if app.switcher.borrow().open || !app.overlay.borrow().is_empty() {
        // ---- launch dialogs: the hub has its own keyboard state.
        if !app.switcher.borrow().open && *app.overlay.borrow() == crate::launch_view::HUB {
            return "launch";
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
    } else if app.active_report().composer_focused || window.global::<crate::ArtifactBridge>().get_editing() {
        // A card's answer field types like the composer.
        "composer"
    } else if window.get_sidebar_focused() {
        "sidebar"
    } else {
        "pane"
    }
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
    // A dialog, the switcher and the other surfaces keep their own.
    if window.get_surface() != "chats" || app.switcher.borrow().open || !app.overlay.borrow().is_empty() {
        return;
    }
    if i_slint_core::window::WindowInner::from_pub(window.window()).focus_item.borrow().upgrade().is_some() {
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
    let mut hints: Vec<Hint> = keymap::hints(state, &overrides, facts(app, window))
        .into_iter()
        // The keyboard's card says what its keys do (below).
        .filter(|b| card_keys.is_none() || !b.action.starts_with("artifact-") || b.action == "artifact-previous")
        .filter(|b| b.action != "artifact-open")
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
                if app.engine.borrow().settings().boolean("explorer/livePreview", false) { "Preview: on".into() } else { "Preview: off".into() }
            } else {
                b.label.into()
            },
        })
        .collect();
    if let Some(keys) = card_keys {
        let at = hints.iter().position(|h| h.label == "Cards").map_or(0, |i| i + 1);
        for (offset, (keys, label)) in keys.into_iter().enumerate() {
            hints.insert(at + offset, Hint { keys: keys.into(), label: label.into() });
        }
    }
    // From the composer, the way onto the chat's cards.
    if state == "composer" && crate::artifacts_view::has_cards(app) && !window.global::<crate::ArtifactBridge>().get_editing() {
        let keys = format!("{} {}", shown("escape", "Esc"), keymap::shown_or("pane", "artifact-previous", &overrides, "K"));
        hints.push(Hint { keys: keys.trim().into(), label: "Cards".into() });
    }
    if state == "keymap" {
        hints = crate::keymap_view::hints(app, window).into_iter().map(|(keys, label)| Hint { keys: keys.into(), label: label.into() }).collect();
    }
    if state == "processes" {
        hints = crate::processes_view::hints();
    }
    if state == "fonts" {
        hints = crate::font_view::hints();
    }
    if state == "hints" && crate::link_hints::count() == 0 {
        hints = vec![Hint { keys: "".into(), label: "No links on screen".into() }];
    }
    // Vim mode's own keys lead; Space is its leader, the switcher two of them.
    if vim_on(app) {
        for hint in hints.iter_mut() {
            match hint.label.as_str() {
                "Commands" if matches!(state, "pane" | "sidebar") => hint.keys = "Space Space".into(),
                "Cards" => hint.keys = "Shift+J/K".into(),
                _ => {}
            }
        }
        let mut lead = crate::vim_view::hints(state);
        lead.append(&mut hints);
        hints = lead;
    }
    crate::vim_view::show(app, window, state);
    let mode = match state {
        "composer" => "INSERT".to_owned(),
        "sidebar" => "EXPLORER".to_owned(),
        "pane" => "CHAT".to_owned(),
        "keymap" => "KEYS".to_owned(),
        "fonts" => "FONT".to_owned(),
        other => other.to_uppercase(),
    };
    window.set_keyboard_mode(mode.into());
    // The one focus ring follows the same state.
    window.global::<Focus>().set_mode(state.into());
    window.set_hints(ModelRc::new(VecModel::from(hints)));
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
        let state = match (crate::app(), crate::window()) {
            (Some(app), Some(window)) => context(&app, &window).to_string(),
            _ => String::new(),
        };
        eprintln!("key-trace: pressed {} {held:?} repeat={repeat} chord={chord:?} state={state} -> {}", keyboard::code_points(text), if used { "taken" } else { "passed on" });
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
            window.set_switcher_placeholder("Recent agents".into());
            window.set_switcher_empty("No other agents yet".into());
            window.set_switcher_hint("↑↓ move · Enter open · Esc close".into());
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
        // The composer's mention list closes before the composer is left.
        "escape" if crate::mention_view::is_open(app) => crate::mention_view::dismiss_active(app),
        // Ctrl+F: message search, in the switcher.
        "search-messages" => {
            open_switcher(app, window);
            app.switcher.borrow_mut().messages_only = true;
            window.set_switcher_placeholder("Search messages in every chat".into());
            window.set_switcher_empty("No message matches".into());
            window.set_switcher_hint("↑↓ move · Enter opens the chat at the message · Esc close".into());
            refresh_switcher(app, window);
        }
        // ---- profile and overview
        "escape" if crate::profile_view::owns(app) => crate::profile_view::escape(app, window),
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
        // The value editor puts the old value back.
        "escape" if matches!(app.overlay.borrow().as_str(), crate::look::EDITOR | crate::look::CONFIRM) => crate::look::cancel_editor(app, window),
        "escape" if !app.overlay.borrow().is_empty() => close_overlay(app, window),
        "connection" => {
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
                app.engine.borrow_mut().stop();
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
                    .filter(|a| a.role == "helper")
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
        "list-all" | "list-unread" => {
            window.invoke_show_pairs(false);
            window.invoke_show_archive(false);
            window.invoke_choose_scope(if action == "list-all" { "all" } else { "unread" }.into());
            window.set_sidebar_visible(true);
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
        "stop-agent" => app.engine.borrow_mut().stop(),
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
    let name = if name.trim().is_empty() { format!("Workspace {}", app.engine.borrow().panes().workspaces().len() + 1) } else { name.trim().to_owned() };
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

fn toggles() -> switcher::Toggles {
    switcher::Toggles { preview_versions: crate::preview_view::enabled() }
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
    }
    window.set_switcher_note("".into());
    window.set_switcher_placeholder("Agent, contact, setting or command".into());
    window.set_switcher_empty("No matching agent or contact".into());
    window.set_switcher_hint("↑↓ move · Enter runs a command, switches a setting or lists its choices · Esc close".into());
    window.set_switcher_open(true);
    window.invoke_show_switcher();
    refresh_switcher(app, window);
}

/// Enter on a setting with choices: the switcher lists its options, the
/// current one marked; Escape goes back to everything.
fn open_picker(app: &App, window: &AppWindow, id: &str) {
    let label = crate::catalogue::find(id).map_or(id, |e| e.label);
    {
        let mut state = app.switcher.borrow_mut();
        state.open = true;
        state.query.clear();
        state.selected.clear();
        state.picker = id.to_owned();
    }
    window.set_switcher_placeholder(format!("{label}: choose one").into());
    window.set_switcher_empty("No matching choice".into());
    window.set_switcher_hint("↑↓ move · Enter chooses · Esc back to everything".into());
    window.set_switcher_open(true);
    window.invoke_show_switcher();
    refresh_switcher(app, window);
    // The current option is where the keyboard starts.
    let current = app.switcher.borrow().items.iter().position(|i| i.value == "Current");
    if let Some(index) = current {
        switcher_moved(app, window, index as i32);
    }
}

/// Escape in a picker: back to the whole list.
fn close_picker(app: &App, window: &AppWindow) {
    app.switcher.borrow_mut().picker.clear();
    window.set_switcher_placeholder("Agent, contact, setting or command".into());
    window.set_switcher_empty("No matching agent or contact".into());
    app.switcher.borrow_mut().query.clear();
    window.set_switcher_hint("↑↓ move · Enter runs a command, switches a setting or lists its choices · Esc close".into());
    window.invoke_show_switcher();
    refresh_switcher(app, window);
}

/// The explorer's and the switcher's note while the agent list is stale,
/// and why (both empty while it is current).
pub fn roster_note(engine: &clarp_engine::Engine) -> (String, String) {
    let state = engine.roster_freshness();
    (state.note(clarp_engine::roster_freshness::local_clock), state.detail())
}

/// Rebuilds the results (a query typed, or the roster changed while open).
pub fn refresh_switcher(app: &App, window: &AppWindow) {
    if !app.switcher.borrow().open {
        return;
    }
    let toggles = toggles();
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
        // Ctrl+F: messages, and what was searched.
        let (items, results) = crate::search_view::items(&mut app.engine.borrow_mut(), &query, crate::search_view::LIMIT);
        note = crate::search_view::note(&results);
        items
    } else if recent_only {
        switcher::recent(&app.engine.borrow(), &app.recent.borrow(), &query)
    } else {
        // Ctrl+K lists agents, contacts, commands and settings, never
        // messages: those are Ctrl+F's.
        if !contacts_only && query.trim().chars().count() >= crate::search_view::HINT_FROM {
            note = "Ctrl+F searches the messages of every chat.".to_owned();
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
    if restore {
        app.focus_composer();
    } else {
        app.focus_transcript();
    }
    show_hints(app, window);
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
        // The picker keeps the keyboard in its search.
        switcher::Kind::Command if item.target == "choose-font" => {
            crate::font_view::open(app, window);
            return;
        }
        switcher::Kind::Command if item.target.starts_with("settingpicker:") => {
            open_picker(app, window, item.target.trim_start_matches("settingpicker:"));
            return;
        }
        // A message: its chat, scrolled to it, the transcript keeping the
        // keyboard to read around it.
        switcher::Kind::Message => {
            crate::search_view::jump(app, &item.target);
            restore = false;
        }
        // The panel keeps the keyboard.
        switcher::Kind::Command if item.target == "agent-processes" => {
            run(app, window, &item.target);
            crate::processes_view::set_origin(restore);
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
    *app.overlay.borrow_mut() = name.to_owned();
    window.set_overlay(name.into());
    show_hints(app, window);
}

/// Closes the dialog; the surface under it (the active pane's composer on
/// the chats) gets the keyboard back.
pub fn close_overlay(app: &App, window: &AppWindow) {
    let closed = std::mem::take(&mut *app.overlay.borrow_mut());
    window.set_overlay("".into());
    if closed == crate::help_view::OVERLAY && crate::help_view::give_back(app, window) {
        show_hints(app, window);
        return;
    }
    if closed == crate::processes_view::OVERLAY && crate::processes_view::give_back(app, window) {
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
    match window.get_surface().as_str() {
        "updates" => window.invoke_focus_updates(),
        "teams" => window.invoke_focus_teams(),
        "settings" => window.invoke_focus_settings(),
        _ => app.focus_composer(),
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
}

pub fn keymap_export(app: &App, window: &AppWindow) {
    window.set_keymap_profile(keymap::export(&overrides(app)).into());
}
