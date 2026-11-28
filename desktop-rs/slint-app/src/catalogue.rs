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

use clarp_core::tr;

use crate::{App, AppWindow};

pub mod lanes;
mod language;
mod prefs;
mod settings;
pub use settings::COORDINATION_LINE;

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

/// English kept in a `const` or a static table and marked for the catalog:
/// `en::tr!("Text")` is the text itself, and `tools/i18n-export` reads it as
/// it reads `tr!` (with its `// i18n:` note). It stays English where it is
/// kept, so it identifies and finds things in English, and is translated
/// where it is drawn, with `local`.
pub mod en {
    macro_rules! marked {
        ($text:literal $(,)?) => {
            $text
        };
    }
    pub(crate) use marked as tr;
}

/// Text marked with `en::tr!` (or any English the catalog holds) in the
/// app's language now; English when it has no translation.
pub fn local(english: &str) -> String {
    if english.is_empty() {
        return String::new();
    }
    clarp_core::i18n::translate("", english, "", None, &[])
}

impl Entry {
    /// The label in the app's language.
    pub fn label_text(&self) -> String {
        local(self.label)
    }

    /// The description in the app's language.
    pub fn description_text(&self) -> String {
        local(self.description)
    }
}

/// How well `query` matches an entry in the app's language or in English:
/// the better of the two, so a setting is found by its English words too.
pub fn entry_score(query: &str, entry: &Entry) -> Option<u32> {
    let english = score(query, entry.label, entry.aliases, entry.description);
    if clarp_core::i18n::current() == "en" {
        return english;
    }
    let shown = score(query, &entry.label_text(), entry.aliases, &entry.description_text());
    shown.max(english)
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
    /// A description that changes (in the app's language), instead of the entry's.
    pub about: Option<Getter<String>>,
    /// The value as shown, instead of the option's label (an action's state).
    pub detail: Option<Getter<String>>,
    /// How `reset` puts it back when its default is not one value (a font
    /// size back to the theme's own): runs instead of applying `default`.
    pub reset_to: Option<Run>,
    /// A number's range read at run time (from its Host), instead of its
    /// own `min` and `max` while it says one.
    pub range: Option<Getter<Option<(f64, f64)>>>,
}

impl Setting {
    fn with(entry: Entry, section: &'static str, control: Control) -> Self {
        Setting { entry, section, control, default: None, shown: None, label: None, about: None, detail: None, reset_to: None, range: None }
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

    /// A number's range as something else says it now (its Host).
    pub fn range_from(self, range: impl Fn(&App) -> Option<(f64, f64)> + 'static) -> Self {
        Setting { range: Some(Rc::new(range)), ..self }
    }

    /// A number's range now: the one read at run time, else its own.
    fn bounds(&self, app: &App, min: f64, max: f64) -> (f64, f64) {
        self.range.as_ref().and_then(|range| range(app)).unwrap_or((min, max))
    }

    pub fn when(self, shown: impl Fn(&App) -> bool + 'static) -> Self {
        Setting { shown: Some(Rc::new(shown)), ..self }
    }

    pub fn label_from(self, label: impl Fn(&App) -> String + 'static) -> Self {
        Setting { label: Some(Rc::new(label)), ..self }
    }

    pub fn describe_from(self, about: impl Fn(&App) -> String + 'static) -> Self {
        Setting { about: Some(Rc::new(about)), ..self }
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
        self.label.as_ref().map_or_else(|| self.entry.label_text(), |label| label(app))
    }

    pub fn description(&self, app: &App) -> String {
        self.about.as_ref().map_or_else(|| self.entry.description_text(), |about| about(app))
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
            Control::Toggle { get, .. } => {
                if get(app) {
                    // i18n: Settings and Ctrl+K, the value of a switch that is on (adjective, not a verb; the same word as the quick switcher's "On" pill)
                    tr!("On")
                } else {
                    // i18n: Settings and Ctrl+K, the value of a switch that is off (adjective, not a verb)
                    tr!("Off")
                }
            }
            Control::Choice { options, get, .. } => {
                let now = get(app);
                options(app).into_iter().find(|o| o.value == now).map_or(now, |o| o.label)
            }
            Control::Number { get, step, unit, .. } => shown_number(get(app), *step, unit),
            Control::Action { .. } => String::new(),
        }
    }

