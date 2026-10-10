//! The one place every Ctrl+K command and every user setting is
//! registered: what it is called, a one-line description, the other names
//! people search it by, and, for a setting, how to read, change and reset
//! it. The settings page, Ctrl+K and its pickers all read from here, and
//! the one matcher they share lives here too.
//!
//! A command is an `e(...)` line in `ENTRIES`, run by its action in
//! `commands::run`. A setting is a `Setting` in `settings::register` (or in
//! a module of its own that `register` adds): its text, its section on the
//! settings page, and a toggle, a choice or an action. Ties in a search
//! keep `order`, so the window's own parts come first, left to right as
//! they sit on screen. The tests keep a new command or setting from
//! shipping without a description and two aliases.

use std::rc::Rc;

use crate::{App, AppWindow};

mod prefs;
mod settings;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Entry {
    pub id: &'static str,
    pub label: &'static str,
    pub description: &'static str,
    /// Other names: synonyms, verbs, British and American spellings, old labels.
    pub aliases: &'static [&'static str],
    pub group: &'static str,
    /// The actions whose keys reach it (shown beside a setting).
    pub actions: &'static [&'static str],
}

pub const fn e(id: &'static str, label: &'static str, group: &'static str, description: &'static str, aliases: &'static [&'static str]) -> Entry {
    Entry { id, label, description, aliases, group, actions: &[] }
}

pub const fn keyed(entry: Entry, actions: &'static [&'static str]) -> Entry {
    Entry { actions, ..entry }
}

/// Words a visibility switch answers to.
macro_rules! shown {
    ($($alias:literal),* $(,)?) => { &[$($alias,)* "hide", "show", "toggle", "on off", "visible", "collapse", "expand"] };
}
pub(crate) use shown;

// ---- settings

pub type Getter<T> = Rc<dyn Fn(&App) -> T>;
pub type Setter<T> = Rc<dyn Fn(&Rc<App>, &AppWindow, T)>;
pub type Run = Rc<dyn Fn(&Rc<App>, &AppWindow)>;
pub type Pick = Rc<dyn Fn(&Rc<App>, &AppWindow, &str)>;

/// One option of a choice: the value stored, the label shown, a line about it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Choice {
    pub value: String,
    pub label: String,
    pub description: String,
}

impl Choice {
    pub fn new(value: impl Into<String>, label: impl Into<String>) -> Self {
        Choice { value: value.into(), label: label.into(), description: String::new() }
    }

    pub fn about(self, description: impl Into<String>) -> Self {
        Choice { description: description.into(), ..self }
    }
}

#[derive(Clone)]
pub enum Control {
    /// On or off: Enter and Space switch it.
    Toggle { get: Getter<bool>, set: Setter<bool> },
    /// One of `options` (by value): Left/Right step it; Enter in Ctrl+K lists them.
    Choice { options: Getter<Vec<Choice>>, get: Getter<String>, set: Pick },
    /// A number from `min` to `max`: Left/Right and Ctrl+K +/- move it one
    /// `step` from where it is (clamped); Enter in Ctrl+K lists the steps.
    Number { get: Getter<f64>, set: Setter<f64>, min: f64, max: f64, step: f64, unit: &'static str },
    /// Opens something (a dialog, the font picker).
    Action { run: Run },
}

/// `now` moved `delta` steps, kept within `min..=max`.
pub fn stepped(now: f64, delta: i32, min: f64, max: f64, step: f64) -> f64 {
    tidy(now + f64::from(delta) * step, places(now, step)).clamp(min, max)
}

/// Rounds away float noise at `places` decimals (0.1 + 0.2 is 0.3).
fn tidy(value: f64, places: usize) -> f64 {
    let scale = 10f64.powi(places as i32);
    (value * scale).round() / scale
}

/// The decimals a value needs: its step's, or its own when it falls
/// between steps (12.5 with a step of 1).
fn places(value: f64, step: f64) -> usize {
    decimals(step).max(decimals(tidy(value, 6)))
}

/// How many decimals a step needs: 1 → 0, 0.05 → 2.
fn decimals(step: f64) -> usize {
    (0..6).find(|places| {
        let scale = 10f64.powi(*places);
        ((step * scale).round() - step * scale).abs() < 1e-9
    }).unwrap_or(6) as usize
}

/// A number as stored and listed: as many decimals as its step needs.
pub fn number_text(value: f64, step: f64) -> String {
    let places = places(value, step);
    format!("{:.*}", places, tidy(value, places))
}

/// The steps from `min` to `max`, with `now` among them when it falls between.
pub fn steps(min: f64, max: f64, step: f64, now: f64) -> Vec<f64> {
    let count = ((max - min) / step).round().max(0.0) as usize;
    let mut out: Vec<f64> = (0..=count).map(|i| tidy(min + i as f64 * step, decimals(step))).collect();
    let now = tidy(now, places(now, step));
    if now >= min && now <= max && !out.iter().any(|v| (v - now).abs() < 1e-9) {
        out.push(now);
        out.sort_by(f64::total_cmp);
    }
    out
}

/// A user setting, registered once; the settings page and Ctrl+K show it.
#[derive(Clone)]
pub struct Setting {
    pub entry: Entry,
    /// Its section on the settings page ("APPEARANCE"); `SECTIONS` orders them.
    pub section: &'static str,
    pub control: Control,
    /// What `reset` puts back: "on"/"off", or a choice's value. None: no reset.
    pub default: Option<String>,
    /// Listed only while this holds (a Host feature).
    pub shown: Option<Getter<bool>>,
    /// A label that changes (the Host's name), instead of the entry's.
    pub label: Option<Getter<String>>,
    /// The value as shown, instead of the option's label (an action's state).
    pub detail: Option<Getter<String>>,
    /// How `reset` puts it back when its default is not one value (a font
    /// size back to the theme's own): runs instead of applying `default`.
    pub reset_to: Option<Run>,
}

impl Setting {
    fn with(entry: Entry, section: &'static str, control: Control) -> Self {
        Setting { entry, section, control, default: None, shown: None, label: None, detail: None, reset_to: None }
    }

