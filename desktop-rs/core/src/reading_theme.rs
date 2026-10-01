//! Reading themes for the transcript, message text, composer and chrome.
//! Port of the C++ client's `ReadingTheme.h`; the palette is
//! `reading_themes.json`, generated from that header so no value is retyped.
//! Choices follow docs/reading-themes.md (AAA body text, AA secondary text,
//! no pure polarity, a 60-110 character measure).

use std::sync::LazyLock;

use serde_json::{Map, Value, json};

use crate::json::Object;

static THEMES: LazyLock<Vec<Object>> = LazyLock::new(|| {
    serde_json::from_str::<Vec<Object>>(include_str!("reading_themes.json")).expect("reading_themes.json parses")
});

pub fn themes() -> &'static [Object] {
    &THEMES
}

pub fn default_theme_id() -> &'static str {
    "terminal"
}

/// The theme with `id`, or the terminal default.
pub fn theme(id: &str) -> &'static Object {
    themes().iter().find(|t| t.get("id").and_then(Value::as_str) == Some(id)).unwrap_or(&themes()[0])
}

pub fn normalized_theme_id(id: &str) -> String {
    theme(id).get("id").and_then(Value::as_str).unwrap_or_default().to_owned()
}

fn families(theme: &Object) -> Vec<&str> {
    theme.get("fontFamilies").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).collect()).unwrap_or_default()
}

/// The first installed family; generic CSS names are always accepted so
/// Qt's own matching can take over.
pub fn resolve_font(theme: &Object, installed: impl Fn(&str) -> bool) -> String {
    let families = families(theme);
    families
        .iter()
        .find(|f| matches!(**f, "serif" | "sans-serif" | "monospace") || installed(f))
        .or(families.last())
        .map_or_else(|| "JetBrains Mono".to_owned(), |f| (*f).to_owned())
}

/// Everything QML styles from: the palette plus the resolved font.
pub fn style(id: &str, installed: impl Fn(&str) -> bool) -> Object {
    let theme = theme(id);
    let mut style: Map<String, Value> = theme.clone();
    style.remove("fontFamilies");
    style.remove("detail");
    style.insert("fontFamily".into(), Value::from(resolve_font(theme, installed)));
    style
}

pub fn options() -> Vec<Value> {
    themes()
        .iter()
        .map(|t| json!({"id": t["id"], "label": t["label"], "detail": t["detail"], "fontFamily": families(t).first().copied().unwrap_or_default()}))
        .collect()
}

/// `#rrggbb` or `#aarrggbb` → (r, g, b) in 0..=1.
pub fn rgb(color: &str) -> Option<(f64, f64, f64)> {
    let hex = color.strip_prefix('#')?;
    let hex = match hex.len() {
        6 => hex,
        8 => &hex[2..],
        _ => return None,
    };
    let channel = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).ok().map(|v| f64::from(v) / 255.0);
    Some((channel(0)?, channel(2)?, channel(4)?))
}

fn luminance(color: &str) -> f64 {
    let (r, g, b) = rgb(color).unwrap_or((0.0, 0.0, 0.0));
    let linear = |v: f64| if v <= 0.03928 { v / 12.92 } else { ((v + 0.055) / 1.055).powf(2.4) };
    0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

/// WCAG 2.x contrast ratio.
pub fn contrast(foreground: &str, background: &str) -> f64 {
    let (a, b) = (luminance(foreground), luminance(background));
    (a.max(b) + 0.05) / (a.min(b) + 0.05)
}

/// HSL lightness, as QColor::lightnessF.
pub fn lightness(color: &str) -> f64 {
    let (r, g, b) = rgb(color).unwrap_or((0.0, 0.0, 0.0));
    (r.max(g).max(b) + r.min(g).min(b)) / 2.0
}