    /// A choice's options, or a number's steps (its current value among them).
    pub fn options(&self, app: &App) -> Vec<Choice> {
        match &self.control {
            Control::Choice { options, .. } => options(app),
            Control::Number { get, min, max, step, unit, .. } => {
                let (min, max) = self.bounds(app, *min, *max);
                steps(min, max, *step, get(app))
                .into_iter()
                .map(|value| {
                    let choice = Choice::new(number_text(value, *step), shown_number(value, *step, unit));
                    match &self.default {
                        // i18n: Settings and Ctrl+K, list of a number setting's values, the line beside the default value
                        Some(default) if *default == choice.value => choice.about(tr!("The default")),
                        _ => choice,
                    }
                })
                .collect()
            }
            _ => Vec::new(),
        }
    }

    /// Sets it from a stored value ("on"/"off", a choice's value), as a
    /// settings file or `reset` would; false for a value it does not take.
    pub fn apply(&self, app: &Rc<App>, window: &AppWindow, value: &str) -> bool {
        // A setting the user changes may resize the chats (text size, density): their own input.
        crate::scroll_journal::input_all();
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
                Ok(number) if number.is_finite() => {
                    let (min, max) = self.bounds(app, *min, *max);
                    set(app, window, number.clamp(min, max))
                }
                _ => return false,
            },
            Control::Action { .. } => return false,
        }
        true
    }

    /// Puts its default back; false when it has none.
    pub fn reset(&self, app: &Rc<App>, window: &AppWindow) -> bool {
        // A setting the user changes may resize the chats (text size, density): their own input.
        crate::scroll_journal::input_all();
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
        // A setting the user changes may resize the chats (text size, density): their own input.
        crate::scroll_journal::input_all();
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
                let (min, max) = self.bounds(app, *min, *max);
                let next = stepped(get(app), delta, min, max, *step);
                set(app, window, next);
            }
            Control::Action { run } => run(app, window),
        }
    }
}

/// A number's text with its unit, as the settings page shows it.
pub fn with_unit(number: &str, unit: &str) -> String {
    match unit {
        "" => number.to_owned(),
        "%" => format!("{number}%"),
        unit => format!("{number} {unit}"),
    }
}

/// A number as the settings page shows it, in the app's language: its
/// decimal separator, and the unit written as the language writes it.
pub fn shown_number(value: f64, step: f64, unit: &str) -> String {
    let places = places(value, step);
    let number = clarp_core::i18n::format::decimal(clarp_core::i18n::current(), tidy(value, places), places as u8);
    match unit {
        "" => number,
        // i18n: Settings, a size or distance value; {} is the number, "px" is pixels
        "px" => tr!("{} px", number),
        // i18n: Settings, a percentage value; {} is the number
        "%" => tr!("{}%", number),
        // i18n: Settings, a time value; {} is the number, "ms" is milliseconds
        "ms" => tr!("{} ms", number),
        // i18n: Settings, a time value; {} is the number, "min" is minutes (abbreviated)
        "min" => tr!("{} min", number),
        // i18n: Settings, a memory size; {} is the number, "MB" is megabytes
        "MB" => tr!("{} MB", number),
        unit => with_unit(&number, unit),
    }
}