    pub fn toggle(entry: Entry, section: &'static str, get: impl Fn(&App) -> bool + 'static, set: impl Fn(&Rc<App>, &AppWindow, bool) + 'static) -> Self {
        Self::with(entry, section, Control::Toggle { get: Rc::new(get), set: Rc::new(set) })
    }

    /// A switch kept in the app's settings under `key`.
    pub fn stored_toggle(entry: Entry, section: &'static str, key: &'static str, default: bool) -> Self {
        Self::toggle(entry, section, move |app| app.engine.borrow().settings().boolean(key, default), move |app, _, on| app.engine.borrow_mut().settings_mut().set(key, on))
            .default(if default { "on" } else { "off" })
    }

    pub fn choice(
        entry: Entry,
        section: &'static str,
        options: impl Fn(&App) -> Vec<Choice> + 'static,
        get: impl Fn(&App) -> String + 'static,
        set: impl Fn(&Rc<App>, &AppWindow, &str) + 'static,
    ) -> Self {
        Self::with(entry, section, Control::Choice { options: Rc::new(options), get: Rc::new(get), set: Rc::new(set) })
    }

    /// A choice kept in the app's settings under `key`, as its value.
    pub fn stored_choice(entry: Entry, section: &'static str, key: &'static str, options: Vec<Choice>, default: &'static str) -> Self {
        Self::choice(
            entry,
            section,
            move |_| options.clone(),
            move |app| app.engine.borrow().settings().string(key, default),
            move |app, _, value| app.engine.borrow_mut().settings_mut().set(key, value),
        )
        .default(default)
    }

    /// A number from `min` to `max` in `step`s, shown with its `unit` ("px", "%", "").
    #[allow(clippy::too_many_arguments)]
    pub fn number(
        entry: Entry,
        section: &'static str,
        get: impl Fn(&App) -> f64 + 'static,
        set: impl Fn(&Rc<App>, &AppWindow, f64) + 'static,
        min: f64,
        max: f64,
        step: f64,
        unit: &'static str,
    ) -> Self {
        debug_assert!(step > 0.0 && min <= max, "{}: a number needs a step and min <= max", entry.id);
        Self::with(entry, section, Control::Number { get: Rc::new(get), set: Rc::new(set), min, max, step, unit })
    }

    /// A number kept in the app's settings under `key`.
    #[allow(clippy::too_many_arguments)]
    pub fn stored_number(entry: Entry, section: &'static str, key: &'static str, default: f64, min: f64, max: f64, step: f64, unit: &'static str) -> Self {
        Self::number(
            entry,
            section,
            move |app| app.engine.borrow().settings().get(key).and_then(serde_json::Value::as_f64).unwrap_or(default),
            move |app, _, value| app.engine.borrow_mut().settings_mut().set(key, value),
            min,
            max,
            step,
            unit,
        )
        .default(number_text(default, step))
    }

    pub fn action(entry: Entry, section: &'static str, run: impl Fn(&Rc<App>, &AppWindow) + 'static) -> Self {
        Self::with(entry, section, Control::Action { run: Rc::new(run) })
    }

