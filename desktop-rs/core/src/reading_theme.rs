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

/// Where the reader's own fonts are kept: `{themeId: {"family", "size"}}`.
pub const FONT_OVERRIDES_KEY: &str = "appearance/fontOverrides";

/// The reader's own font for one theme (Settings → Font), over the theme's.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct FontOverride {
    /// Empty keeps the theme's family.
    pub family: String,
    /// None keeps the theme's size.
    pub size: Option<f64>,
}

/// `theme_id`'s override in the stored overrides, if it has one.
pub fn font_override(overrides: Option<&Value>, theme_id: &str) -> Option<FontOverride> {
    let entry = overrides?.get(theme_id)?;
    let family = entry.get("family").and_then(Value::as_str).unwrap_or_default().trim().to_owned();
    let size = entry.get("size").and_then(Value::as_f64).filter(|s| *s > 0.0);
    (!family.is_empty() || size.is_some()).then_some(FontOverride { family, size })
}

/// The stored overrides with `theme_id`'s set (or removed, for None); the
/// other themes' stay as they were.
pub fn with_font_override(overrides: Option<&Value>, theme_id: &str, value: Option<&FontOverride>) -> Value {
    let mut all = overrides.and_then(Value::as_object).cloned().unwrap_or_default();
    match value.filter(|v| !v.family.is_empty() || v.size.is_some()) {
        Some(chosen) => {
            let mut entry = Map::new();
            if !chosen.family.is_empty() {
                entry.insert("family".into(), Value::from(chosen.family.clone()));
            }
            if let Some(size) = chosen.size {
                entry.insert("size".into(), Value::from(size));
            }
            all.insert(theme_id.to_owned(), Value::Object(entry));
        }
        None => {
            all.remove(theme_id);
        }
    }
    Value::Object(all)
}

/// The font a theme draws in once the reader's override is applied.
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvedFont {
    pub family: String,
    pub size: f64,
    /// The chosen family, when it is not installed (the theme's own
    /// fallbacks are used instead).
    pub missing: Option<String>,
}

/// The chosen family when it is installed, else the theme's first installed
/// family; the chosen size, else the theme's.
pub fn resolve(theme: &Object, chosen: Option<&FontOverride>, installed: impl Fn(&str) -> bool) -> ResolvedFont {
    let size = chosen.and_then(|c| c.size).or_else(|| theme.get("fontPixelSize").and_then(Value::as_f64)).unwrap_or(15.0);
    let wanted = chosen.map(|c| c.family.as_str()).filter(|f| !f.is_empty());
    match wanted {
        Some(family) if matches!(family, "serif" | "sans-serif" | "monospace") || installed(family) => {
            ResolvedFont { family: family.to_owned(), size, missing: None }
        }
        _ => ResolvedFont { family: resolve_font(theme, installed), size, missing: wanted.map(str::to_owned) },
    }
}

/// One installed family, as the font picker lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FontFamily {
    pub name: String,
    pub monospace: bool,
}

/// `fc-list -f '%{family}\t%{spacing}\n'` as families, sorted and once each:
/// a family is monospace when its faces are (fontconfig spacing 90 or more).
pub fn parse_font_list(output: &str) -> Vec<FontFamily> {
    let mut families: std::collections::BTreeMap<String, bool> = std::collections::BTreeMap::new();
    for line in output.lines() {
        let (names, spacing) = line.split_once('\t').unwrap_or((line, ""));
        let Some(name) = names.split(',').map(str::trim).find(|n| !n.is_empty()) else { continue };
        let monospace = spacing.trim().parse::<i32>().is_ok_and(|s| s >= 90);
        *families.entry(name.to_owned()).or_default() |= monospace;
    }
    let mut fonts: Vec<FontFamily> = families.into_iter().map(|(name, monospace)| FontFamily { name, monospace }).collect();
    fonts.sort_by_cached_key(|f| (f.name.to_lowercase(), f.name.clone()));
    fonts
}

/// The families whose names hold every word of `query`, monospace ones only
/// if asked.
pub fn filter_fonts<'a>(fonts: &'a [FontFamily], query: &str, monospace_only: bool) -> Vec<&'a FontFamily> {
    let words: Vec<String> = query.split_whitespace().map(str::to_lowercase).collect();
    fonts
        .iter()
        .filter(|f| !monospace_only || f.monospace)
        .filter(|f| {
            let name = f.name.to_lowercase();
            words.iter().all(|w| name.contains(w.as_str()))
        })
        .collect()
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
