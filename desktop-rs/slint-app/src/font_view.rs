//! The font picker (Settings → Font, Ctrl+K "Choose font…"): the installed
//! families beside the chat, which shows the current one as the keyboard
//! moves. Enter keeps it as the reading theme's own font, Escape puts back
//! what was there, Ctrl+R returns to the theme's default. The choice is the
//! theme's alone: every theme remembers its own.

use std::cell::RefCell;
use std::rc::Rc;

use clarp_core::reading_theme::{self as reading, FontFamily, FontOverride};
use slint::{ComponentHandle, ModelRc, VecModel};

use crate::{App, AppWindow, FontBridge, FontItem, FontKey, Hint, commands};

pub const OVERLAY: &str = "fonts";

#[derive(Default)]
struct Picker {
    open: bool,
    theme: String,
    /// The theme's own font when the picker opened (Escape returns to it).
    saved: Option<FontOverride>,
    fonts: Vec<FontFamily>,
    query: String,
    monospace_only: bool,
    /// The families the query and the filter leave.
    shown: Vec<FontFamily>,
    current: usize,
    /// The size being previewed (None: the theme's).
    size: Option<f64>,
    /// The surface it was opened from, to go back to.
    from: String,
}

thread_local! {
    static PICKER: RefCell<Picker> = RefCell::new(Picker::default());
}

/// The picker's keys: (keys shown, label, action, chords).
const KEYS: &[(&str, &str, &str, &[&str])] = &[
    ("↑↓", "Move", "move", &["Up", "Down"]),
    ("Enter", "Choose", "choose", &["Return"]),
    ("Ctrl+M", "Monospace only", "monospace", &["Ctrl+M"]),
    ("Ctrl+↑↓", "Size", "size", &["Ctrl+Up", "Ctrl+Down"]),
    ("Ctrl+R", "Theme default", "reset", &["Ctrl+R"]),
    ("Esc", "Cancel", "cancel", &["Escape"]),
];

pub fn is_open() -> bool {
    PICKER.with(|p| p.borrow().open)
}

/// The font the chat shows while the picker is open: the current family
/// and size.
pub fn previewed() -> Option<FontOverride> {
    PICKER.with(|p| {
        let p = p.borrow();
        if !p.open {
            return None;
        }
        let family = match p.shown.get(p.current) {
            Some(font) => font.name.clone(),
            None => p.saved.as_ref().map(|s| s.family.clone()).unwrap_or_default(),
        };
        Some(FontOverride { family, size: p.size })
    })
}

fn theme_size(theme: &str) -> f64 {
    reading::theme(theme).get("fontPixelSize").and_then(serde_json::Value::as_f64).unwrap_or(15.0)
}

/// Opens the picker for the current theme, on the family it uses now.
pub fn open(app: &Rc<App>, window: &AppWindow) {
    let (theme, saved) = {
        let engine = app.engine.borrow();
        let theme = engine.reading_theme();
        let saved = engine.font_override(&theme);
        (theme, saved)
    };
    let fonts = crate::view::font_list();
    let using = crate::view::resolved_font(reading::theme(&theme), saved.as_ref()).family;
    let from = window.get_surface().to_string();
    PICKER.with(|p| {
        let mut p = p.borrow_mut();
        *p = Picker { open: true, theme, size: saved.as_ref().and_then(|s| s.size), saved, fonts, from, ..Picker::default() };
        filter(&mut p, Some(&using));
    });
    // The chat shows the font as it changes.
    if window.get_surface() != "chats" {
        window.set_surface("chats".into());
    }
    commands::open_overlay(app, window, OVERLAY);
    window.invoke_open_font_picker();
    show(app, window);
}

/// The families the query and the filter leave, on `keep` if it is one.
fn filter(p: &mut Picker, keep: Option<&str>) {
    let keep = keep.map(str::to_owned).or_else(|| p.shown.get(p.current).map(|f| f.name.clone()));
    p.shown = reading::filter_fonts(&p.fonts, &p.query, p.monospace_only).into_iter().cloned().collect();
    p.current = keep.and_then(|name| p.shown.iter().position(|f| f.name == name)).unwrap_or(0);
}

/// Shows the picker's state and the chat in the current font.
fn show(app: &App, window: &AppWindow) {
    let bridge = window.global::<FontBridge>();
    PICKER.with(|p| {
        let p = p.borrow();
        let label = reading::theme(&p.theme).get("label").and_then(serde_json::Value::as_str).unwrap_or_default().to_owned();
        bridge.set_title(format!("Font · {label} theme").into());
        let saved = p.saved.as_ref().map(|s| s.family.as_str()).unwrap_or_default();
        let items: Vec<FontItem> =
            p.shown.iter().map(|f| FontItem { name: f.name.as_str().into(), monospace: f.monospace, saved: f.name == saved }).collect();
        bridge.set_fonts(ModelRc::new(VecModel::from(items)));
        bridge.set_current(if p.shown.is_empty() { -1 } else { p.current as i32 });
        bridge.set_monospace_only(p.monospace_only);
        let default = theme_size(&p.theme);
        let size = match p.size {
            Some(size) if size != default => format!("{size} px (theme {default} px)"),
            _ => format!("{default} px"),
        };
        bridge.set_size_text(size.into());
        let missing = p.saved.as_ref().filter(|s| !s.family.is_empty() && !p.fonts.iter().any(|f| f.name == s.family));
        bridge.set_note(missing.map_or_else(String::new, |s| format!("{} is not installed; the theme's own font is used", s.family)).into());
        let theme_font = crate::view::theme_font(reading::theme(&p.theme));
        let keys: Vec<FontKey> = KEYS
            .iter()
            .map(|(keys, label, action, _)| FontKey {
                keys: (*keys).into(),
                label: (if *action == "reset" { format!("Reset to theme default ({theme_font} {default} px)") } else { (*label).to_owned() }).into(),
                action: (*action).into(),
            })
            .filter(|k| k.action != "move")
            .collect();
        bridge.set_actions(ModelRc::new(VecModel::from(keys)));
    });
    crate::view::apply_app_theme(app, window);
}