    pub fn default(self, value: impl Into<String>) -> Self {
        Setting { default: Some(value.into()), ..self }
    }

    /// Resets by running `reset` (when the default is not one fixed value).
    pub fn reset_with(self, reset: impl Fn(&Rc<App>, &AppWindow) + 'static) -> Self {
        Setting { reset_to: Some(Rc::new(reset)), ..self }
    }

    pub fn when(self, shown: impl Fn(&App) -> bool + 'static) -> Self {
        Setting { shown: Some(Rc::new(shown)), ..self }
    }

    pub fn label_from(self, label: impl Fn(&App) -> String + 'static) -> Self {
        Setting { label: Some(Rc::new(label)), ..self }
    }

    pub fn detail(self, detail: impl Fn(&App) -> String + 'static) -> Self {
        Setting { detail: Some(Rc::new(detail)), ..self }
    }

    /// Runs `after` once a change is made (to show it in the window).
    pub fn then(self, after: impl Fn(&Rc<App>, &AppWindow) + 'static) -> Self {
        let after: Run = Rc::new(after);
        let control = match self.control {
            Control::Toggle { get, set } => {
                let set: Setter<bool> = Rc::new(move |app: &Rc<App>, window: &AppWindow, on: bool| {
                    set(app, window, on);
                    after(app, window);
                });
                Control::Toggle { get, set }
            }
            Control::Choice { options, get, set } => {
                let set: Pick = Rc::new(move |app: &Rc<App>, window: &AppWindow, value: &str| {
                    set(app, window, value);
                    after(app, window);
                });
                Control::Choice { options, get, set }
            }
            Control::Number { get, set, min, max, step, unit } => {
                let after = after.clone();
                let set: Setter<f64> = Rc::new(move |app: &Rc<App>, window: &AppWindow, value: f64| {
                    set(app, window, value);
                    after(app, window);
                });
                Control::Number { get, set, min, max, step, unit }
            }
            Control::Action { run } => {
                let run: Run = Rc::new(move |app: &Rc<App>, window: &AppWindow| {
                    run(app, window);
                    after(app, window);
                });
                Control::Action { run }
            }
        };
        Setting { control, ..self }
    }

    pub fn id(&self) -> &'static str {
        self.entry.id
    }

    /// "toggle", "choice", "number" or "action" (the settings page's row kinds).
    pub fn kind(&self) -> &'static str {
        match self.control {
            Control::Toggle { .. } => "toggle",
            Control::Choice { .. } => "choice",
            Control::Number { .. } => "number",
            Control::Action { .. } => "action",
        }
    }

    pub fn is_shown(&self, app: &App) -> bool {
        self.shown.as_ref().is_none_or(|shown| shown(app))
    }

    pub fn label(&self, app: &App) -> String {
        self.label.as_ref().map_or_else(|| self.entry.label.to_owned(), |label| label(app))
    }

    /// The stored value: "on"/"off", the chosen option's value or the number.
    pub fn current(&self, app: &App) -> String {
        match &self.control {
            Control::Toggle { get, .. } => if get(app) { "on" } else { "off" }.to_owned(),
            Control::Choice { get, .. } => get(app),
            Control::Number { get, step, .. } => number_text(get(app), *step),
            Control::Action { .. } => String::new(),
        }
    }

    pub fn on(&self, app: &App) -> bool {
        matches!(&self.control, Control::Toggle { get, .. } if get(app))
    }

    /// The value as shown: "On", the chosen option's label, an action's state.
    pub fn value(&self, app: &App) -> String {
        if let Some(detail) = &self.detail {
            return detail(app);
        }
        match &self.control {
            Control::Toggle { get, .. } => if get(app) { "On" } else { "Off" }.to_owned(),
            Control::Choice { options, get, .. } => {
                let now = get(app);
                options(app).into_iter().find(|o| o.value == now).map_or(now, |o| o.label)
            }
            Control::Number { get, step, unit, .. } => with_unit(&number_text(get(app), *step), unit),
            Control::Action { .. } => String::new(),
        }
    }

    /// A choice's options, or a number's steps (its current value among them).
    pub fn options(&self, app: &App) -> Vec<Choice> {
        match &self.control {
            Control::Choice { options, .. } => options(app),
            Control::Number { get, min, max, step, unit, .. } => steps(*min, *max, *step, get(app))
                .into_iter()
                .map(|value| {
                    let text = number_text(value, *step);
                    let label = with_unit(&text, unit);
                    let choice = Choice::new(text, label);
                    match &self.default {
                        Some(default) if *default == choice.value => choice.about("The default"),
                        _ => choice,
                    }
                })
                .collect(),
            _ => Vec::new(),
        }
    }

    /// Sets it from a stored value ("on"/"off", a choice's value), as a
    /// settings file or `reset` would; false for a value it does not take.
    pub fn apply(&self, app: &Rc<App>, window: &AppWindow, value: &str) -> bool {
        match &self.control {
            Control::Toggle { set, .. } => match value {
                "on" | "true" => set(app, window, true),
                "off" | "false" => set(app, window, false),
                _ => return false,
            },
            Control::Choice { options, set, .. } => {
                if !options(app).iter().any(|o| o.value == value) {
                    return false;
                }
                set(app, window, value);
            }
            Control::Number { set, min, max, .. } => match value.trim().parse::<f64>() {
                Ok(number) if number.is_finite() => set(app, window, number.clamp(*min, *max)),
                _ => return false,
            },
            Control::Action { .. } => return false,
        }
        true
    }

    /// Puts its default back; false when it has none.
    pub fn reset(&self, app: &Rc<App>, window: &AppWindow) -> bool {
        if let Some(reset) = &self.reset_to {
            reset(app, window);
            return true;
        }
        let Some(default) = self.default.clone() else { return false };
        self.current(app) == default || self.apply(app, window, &default)
    }

    /// Enter or Space (`delta` 1) and Left/Right: a toggle switches, a
    /// choice steps, an action runs.
    pub fn change(&self, app: &Rc<App>, window: &AppWindow, delta: i32) {
        match &self.control {
            Control::Toggle { get, set } => {
                let on = get(app);
                set(app, window, !on);
            }
            Control::Choice { options, get, set } => {
                let options = options(app);
                if options.is_empty() {
                    return;
                }
                let now = get(app);
                let at = options.iter().position(|o| o.value == now).map_or(0, |i| i as i32);
                let next = &options[(at + delta).rem_euclid(options.len() as i32) as usize];
                set(app, window, &next.value);
            }
            Control::Number { get, set, min, max, step, .. } => {
                let next = stepped(get(app), delta, *min, *max, *step);
                set(app, window, next);
            }
            Control::Action { run } => run(app, window),
        }
    }
}