/// A section's title as the settings page shows it, in the app's language
/// (`section` stays the English that identifies it).
pub fn section_title(section: &str) -> String {
    match section {
        // i18n: Settings page, section heading in capitals: chat settings (noun)
        "CHATS" => tr!("CHATS"),
        // i18n: Settings page, section heading in capitals: features still being tried (noun)
        "EXPERIMENTS" => tr!("EXPERIMENTS"),
        // i18n: Settings page, section heading in capitals: what happens when the app opens (noun)
        "STARTUP" => tr!("STARTUP"),
        // i18n: Settings page, section heading in capitals: whether new agents get a contact's name and persona
        "AGENT IDENTITY" => tr!("AGENT IDENTITY"),
        // i18n: Settings page, section heading in capitals: how the app looks (noun)
        "APPEARANCE" => tr!("APPEARANCE"),
        // i18n: Settings page, section heading in capitals: fonts and text sizes (noun)
        "TYPOGRAPHY" => tr!("TYPOGRAPHY"),
        // i18n: Settings page, section heading in capitals: spacing and placement of the window's parts (noun)
        "LAYOUT" => tr!("LAYOUT"),
        // i18n: Settings page, section heading in capitals: colour settings (noun)
        "COLOURS" => tr!("COLOURS"),
        // i18n: Settings page, section heading in capitals: spoken replies and voice settings
        "VOICE & AUDIO" => tr!("VOICE & AUDIO"),
        // i18n: Settings page, section heading in capitals: notification settings (noun)
        "NOTIFICATIONS" => tr!("NOTIFICATIONS"),
        // i18n: Settings page, section heading in capitals: how the app behaves (noun)
        "BEHAVIOUR" => tr!("BEHAVIOUR"),
        // i18n: Settings page, section heading in capitals: the Clarp Host this window talks to; Host is the product noun
        "HOST" => tr!("HOST"),
        // i18n: Settings page, section heading in capitals: the Host's tool processes kept running between messages
        "KEPT PROCESSES" => tr!("KEPT PROCESSES"),
        // i18n: Settings page, section heading in capitals: how the Host is doing (its diagnostics); Host is the product noun
        "HOST STATUS" => tr!("HOST STATUS"),
        // i18n: Settings page, section heading in capitals: keyboard settings and key bindings (noun)
        "KEYBOARD" => tr!("KEYBOARD"),
        // i18n: Settings page, section heading in capitals: the settings file and actions on every setting
        "ALL SETTINGS" => tr!("ALL SETTINGS"),
        // i18n: Settings page, section heading in capitals: the app's and the Host's versions
        "ABOUT" => tr!("ABOUT"),
        // i18n: Settings page, section heading in capitals: diagnostic recorders for reporting bugs (noun)
        "DIAGNOSTICS" => tr!("DIAGNOSTICS"),
        // i18n: Settings page, section heading in capitals: problems found in the settings file and the result of an export or import
        "SETTINGS FILE" => tr!("SETTINGS FILE"),
        other => local(other),
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
    "KEPT PROCESSES",
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
    e(
        "choose-font",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Picks the chat's typeface and size for this reading theme, previewed in the chat.
        en::tr!("Choose font…"),
        "appearance",
        // i18n: Ctrl+K command palette, the line under the command “Choose font…” saying what it does
        en::tr!("Picks the chat's typeface and size for this reading theme, previewed in the chat."),
        &["font", "typeface", "font family", "monospace", "serif", "sans"],
    ),
    e(
        "edit-keymap",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the editor for every keyboard shortcut.
        en::tr!("Customize key bindings"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Customize key bindings” saying what it does
        en::tr!("Opens the editor for every keyboard shortcut."),
        &["keymap", "shortcuts", "hotkeys", "keyboard", "customise", "rebind", "bindings"],
    ),
    e(
        "new-workspace",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens a new workspace tab with its own pane layout.
        en::tr!("New tab"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “New tab” saying what it does
        en::tr!("Opens a new workspace tab with its own pane layout."),
        &["new workspace", "tabnew", "add tab", "open tab", "workspace"],
    ),
    e(
        "next-workspace",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Switches to the next workspace tab.
        en::tr!("Next tab"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Next tab” saying what it does
        en::tr!("Switches to the next workspace tab."),
        &["next workspace", "tabnext", "cycle", "switch workspace", "tab"],
    ),
    e(
        "previous-workspace",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Switches to the workspace tab before this one.
        en::tr!("Previous tab"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Previous tab” saying what it does
        en::tr!("Switches to the workspace tab before this one."),
        &["previous workspace", "tabprevious", "back", "switch workspace", "tab"],
    ),
    e(
        "close-workspace",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Closes this workspace tab; its chats stay in the explorer.
        en::tr!("Close tab"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Close tab” saying what it does
        en::tr!("Closes this workspace tab; its chats stay in the explorer."),
        &["close workspace", "tabclose", "remove tab", "delete workspace", "tab"],
    ),
    e(
        "help-keys",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows the keys that matter where the keyboard is, your own bindings included.
        en::tr!("Keys here"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Keys here” saying what it does
        en::tr!("Shows the keys that matter where the keyboard is, your own bindings included."),
        &["help", "cheat sheet", "shortcuts", "which key", "keyboard help", "?"],
    ),
    e(
        "quick-new-agent",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Creates a new contact and opens a chat with it.
        en::tr!("New contact & chat"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “New contact & chat” saying what it does
        en::tr!("Creates a new contact and opens a chat with it."),
        &["new session", "hub", "create", "add contact", "new persona"],
    ),
    e(
        "rename-agent",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Changes the open contact's name.
        en::tr!("Rename contact"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Rename contact” saying what it does
        en::tr!("Changes the open contact's name."),
        &["rename", "name", "title", "relabel", "persona"],
    ),
    e(
        "new",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the hub to start a new agent.
        en::tr!("New session"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “New session” saying what it does
        en::tr!("Opens the hub to start a new agent."),
        &["new agent", "start", "chat", "provider", "contact", "hub", "create"],
    ),
    e(
        "preview-versions",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Installs another build of this app, newer or older.
        en::tr!("Preview versions · update or roll back"),
        "settings",
        // i18n: Ctrl+K command palette, the line under the command “Preview versions · update or roll back” saying what it does
        en::tr!("Installs another build of this app, newer or older."),
        &["previous installs", "rollback", "downgrade", "upgrade", "update", "version"],
    ),
    e(
        "new-contact",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Starts one of your idle contacts in a new session.
        en::tr!("Start an idle contact"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Start an idle contact” saying what it does
        en::tr!("Starts one of your idle contacts in a new session."),
        &["new session", "hub", "contact", "wake", "resume"],
    ),
    e(
        "agent-terminal",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the agent's CLI, run by its Host, in a terminal.
        en::tr!("Open agent in terminal"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Open agent in terminal” saying what it does
        en::tr!("Opens the agent's CLI, run by its Host, in a terminal."),
        &["terminal", "cli", "shell", "console", "native"],
    ),
    e(
        "split-right",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens a second pane to the right.
        en::tr!("Split right"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Split right” saying what it does
        en::tr!("Opens a second pane to the right."),
        &["vertical split", "side by side", "pane", "columns", "divide"],
    ),
    e(
        "split-down",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens a second pane below.
        en::tr!("Split down"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Split down” saying what it does
        en::tr!("Opens a second pane below."),
        &["horizontal split", "stack", "pane", "rows", "divide"],
    ),
    e(
        "close-pane",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Closes the active pane.
        en::tr!("Close pane"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Close pane” saying what it does
        en::tr!("Closes the active pane."),
        &["remove pane", "unsplit", "merge", "pane"],
    ),
    e(
        "zoom",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Fills the window with the active pane, or restores the split.
        en::tr!("Zoom pane"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Zoom pane” saying what it does
        en::tr!("Fills the window with the active pane, or restores the split."),
        &["maximise", "maximize", "fullscreen", "focus pane", "pane"],
    ),
    e(
        "balance",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Makes every pane the same size.
        en::tr!("Balance panes"),
        "layout",
        // i18n: Ctrl+K command palette, the line under the command “Balance panes” saying what it does
        en::tr!("Makes every pane the same size."),
        &["equalise", "equalize", "even", "resize", "panes"],
    ),
    e(
        "chat-zoom-in",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Draws the chats one step (10%) larger: text, code, cards and spacing, apart from the rest of the window.
        en::tr!("Zoom in"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Zoom in” saying what it does
        en::tr!("Draws the chats one step (10%) larger: text, code, cards and spacing, apart from the rest of the window."),
        &["magnify", "bigger", "larger", "enlarge", "text size", "chat zoom", "increase"],
    ),
    e(
        "chat-zoom-out",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Draws the chats one step (10%) smaller, apart from the rest of the window.
        en::tr!("Zoom out"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Zoom out” saying what it does
        en::tr!("Draws the chats one step (10%) smaller, apart from the rest of the window."),
        &["smaller", "shrink", "reduce", "text size", "chat zoom", "decrease"],
    ),
    e(
        "chat-zoom-reset",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Draws the chats at 100% again.
        en::tr!("Reset zoom"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Reset zoom” saying what it does
        en::tr!("Draws the chats at 100% again."),
        &["actual size", "100%", "normal size", "unzoom", "chat zoom", "default zoom"],
    ),
    e(
        "ui-larger",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Draws the whole window one step larger, the chats included.
        en::tr!("Larger interface"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Larger interface” saying what it does
        en::tr!("Draws the whole window one step larger, the chats included."),
        &["scale up", "bigger window", "increase", "window size", "dpi"],
    ),
    e(
        "ui-smaller",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Draws the whole window one step smaller, the chats included.
        en::tr!("Smaller interface"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Smaller interface” saying what it does
        en::tr!("Draws the whole window one step smaller, the chats included."),
        &["scale down", "smaller window", "shrink window", "window size", "dpi"],
    ),
    e(
        "ui-reset",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Returns the whole window to its usual scale.
        en::tr!("Reset interface size"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Reset interface size” saying what it does
        en::tr!("Returns the whole window to its usual scale."),
        &["default size", "actual size", "scale", "window size"],
    ),
    e(
        "toggle-queue",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Hides the strip of waiting messages above the composer, or shows it again (the setting Show the queue).
        en::tr!("Hide queue"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Hide queue” saying what it does
        en::tr!("Hides the strip of waiting messages above the composer, or shows it again (the setting Show the queue)."),
        &["queue strip", "waiting messages", "messages waiting", "show queue"],
    ),
    e(
        "send-queued",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Hands the open chat's first waiting message to the agent now: read into its running turn where the Host offers that (Steer now), else sent ahead of the queue.
        en::tr!("Send waiting message now"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Send waiting message now” saying what it does
        en::tr!("Hands the open chat's first waiting message to the agent now: read into its running turn where the Host offers that (Steer now), else sent ahead of the queue."),
        &["send now", "steer", "queue", "waiting", "interrupt", "now"],
    ),
    e(
        "list-all",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows every chat in the explorer.
        en::tr!("Agents: all chats"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agents: all chats” saying what it does
        en::tr!("Shows every chat in the explorer."),
        &["sidebar list", "scope", "everything", "back", "all agents"],
    ),
    e(
        "list-unread",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows only chats with unread messages in the explorer.
        en::tr!("Agents: unread only"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agents: unread only” saying what it does
        en::tr!("Shows only chats with unread messages in the explorer."),
        &["sidebar list", "filter", "new", "unread", "scope"],
    ),
    e(
        "list-working",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows only the chats whose agent is working now in the explorer.
        en::tr!("Agents: working"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agents: working” saying what it does
        en::tr!("Shows only the chats whose agent is working now in the explorer."),
        &["sidebar list", "filter", "busy", "at work", "triage", "scope"],
    ),
    e(
        "list-needs-you",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows only the chats waiting for you: unread, asking a question or holding a decision. Snoozed chats stay out.
        en::tr!("Agents: needs you"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agents: needs you” saying what it does
        en::tr!("Shows only the chats waiting for you: unread, asking a question or holding a decision. Snoozed chats stay out."),
        &["sidebar list", "filter", "attention", "waiting", "inbox", "triage", "scope"],
    ),
    e(
        "list-done",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows only the chats that are idle and read in the explorer.
        en::tr!("Agents: done"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agents: done” saying what it does
        en::tr!("Shows only the chats that are idle and read in the explorer."),
        &["sidebar list", "filter", "finished", "settled", "idle", "triage", "scope"],
    ),
    e(
        "list-snoozed",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows the chats you snoozed, with when each comes back.
        en::tr!("Agents: snoozed"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agents: snoozed” saying what it does
        en::tr!("Shows the chats you snoozed, with when each comes back."),
        &["sidebar list", "filter", "later", "put off", "triage", "scope"],
    ),
    e(
        "snooze-chat",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Takes the chat out of Needs you until it gets a new message. Kept on this device.
        en::tr!("Snooze chat until its next message"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Snooze chat until its next message” saying what it does
        en::tr!("Takes the chat out of Needs you until it gets a new message. Kept on this device."),
        &["snooze", "later", "remind", "hide", "defer", "triage"],
    ),
    e(
        "snooze-chat-hour",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Takes the chat out of Needs you for an hour. Kept on this device.
        en::tr!("Snooze chat for an hour"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Snooze chat for an hour” saying what it does
        en::tr!("Takes the chat out of Needs you for an hour. Kept on this device."),
        &["snooze", "later", "1 hour", "remind", "defer", "triage"],
    ),
    e(
        "snooze-chat-morning",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Takes the chat out of Needs you until 08:00 tomorrow. Kept on this device.
        en::tr!("Snooze chat until tomorrow morning"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Snooze chat until tomorrow morning” saying what it does
        en::tr!("Takes the chat out of Needs you until 08:00 tomorrow. Kept on this device."),
        &["snooze", "tomorrow", "morning", "later", "defer", "triage"],
    ),
    e(
        "wake-chat",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Ends the chat's snooze: it is back in Needs you if it waits for you.
        en::tr!("Wake snoozed chat"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Wake snoozed chat” saying what it does
        en::tr!("Ends the chat's snooze: it is back in Needs you if it waits for you."),
        &["unsnooze", "wake", "snooze", "back", "triage"],
    ),
    e(
        "snooze-toggle",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Snoozes the chat until its next message, or wakes it when snoozed.
        en::tr!("Snooze or wake chat"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Snooze or wake chat” saying what it does
        en::tr!("Snoozes the chat until its next message, or wakes it when snoozed."),
        &["snooze", "wake", "toggle", "later", "triage"],
    ),
    e(
        "list-rooms",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows the rooms where agents talk to each other.
        en::tr!("Agent conversations (agent to agent)"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agent conversations (agent to agent)” saying what it does
        en::tr!("Shows the rooms where agents talk to each other."),
        &["pairs", "rooms", "sidebar", "agent to agent", "a2a"],
    ),
    e(
        "list-archive",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows archived agents so you can restore them.
        en::tr!("Archived agents"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Archived agents” saying what it does
        en::tr!("Shows archived agents so you can restore them."),
        &["archive", "restore", "old", "sidebar", "history"],
    ),
    e(
        "link-hints",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Numbers the links on screen; type a number to open one.
        en::tr!("Open a link"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Open a link” saying what it does
        en::tr!("Numbers the links on screen; type a number to open one."),
        &["links", "hints", "url", "urls", "browser", "follow", "vimium", "web"],
    ),
    e(
        "jump-latest",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Scrolls the chat to its newest message.
        en::tr!("Jump to latest"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Jump to latest” saying what it does
        en::tr!("Scrolls the chat to its newest message."),
        &["bottom", "newest", "scroll", "follow", "end"],
    ),
    e(
        "retry-message",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Sends the last message that failed again.
        en::tr!("Retry latest failed message"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Retry latest failed message” saying what it does
        en::tr!("Sends the last message that failed again."),
        &["resend", "send", "delivery", "not delivered", "retry"],
    ),
    e(
        "dismiss-error",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Closes the error banner above the chat.
        en::tr!("Dismiss conversation error"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Dismiss conversation error” saying what it does
        en::tr!("Closes the error banner above the chat."),
        &["clear", "close", "error", "warning", "banner", "voice synthesis failed"],
    ),
    e(
        "dismiss-layout-warning",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Closes the warning that another window saved a newer layout.
        en::tr!("Dismiss layout warning"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Dismiss layout warning” saying what it does
        en::tr!("Closes the warning that another window saved a newer layout."),
        &["another window", "newer layout", "conflict", "recovery", "close"],
    ),
    e(
        "keep-layout",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Saves this window's layout over the newer one another window saved.
        en::tr!("Keep this window's layout"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Keep this window's layout” saying what it does
        en::tr!("Saves this window's layout over the newer one another window saved."),
        &["another window", "newer layout", "conflict", "recovery", "save instead"],
    ),
    e(
        "change-directory",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Starts a new chat in another folder.
        en::tr!("Change directory"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Change directory” saying what it does
        en::tr!("Starts a new chat in another folder."),
        &["folder", "workspace", "cwd", "new chat", "path"],
    ),
    e(
        "recent-agents",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Lists the agents you opened last, newest first.
        en::tr!("Recent agents"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Recent agents” saying what it does
        en::tr!("Lists the agents you opened last, newest first."),
        &["last", "previous", "switch back", "mru", "history"],
    ),
    e(
        "show-work",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens or folds the work of a finished turn in Conversation view (the message the keyboard is on, else the latest turn), or the running turn's Running box.
        en::tr!("Show work"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Show work” saying what it does
        en::tr!("Opens or folds the work of a finished turn in Conversation view (the message the keyboard is on, else the latest turn), or the running turn's Running box."),
        &["hide work", "conversation view", "tool activity", "steps", "what ran", "expand turn"],
    ),
    e(
        "chat-view",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows the open chat the other way: Conversation (your messages, the replies, cards; the work behind Show work) or Session (everything as it ran). On a Host that offers it.
        en::tr!("Conversation or Session view"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Conversation or Session view” saying what it does
        en::tr!("Shows the open chat the other way: Conversation (your messages, the replies, cards; the work behind Show work) or Session (everything as it ran). On a Host that offers it."),
        &["conversation view", "session view", "chat view", "clean chat", "only replies"],
    ),
    e(
        "select-text",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows the open chat as plain text to select from with the mouse or Shift+arrows and copy with Ctrl+C; a message's own select icon shows just that message.
        en::tr!("Select text"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Select text” saying what it does
        en::tr!("Shows the open chat as plain text to select from with the mouse or Shift+arrows and copy with Ctrl+C; a message's own select icon shows just that message."),
        &["select", "highlight", "copy text", "copy part", "mark text", "visual"],
    ),
    e(
        "find-in-chat",
        // i18n: Ctrl+K command palette, a command's name (an action). What it does: opens the find bar of the open chat (Ctrl+F) to find words in its messages.
        en::tr!("Search in chat"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Search in chat” saying what it does
        en::tr!("Finds words in the open chat: every message holding them is marked, the bar says which match of how many shows, and up and down (Enter, Shift+Enter) go to the older and newer one."),
        &["find", "find in chat", "search this chat", "in this chat", "highlight", "ctrl+f"],
    ),
    e(
        "search-messages",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Finds words in the messages of every chat (the Host searches them all where it can, else this computer's chats) and jumps to the message.
        en::tr!("Search messages"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Search messages” saying what it does
        en::tr!("Finds words in the messages of every chat (the Host searches them all where it can, else this computer's chats) and jumps to the message."),
        &["find", "find in chats", "text", "history", "transcript", "grep", "look up"],
    ),
    e(
        "open-in-browser",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens an HTML report's whole page, pictures and layout included, in a new browser window; Clarp shows plain ones as a quick preview.
        en::tr!("Open report in browser"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Open report in browser” saying what it does
        en::tr!("Opens an HTML report's whole page, pictures and layout included, in a new browser window; Clarp shows plain ones as a quick preview."),
        &["browser", "full page", "images", "html", "web page", "external"],
    ),
    e(
        "refresh",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Loads the open chat again from the Host.
        en::tr!("Refresh conversation"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Refresh conversation” saying what it does
        en::tr!("Loads the open chat again from the Host."),
        &["reload", "update", "sync", "fetch"],
    ),
    e(
        "refresh-agents",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Fetches the agent list from the Host now, also while it is retrying after a failure.
        en::tr!("Refresh agents"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Refresh agents” saying what it does
        en::tr!("Fetches the agent list from the Host now, also while it is retrying after a failure."),
        &["reload", "refresh", "roster", "sync", "agent list", "contacts", "stale"],
    ),
    e(
        "agent-processes",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows the selected agent's background jobs and running helpers: open a job's output, stop a job or a helper. On Updates, every job.
        en::tr!("Background processes"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Background processes” saying what it does
        en::tr!("Shows the selected agent's background jobs and running helpers: open a job's output, stop a job or a helper. On Updates, every job."),
        &["processes", "jobs", "background jobs", "tasks", "running", "helpers", "kill", "stop job", "output", "logs", "show processes"],
    ),
    e(
        "overview",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Shows every agent and what it is doing.
        en::tr!("Agent overview"),
        "view",
        // i18n: Ctrl+K command palette, the line under the command “Agent overview” saying what it does
        en::tr!("Shows every agent and what it is doing."),
        &["dashboard", "summary", "all agents", "status", "fleet"],
    ),
    e(
        "agent-profile",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the open agent's profile: its artifacts and images, files, terminal, voice, plan, prompt history, model and backend.
        en::tr!("Agent profile"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Agent profile” saying what it does
        en::tr!("Opens the open agent's profile: its artifacts and images, files, terminal, voice, plan, prompt history, model and backend."),
        &["profile", "agent info", "details", "about agent", "artifacts", "media", "images", "agent model", "backend"],
    ),
    e(
        "chats",
        // i18n: Ctrl+K command palette, a command's name (a noun: the place in the app it goes to). What it does: Goes to the chats.
        en::tr!("Chats"),
        "destination",
        // i18n: Ctrl+K command palette, the line under the command “Chats” saying what it does
        en::tr!("Goes to the chats."),
        &["conversations", "messages", "home", "main"],
    ),
    e(
        "updates",
        // i18n: Ctrl+K command palette, a command's name (a noun: the place in the app it goes to). What it does: Goes to the updates feed.
        en::tr!("Updates"),
        "destination",
        // i18n: Ctrl+K command palette, the line under the command “Updates” saying what it does
        en::tr!("Goes to the updates feed."),
        &["feed", "activity", "news", "attention", "inbox"],
    ),
    e(
        "teams",
        // i18n: Ctrl+K command palette, a command's name (a noun: the place in the app it goes to). What it does: Goes to the teams.
        en::tr!("Teams"),
        "destination",
        // i18n: Ctrl+K command palette, the line under the command “Teams” saying what it does
        en::tr!("Goes to the teams."),
        &["groups", "team", "crews", "squads"],
    ),
    e(
        "settings",
        // i18n: Ctrl+K command palette, a command's name (a noun: the place in the app it goes to). What it does: Opens the settings page.
        en::tr!("Settings"),
        "destination",
        // i18n: Ctrl+K command palette, the line under the command “Settings” saying what it does
        en::tr!("Opens the settings page."),
        &["preferences", "options", "configuration", "config", "customise", "customize"],
    ),
    e(
        "next-attention",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the next agent that is waiting for you.
        en::tr!("Next agent needing attention"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Next agent needing attention” saying what it does
        en::tr!("Opens the next agent that is waiting for you."),
        &["attention", "waiting", "blocked", "needs me", "next"],
    ),
    e(
        "next-agent",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the next agent in the list.
        en::tr!("Next agent"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Next agent” saying what it does
        en::tr!("Opens the next agent in the list."),
        &["following", "chat", "switch", "forward", "down"],
    ),
    e(
        "previous-agent",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Opens the agent above in the list.
        en::tr!("Previous agent"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Previous agent” saying what it does
        en::tr!("Opens the agent above in the list."),
        &["back", "chat", "switch", "prior", "up"],
    ),
    e(
        "release-agent",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Ends the open agent's session and frees its contact.
        en::tr!("Release agent"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Release agent” saying what it does
        en::tr!("Ends the open agent's session and frees its contact."),
        &["end", "free", "finish", "close agent", "dismiss"],
    ),
    e(
        "stop-agent",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Interrupts what the open agent is doing.
        en::tr!("Stop agent"),
        "agent",
        // i18n: Ctrl+K command palette, the line under the command “Stop agent” saying what it does
        en::tr!("Interrupts what the open agent is doing."),
        &["interrupt", "cancel", "halt", "abort", "kill"],
    ),
    e(
        "talk",
        // i18n: Ctrl+K command palette, a command's name (an action; a verb phrase is a command, a noun phrase names what it opens). What it does: Records your voice and sends it to the open agent.
        en::tr!("Talk"),
        "audio",
        // i18n: Ctrl+K command palette, the line under the command “Talk” saying what it does
        en::tr!("Records your voice and sends it to the open agent."),
        &["voice", "microphone", "mic", "dictate", "speak", "record", "push to talk"],
    ),
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
        // The NOTIFICATIONS section answers "notifications"; its switch for
        // the phone by its own words.
        top3("notifications", "desktop-notifications");
        top3("pause phone", "pause-mobile-push");
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