/// The shortcut bar while the picker has the keyboard.
pub fn hints() -> Vec<Hint> {
    KEYS.iter().map(|(keys, label, _, _)| Hint { keys: (*keys).into(), label: (*label).into() }).collect()
}

/// A key while the picker is open: true when it was the picker's (letters
/// type into the search).
pub fn key(app: &Rc<App>, window: &AppWindow, chord: &str) -> bool {
    let action = match chord {
        "Up" => "up",
        "Down" => "down",
        "PageUp" => "page-up",
        "PageDown" => "page-down",
        "Ctrl+Home" => "first",
        "Ctrl+End" => "last",
        "Ctrl+Up" => "larger",
        "Ctrl+Down" => "smaller",
        _ => match KEYS.iter().find(|(_, _, _, chords)| chords.contains(&chord)) {
            Some((_, _, action, _)) => *action,
            None => return false,
        },
    };
    act(app, window, action);
    true
}

/// Runs one of the picker's actions (a key, or a click on its hint).
pub fn act(app: &Rc<App>, window: &AppWindow, action: &str) {
    if !is_open() {
        return;
    }
    match action {
        "choose" => return finish(app, window, Finish::Choose),
        "cancel" => return finish(app, window, Finish::Cancel),
        "reset" => return finish(app, window, Finish::Reset),
        "up" => step(-1),
        "down" => step(1),
        "page-up" => step(-10),
        "page-down" => step(10),
        "first" => step(i64::MIN / 2),
        "last" => step(i64::MAX / 2),
        "monospace" => PICKER.with(|p| {
            let mut p = p.borrow_mut();
            p.monospace_only = !p.monospace_only;
            filter(&mut p, None);
        }),
        "larger" | "smaller" | "size" => PICKER.with(|p| {
            let mut p = p.borrow_mut();
            let current = p.size.unwrap_or_else(|| theme_size(&p.theme));
            let next = (current + if action == "smaller" { -1.0 } else { 1.0 }).clamp(9.0, 40.0);
            p.size = if next == theme_size(&p.theme) { None } else { Some(next) };
        }),
        other => {
            eprintln!("clarp-slint: the font picker has no action {other}");
            return;
        }
    }
    show(app, window);
}

/// Moves the current family by `delta`, within the list.
fn step(delta: i64) {
    PICKER.with(|p| {
        let mut p = p.borrow_mut();
        let last = p.shown.len().saturating_sub(1) as i64;
        p.current = (p.current as i64).saturating_add(delta).clamp(0, last) as usize;
    });
}

/// The search text changed.
pub fn edited(app: &Rc<App>, window: &AppWindow, text: &str) {
    PICKER.with(|p| {
        let mut p = p.borrow_mut();
        p.query = text.to_owned();
        // The first match is the one to look at.
        filter(&mut p, Some(""));
    });
    show(app, window);
}

/// A click on a family.
pub fn moved(app: &Rc<App>, window: &AppWindow, index: i32) {
    PICKER.with(|p| {
        let mut p = p.borrow_mut();
        if let Some(index) = usize::try_from(index).ok().filter(|i| *i < p.shown.len()) {
            p.current = index;
        }
    });
    show(app, window);
}

enum Finish {
    Choose,
    Cancel,
    Reset,
}

fn finish(app: &Rc<App>, window: &AppWindow, how: Finish) {
    let (theme, value, from) = PICKER.with(|p| {
        let mut p = p.borrow_mut();
        let value = match how {
            Finish::Choose => {
                let family = p.shown.get(p.current).map(|f| f.name.clone()).or_else(|| p.saved.as_ref().map(|s| s.family.clone())).unwrap_or_default();
                Some(FontOverride { family, size: p.size }).filter(|v| !v.family.is_empty() || v.size.is_some())
            }
            Finish::Cancel => p.saved.clone(),
            Finish::Reset => None,
        };
        p.open = false;
        (p.theme.clone(), value, std::mem::take(&mut p.from))
    });
    app.engine.borrow_mut().set_font_override(&theme, value);
    if from != "chats" && !from.is_empty() {
        window.set_surface(from.as_str().into());
    }
    commands::close_overlay(app, window);
    crate::pump_now(app);
    crate::view::apply_app_theme(app, window);
    crate::settings_view::show(app, window);
}

pub fn wire(window: &AppWindow) {
    let bridge = window.global::<FontBridge>();
    bridge.on_edited(|text| with(|app, window| edited(app, window, &text)));
    bridge.on_moved(|index| with(|app, window| moved(app, window, index)));
    bridge.on_act(|action| with(|app, window| act(app, window, &action)));
}

fn with(act: impl FnOnce(&Rc<App>, &AppWindow)) {
    if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
        act(&app, &window);
    }
}