fn with_unit(number: &str, unit: &str) -> String {
    match unit {
        "" => number.to_owned(),
        "%" => format!("{number}%"),
        unit => format!("{number} {unit}"),
    }
}

/// Puts every setting in `section` back to its default; how many changed.
pub fn reset_section(app: &Rc<App>, window: &AppWindow, section: &str) -> usize {
    settings().iter().filter(|s| s.section == section).filter(|s| s.reset(app, window)).count()
}

/// Puts every setting back to its default; how many it could.
pub fn reset_all(app: &Rc<App>, window: &AppWindow) -> usize {
    settings().iter().filter(|s| s.reset(app, window)).count()
}

/// The settings page's sections, in order; a setting in another section
/// gets one of its own after these.
pub const SECTIONS: &[&str] = &[
    "CHATS",
    "EXPERIMENTS",
    "STARTUP",
    "AGENT IDENTITY",
    "APPEARANCE",
    "TYPOGRAPHY",
    "LAYOUT",
    "COLOURS",
    "VOICE & AUDIO",
    "NOTIFICATIONS",
    "BEHAVIOUR",
    "HOST",
    "HOST STATUS",
    "KEYBOARD",
    "ALL SETTINGS",
    "ABOUT",
];

thread_local! {
    static SETTINGS: Rc<Vec<Setting>> = Rc::new(settings::register());
}

/// Every setting, in the settings page's order within each section.
pub fn settings() -> Rc<Vec<Setting>> {
    SETTINGS.with(Rc::clone)
}

pub fn setting(id: &str) -> Option<Setting> {
    SETTINGS.with(|all| all.iter().find(|s| s.entry.id == id).cloned())
}

/// The sections in the page's order: `SECTIONS`, then any new ones.
pub fn sections() -> Vec<&'static str> {
    let mut out: Vec<&'static str> = SECTIONS.to_vec();
    for setting in settings().iter() {
        if !out.contains(&setting.section) {
            out.push(setting.section);
        }
    }
    out
}

// ---- commands

