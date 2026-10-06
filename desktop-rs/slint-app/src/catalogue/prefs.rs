//! Every preference in `clarp_core::prefs` the catalogue does not register
//! itself, as catalogue settings: a switch, a choice (a number's steps are
//! its choices), or an action that opens the value editor (a colour, a
//! font); then the settings file's own actions. `look.rs` applies them to
//! the window and keeps the editor, the file and `:set`.

use clarp_core::prefs::{self, Kind, Spec};

use super::{Choice, Entry, Setting, e};
use crate::App;
use crate::look;

/// A number as a choice's value: "15", "1.5".
fn num(value: f64) -> String {
    let value = (value * 1000.0).round() / 1000.0;
    if value.fract() == 0.0 { format!("{value:.0}") } else { format!("{value}") }
}

/// A number's steps as choices (and the current value, when it is off the
/// grid after a hand edit).
fn steps(spec: &Spec, current: f64) -> Vec<Choice> {
    let Kind::Number { default, min, max, step, zero, .. } = spec.kind else { return Vec::new() };
    let mut values: Vec<f64> = zero.map(|_| 0.0).into_iter().collect();
    let count = ((max - min) / step).round() as i64;
    values.extend((0..=count).map(|i| ((min + step * i as f64) * 1000.0).round() / 1000.0));
    if !values.iter().any(|v| (v - current).abs() < 1e-9) {
        values.push(current);
        values.sort_by(f64::total_cmp);
    }
    values
        .into_iter()
        .map(|v| {
            let choice = Choice::new(num(v), prefs::display(spec, &serde_json::json!(v)));
            if (v - default).abs() < 1e-9 { choice.about("The default") } else { choice }
        })
        .collect()
}

/// The page's section and the catalogue group of a preference section.
fn section_of(section: &str) -> (&'static str, &'static str) {
    match section {
        prefs::TYPOGRAPHY => ("TYPOGRAPHY", "typography"),
        prefs::LAYOUT => ("LAYOUT", "layout"),
        prefs::CHAT => ("CHATS", "chats"),
        prefs::COLOURS => ("COLOURS", "colours"),
        _ => ("BEHAVIOUR", "behaviour"),
    }
}

/// Every preference the catalogue does not register itself, as catalogue
/// settings: a switch, a choice (a number's steps are its choices), or an
/// action that opens the value editor (a colour, a font); then the
/// settings file's own actions.
pub(super) fn register() -> Vec<Setting> {
    let mut out = Vec::new();
    for spec in prefs::specs() {
        if look::CATALOGUED.iter().any(|(name, _)| *name == spec.name) {
            continue;
        }
        let (section, group) = section_of(spec.section);
        let entry = Entry { id: spec.name, label: spec.label, description: spec.description, aliases: spec.aliases, group, actions: &[] };
        let name = spec.name;
        let setting = match spec.kind {
            Kind::Toggle { default, .. } => Setting::toggle(
                entry,
                section,
                move |app| prefs::flag(app.engine.borrow().settings(), name),
                move |app, window, on| look::set_pref(app, window, name, if on { "on" } else { "off" }),
            )
            .default(if default { "on" } else { "off" }),
            Kind::Choice { default, .. } => Setting::choice(
                entry,
                section,
                move |_| match spec.kind {
                    Kind::Choice { options, .. } => options.iter().map(|(id, label)| Choice::new(*id, *label)).collect(),
                    _ => Vec::new(),
                },
                move |app| prefs::choice_of(app.engine.borrow().settings(), name),
                move |app, window, value| look::set_pref(app, window, name, value),
            )
            .default(default),
            Kind::Number { default, .. } => Setting::choice(
                entry,
                section,
                move |app| steps(spec, prefs::number_of(app.engine.borrow().settings(), name)),
                move |app| num(prefs::number_of(app.engine.borrow().settings(), name)),
                move |app, window, value| look::set_pref(app, window, name, value),
            )
            .default(num(default)),
            Kind::Color { .. } | Kind::Text { .. } => Setting::action(entry, section, move |app, window| look::open_editor(app, window, name))
                .detail(move |app| prefs::shown(app.engine.borrow().settings(), spec)),
        };
        out.push(setting);
    }
    let path = |app: &App| app.engine.borrow().settings().path().map_or_else(|| "In memory only".to_owned(), |p| p.display().to_string());
    out.push(
        Setting::action(
            e("pref-edit-file", "Edit the settings file", "settings", "Opens settings.json in your editor, with its schema and reference beside it; saved changes apply at once.", &["settings file", "config file", "json", "editor", "dotfile"]),
            "ALL SETTINGS",
            |app, _| {
                look::edit_file(app);
            },
        )
        .detail(path),
    );
    out.push(
        Setting::action(
            e("pref-export", "Export settings", "settings", "Writes every preference to ~/Downloads/clarp-settings.json to keep or share.", &["backup", "save settings", "share settings", "copy settings"]),
            "ALL SETTINGS",
            |app, window| {
                look::export(app, window);
            },
        )
        .detail(|_| look::export_path().display().to_string()),
    );
    out.push(Setting::action(
        e("pref-import", "Import settings…", "settings", "Applies the valid preferences of a settings file exported from Clarp and reports the rest.", &["restore", "load settings", "apply settings", "from file"]),
        "ALL SETTINGS",
        look::open_import,
    ));
    out.push(Setting::action(
        e("pref-reset-all", "Reset every setting", "settings", "Puts every preference back to its default; key bindings and chats stay.", &["defaults", "factory reset", "reset all", "restore defaults"]),
        "ALL SETTINGS",
        |app, window| {
            look::run(app, window, "pref-reset-all");
        },
    ));
    out
}