pub const ENTRIES: &[Entry] = &[
    e("choose-font", "Choose font…", "appearance", "Picks the chat's typeface and size for this reading theme, previewed in the chat.", &["font", "typeface", "font family", "monospace", "serif", "sans"]),
    e("edit-keymap", "Customize key bindings", "view", "Opens the editor for every keyboard shortcut.", &["keymap", "shortcuts", "hotkeys", "keyboard", "customise", "rebind", "bindings"]),
    e("new-workspace", "New tab", "layout", "Opens a new workspace tab with its own pane layout.", &["new workspace", "tabnew", "add tab", "open tab", "workspace"]),
    e("next-workspace", "Next tab", "layout", "Switches to the next workspace tab.", &["next workspace", "tabnext", "cycle", "switch workspace", "tab"]),
    e("previous-workspace", "Previous tab", "layout", "Switches to the workspace tab before this one.", &["previous workspace", "tabprevious", "back", "switch workspace", "tab"]),
    e("close-workspace", "Close tab", "layout", "Closes this workspace tab; its chats stay in the explorer.", &["close workspace", "tabclose", "remove tab", "delete workspace", "tab"]),
    e("help-keys", "Keys here", "view", "Shows the keys that matter where the keyboard is, your own bindings included.", &["help", "cheat sheet", "shortcuts", "which key", "keyboard help", "?"]),
    e("quick-new-agent", "New contact & chat", "agent", "Creates a new contact and opens a chat with it.", &["new session", "hub", "create", "add contact", "new persona"]),
    e("rename-agent", "Rename contact", "agent", "Changes the open contact's name.", &["rename", "name", "title", "relabel", "persona"]),
    e("new", "New session", "agent", "Opens the hub to start a new agent.", &["new agent", "start", "chat", "provider", "contact", "hub", "create"]),
    e("preview-versions", "Preview versions · update or roll back", "settings", "Installs another build of this app, newer or older.", &["previous installs", "rollback", "downgrade", "upgrade", "update", "version"]),
    e("new-contact", "Start an idle contact", "agent", "Starts one of your idle contacts in a new session.", &["new session", "hub", "contact", "wake", "resume"]),
    e("agent-terminal", "Open agent in terminal", "agent", "Opens the agent's native CLI in a terminal.", &["terminal", "cli", "shell", "console", "native"]),
    e("split-right", "Split right", "layout", "Opens a second pane to the right.", &["vertical split", "side by side", "pane", "columns", "divide"]),
    e("split-down", "Split down", "layout", "Opens a second pane below.", &["horizontal split", "stack", "pane", "rows", "divide"]),
    e("close-pane", "Close pane", "layout", "Closes the active pane.", &["remove pane", "unsplit", "merge", "pane"]),
    e("zoom", "Zoom pane", "layout", "Fills the window with the active pane, or restores the split.", &["maximise", "maximize", "fullscreen", "focus pane", "pane"]),
    e("balance", "Balance panes", "layout", "Makes every pane the same size.", &["equalise", "equalize", "even", "resize", "panes"]),
    e("chat-zoom-in", "Zoom in", "view", "Draws the chats one step (10%) larger: text, code, cards and spacing, apart from the rest of the window.", &["magnify", "bigger", "larger", "enlarge", "text size", "chat zoom", "increase"]),
    e("chat-zoom-out", "Zoom out", "view", "Draws the chats one step (10%) smaller, apart from the rest of the window.", &["smaller", "shrink", "reduce", "text size", "chat zoom", "decrease"]),
    e("chat-zoom-reset", "Reset zoom", "view", "Draws the chats at 100% again.", &["actual size", "100%", "normal size", "unzoom", "chat zoom", "default zoom"]),
    e("ui-larger", "Larger interface", "view", "Draws the whole window one step larger, the chats included.", &["scale up", "bigger window", "increase", "window size", "dpi"]),
    e("ui-smaller", "Smaller interface", "view", "Draws the whole window one step smaller, the chats included.", &["scale down", "smaller window", "shrink window", "window size", "dpi"]),
    e("ui-reset", "Reset interface size", "view", "Returns the whole window to its usual scale.", &["default size", "actual size", "scale", "window size"]),
    e("list-all", "Agents: all chats", "view", "Shows every chat in the explorer.", &["sidebar list", "scope", "everything", "back", "all agents"]),
    e("list-unread", "Agents: unread only", "view", "Shows only chats with unread messages in the explorer.", &["sidebar list", "filter", "new", "unread", "scope"]),
    e("list-rooms", "Agent conversations (agent to agent)", "view", "Shows the rooms where agents talk to each other.", &["pairs", "rooms", "sidebar", "agent to agent", "a2a"]),
    e("list-archive", "Archived agents", "view", "Shows archived agents so you can restore them.", &["archive", "restore", "old", "sidebar", "history"]),
    e("link-hints", "Open a link", "view", "Numbers the links on screen; type a number to open one.", &["links", "hints", "url", "urls", "browser", "follow", "vimium", "web"]),
    e("jump-latest", "Jump to latest", "view", "Scrolls the chat to its newest message.", &["bottom", "newest", "scroll", "follow", "end"]),
    e("retry-message", "Retry latest failed message", "view", "Sends the last message that failed again.", &["resend", "send", "delivery", "not delivered", "retry"]),
    e("dismiss-error", "Dismiss conversation error", "view", "Closes the error banner above the chat.", &["clear", "close", "error", "warning", "banner", "voice synthesis failed"]),
    e("dismiss-layout-warning", "Dismiss layout warning", "view", "Closes the warning that another window saved a newer layout.", &["another window", "newer layout", "conflict", "recovery", "close"]),
    e("keep-layout", "Keep this window's layout", "view", "Saves this window's layout over the newer one another window saved.", &["another window", "newer layout", "conflict", "recovery", "save instead"]),
    e("change-directory", "Change directory", "agent", "Starts a new chat in another folder.", &["folder", "workspace", "cwd", "new chat", "path"]),
    e("recent-agents", "Recent agents", "agent", "Lists the agents you opened last, newest first.", &["last", "previous", "switch back", "mru", "history"]),
    e("search-messages", "Search messages", "view", "Finds words in the messages of every chat on this computer and jumps to the message.", &["find", "find in chats", "text", "history", "transcript", "grep", "look up"]),
    e("open-in-browser", "Open report in browser", "view", "Opens an HTML report's whole page, pictures and layout included, in a new browser window; Clarp shows plain ones as a quick preview.", &["browser", "full page", "images", "html", "web page", "external"]),
    e("refresh", "Refresh conversation", "view", "Loads the open chat again from the Host.", &["reload", "update", "sync", "fetch"]),
    e("refresh-agents", "Refresh agents", "view", "Fetches the agent list from the Host now, also while it is retrying after a failure.", &["reload", "refresh", "roster", "sync", "agent list", "contacts", "stale"]),
    e("agent-processes", "Background processes", "agent", "Shows the selected agent's background jobs and running helpers: open a job's output, stop a job or a helper. On Updates, every job.", &["processes", "jobs", "background jobs", "tasks", "running", "helpers", "kill", "stop job", "output", "logs", "show processes"]),
    e("overview", "Agent overview", "view", "Shows every agent and what it is doing.", &["dashboard", "summary", "all agents", "status", "fleet"]),
    e("chats", "Chats", "destination", "Goes to the chats.", &["conversations", "messages", "home", "main"]),
    e("updates", "Updates", "destination", "Goes to the updates feed.", &["feed", "activity", "news", "attention", "inbox"]),
    e("teams", "Teams", "destination", "Goes to the teams.", &["groups", "team", "crews", "squads"]),
    e("settings", "Settings", "destination", "Opens the settings page.", &["preferences", "options", "configuration", "config", "customise", "customize"]),
    e("next-attention", "Next agent needing attention", "agent", "Opens the next agent that is waiting for you.", &["attention", "waiting", "blocked", "needs me", "next"]),
    e("next-agent", "Next agent", "agent", "Opens the next agent in the list.", &["following", "chat", "switch", "forward", "down"]),
    e("previous-agent", "Previous agent", "agent", "Opens the agent above in the list.", &["back", "chat", "switch", "prior", "up"]),
    e("release-agent", "Release agent", "agent", "Ends the open agent's session and frees its contact.", &["end", "free", "finish", "close agent", "dismiss"]),
    e("stop-agent", "Stop agent", "agent", "Interrupts what the open agent is doing.", &["interrupt", "cancel", "halt", "abort", "kill"]),
    e("talk", "Talk", "audio", "Records your voice and sends it to the open agent.", &["voice", "microphone", "mic", "dictate", "speak", "record", "push to talk"]),
];

/// A setting's or a command's text.
pub fn find(id: &str) -> Option<Entry> {
    setting(id).map(|s| s.entry).or_else(|| ENTRIES.iter().find(|entry| entry.id == id).copied())
}

/// Ties in a search: the window's parts first, left to right, then the
/// settings in their page order, then the commands.
const FIRST: &[&str] = &["nav-rail", "explorer", "avatar-size", "compact-explorer", "live-preview", "workspace-bar", "shortcut-bar", "minimal-ui", "timestamps"];

/// Every setting's and command's text, in `order`.
pub fn entries() -> Vec<Entry> {
    let mut all: Vec<Entry> = settings().iter().map(|s| s.entry).chain(ENTRIES.iter().copied()).collect();
    all.sort_by_key(|entry| order(entry.id));
    all
}

/// The entry's place in a tie.
pub fn order(id: &str) -> usize {
    if let Some(at) = FIRST.iter().position(|first| *first == id) {
        return at;
    }
    let (count, at) = SETTINGS.with(|all| (all.len(), all.iter().position(|s| s.entry.id == id)));
    match at {
        Some(at) => FIRST.len() + at,
        None => FIRST.len() + count + ENTRIES.iter().position(|entry| entry.id == id).unwrap_or(ENTRIES.len()),
    }
}

fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty())
}

/// Optimal string alignment distance: insertions, deletions, substitutions
/// and swapped neighbours each cost one.
fn distance(a: &[char], b: &[char]) -> usize {
    let mut rows = vec![vec![0usize; b.len() + 1]; a.len() + 1];
    for (i, row) in rows.iter_mut().enumerate() {
        row[0] = i;
    }
    for j in 0..=b.len() {
        rows[0][j] = j;
    }
    for i in 1..=a.len() {
        for j in 1..=b.len() {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            let mut best = (rows[i - 1][j] + 1).min(rows[i][j - 1] + 1).min(rows[i - 1][j - 1] + cost);
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                best = best.min(rows[i - 2][j - 2] + 1);
            }
            rows[i][j] = best;
        }
    }
    rows[a.len()][b.len()]
}

/// A typo of `word` or of how it starts (as long as the term or one letter
/// longer): within one edit, two from seven letters.
fn near(term: &str, word: &str) -> bool {
    let term: Vec<char> = term.chars().collect();
    if term.len() < 4 {
        return false;
    }
    let word: Vec<char> = word.chars().collect();
    let allowed = if term.len() >= 7 { 2 } else { 1 };
    let lengths = [term.len(), term.len() + 1, word.len()];
    lengths.iter().filter(|n| **n > 0 && **n <= word.len()).any(|n| distance(&term, &word[..*n]) <= allowed)
}

/// How well one lowercase term matches: the label first (from its start,
/// then a word's start, then anywhere), then an alias, then the description,
/// then a typo of any of them.
fn term_score(term: &str, label: &str, aliases: &[String], description: &str) -> Option<u32> {
    let starts = |text: &str| words(text).any(|w| w.starts_with(term));
    let score = if label.starts_with(term) {
        1000
    } else if starts(label) {
        900
    } else if label.contains(term) {
        700
    } else if aliases.iter().any(|a| a.starts_with(term) || starts(a)) {
        600
    } else if aliases.iter().any(|a| a.contains(term)) {
        500
    } else if starts(description) {
        300
    } else if description.contains(term) {
        200
    } else if words(label).any(|w| near(term, w)) {
        150
    } else if aliases.iter().any(|a| words(a).any(|w| near(term, w))) {
        120
    } else if words(description).any(|w| near(term, w)) {
        50
    } else {
        return None;
    };
    Some(score)
}

/// The least score of a query matched as typed, every word of it (a typo
/// alone scores less): once something matches as typed, the typos go.
pub fn as_typed(query: &str) -> u32 {
    200 * query.split_whitespace().count() as u32
}

/// How well `query` matches: every word must match somewhere (higher is
/// better); None when one does not. An empty query matches everything.
pub fn score(query: &str, label: &str, aliases: &[&str], description: &str) -> Option<u32> {
    let label = label.to_lowercase();
    let description = description.to_lowercase();
    let aliases: Vec<String> = aliases.iter().map(|a| a.to_lowercase()).collect();
    let terms: Vec<String> = query.to_lowercase().split_whitespace().map(str::to_owned).collect();
    // The whole query as one alias ("left bar") is worth more than its words apart.
    let phrase = if terms.len() > 1 && aliases.iter().any(|a| a.starts_with(&terms.join(" "))) { 200 } else { 0 };
    terms.iter().try_fold(phrase, |total, term| term_score(term, &label, &aliases, &description).map(|s| total + s))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ranked(query: &str) -> Vec<&'static str> {
        let mut hits: Vec<(u32, usize, &str)> =
            entries().iter().enumerate().filter_map(|(i, e)| score(query, e.label, e.aliases, e.description).map(|s| (s, i, e.id))).collect();
        hits.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        hits.into_iter().map(|(_, _, id)| id).collect()
    }

    fn top3(query: &str, id: &str) {
        let hits = ranked(query);
        assert!(hits.iter().take(3).any(|h| *h == id), "{query:?} must find {id} in the top three: {hits:?}");
    }

    #[test]
    fn every_command_and_setting_has_a_description_and_two_aliases() {
        let all = entries();
        for entry in &all {
            assert!(!entry.label.is_empty(), "{} has no label", entry.id);
            assert!(entry.description.len() > 10 && !entry.description.contains('\n'), "{} needs a one-line description", entry.id);
            assert!(entry.aliases.len() >= 2, "{} needs at least two aliases", entry.id);
            assert_eq!(all.iter().filter(|other| other.id == entry.id).count(), 1, "{} is registered twice", entry.id);
        }
    }

    #[test]
    fn every_setting_has_a_section_and_a_toggle_defaults_on_or_off() {
        let sections = sections();
        for setting in settings().iter() {
            assert!(sections.contains(&setting.section), "{} has no section", setting.id());
            if let (Some(default), Control::Toggle { .. }) = (&setting.default, &setting.control) {
                assert!(default == "on" || default == "off", "{}: a toggle's default is on or off", setting.id());
            }
        }
        for id in ["nav-rail", "font-size", "reading-theme", "connection"] {
            assert!(setting(id).is_some(), "{id} is registered");
        }
    }

    #[test]
    fn peters_words_find_the_activity_bar() {
        for query in ["hide", "collapse", "toggle", "left bar", "icons", "rail", "activity", "colapse", "hdie", "navigation rail"] {
            top3(query, "nav-rail");
        }
    }

    #[test]
    fn the_label_beats_an_alias_and_an_alias_beats_the_description() {
        assert_eq!(ranked("split")[..2], ["split-right", "split-down"]);
        assert_eq!(ranked("theme")[0], "reading-theme");
        assert_eq!(ranked("colour")[0], "reading-theme", "British spelling");
        assert_eq!(ranked("color")[0], "reading-theme", "American spelling");
        assert_eq!(ranked("mute")[0], "spoken-replies");
        top3("sidebar", "explorer");
        top3("notifications", "pause-mobile-push");
        top3("double tap", "double-press");
        // Zoom is the chat's; the window's own size is its scale.
        assert_eq!(ranked("zoom in")[0], "chat-zoom-in");
        assert_eq!(ranked("zoom out")[0], "chat-zoom-out");
        assert_eq!(ranked("reset zoom")[0], "chat-zoom-reset");
        top3("magnify", "chat-zoom-in");
        top3("interface scale", "ui-scale");
        assert_eq!(ranked("avatar")[0], "avatar-size");
        assert_eq!(ranked("font size")[0], "font-size");
        assert_eq!(ranked("text size")[0], "font-size");
        top3("bigger text", "font-size");
        top3("portrait size", "avatar-size");
    }

    #[test]
    fn a_number_steps_from_where_it_is_and_stays_in_range() {
        assert_eq!(stepped(15.0, 1, 11.0, 28.0, 1.0), 16.0, "from the theme's 15, + is 16, not the first step");
        assert_eq!(stepped(15.0, -1, 11.0, 28.0, 1.0), 14.0);
        assert_eq!(stepped(28.0, 1, 11.0, 28.0, 1.0), 28.0, "clamped at the top");
        assert_eq!(stepped(11.0, -3, 11.0, 28.0, 1.0), 11.0, "and the bottom");
        assert_eq!(stepped(1.15, 1, 1.0, 1.4, 0.05), 1.2, "no float noise");
        assert_eq!(stepped(1.1, 2, 1.0, 1.4, 0.1), 1.3);
        assert_eq!(number_text(1.2000000001, 0.05), "1.20");
        assert_eq!(number_text(16.0, 1.0), "16");
        assert_eq!(number_text(12.5, 1.0), "12.5", "an off-step value keeps its own decimals");
        assert_eq!(stepped(12.5, 1, 11.0, 28.0, 1.0), 13.5);
        assert_eq!(steps(1.0, 1.2, 0.1, 1.0), [1.0, 1.1, 1.2]);
        assert_eq!(steps(11.0, 13.0, 1.0, 12.5), [11.0, 12.0, 12.5, 13.0], "an off-step value is listed where it falls");
        assert_eq!(with_unit("16", "px"), "16 px");
        assert_eq!(with_unit("115", "%"), "115%");
    }

    #[test]
    fn every_word_must_match() {
        assert_eq!(ranked("split right"), ["split-right"]);
        assert!(ranked("split banana").is_empty());
        assert!(score("", "Anything", &[], "").is_some());
    }

    #[test]
    fn typos_are_forgiven_only_so_far() {
        assert!(near("colapse", "collapse"));
        assert!(near("hdie", "hide"));
        assert!(near("timstamps", "timestamps"));
        assert!(!near("hid", "hide"), "three letters are matched exactly");
        assert!(!near("zebra", "theme"));
        assert!(!near("hide", "identity"), "a shorter start is not a typo");
    }
}
