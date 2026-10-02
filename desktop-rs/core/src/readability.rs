//! The readability rules every reading theme must meet, and the colour
//! science they are measured with: APCA lightness contrast (the WCAG 3 draft
//! method), the WCAG 2 ratio, alpha compositing of the `#aarrggbb` tints the
//! UI draws (the keyboard cursor, field selections), colour-vision deficiency
//! simulation (Machado, Oliveira & Fernandes 2009, severity 1.0) and the
//! CIEDE2000 colour difference.
//!
//! The pairs below are the ones the Slint UI actually draws (see the
//! comments on each), not every combination of roles; `background`,
//! `secondary`, `selection`, `selectedText`, `shadow` and `scrim` are kept
//! for completeness but the Slint UI does not draw them. The rationale and
//! the sources are in docs/readability.md.

use serde_json::Value;

use crate::json::Object;
use crate::reading_theme::{contrast, rgb, themes};

/// An sRGB colour, channels 0..=1.
pub type Rgb = (f64, f64, f64);

fn parse(color: &str) -> Option<(Rgb, f64)> {
    let hex = color.strip_prefix('#')?;
    let alpha = match hex.len() {
        6 => 1.0,
        8 => f64::from(u8::from_str_radix(&hex[..2], 16).ok()?) / 255.0,
        _ => return None,
    };
    Some((rgb(color)?, alpha))
}

pub fn hex((r, g, b): Rgb) -> String {
    let c = |v: f64| (v.clamp(0.0, 1.0) * 255.0).round() as u8;
    format!("#{:02x}{:02x}{:02x}", c(r), c(g), c(b))
}

/// `top` drawn with `alpha` (times its own `#aa` alpha) over the opaque
/// `under`, blended in sRGB as the software renderer does.
pub fn over(top: &str, alpha: f64, under: &str) -> Option<String> {
    let ((r, g, b), own) = parse(top)?;
    let ((ur, ug, ub), _) = parse(under)?;
    let a = (alpha * own).clamp(0.0, 1.0);
    Some(hex((r * a + ur * (1.0 - a), g * a + ug * (1.0 - a), b * a + ub * (1.0 - a))))
}

/// APCA-W3 0.0.98G-4g lightness contrast Lc of `text` on `background`:
/// positive for dark text on light, negative for light on dark. Its
/// magnitude is what the rules compare.
pub fn apca(text: &str, background: &str) -> f64 {
    let y = |c: &str| {
        let (r, g, b) = rgb(c).unwrap_or((0.0, 0.0, 0.0));
        let y = 0.212_672_9 * r.powf(2.4) + 0.715_152_2 * g.powf(2.4) + 0.072_175 * b.powf(2.4);
        if y < 0.022 { y + (0.022 - y).powf(1.414) } else { y }
    };
    let (txt, bg) = (y(text), y(background));
    if (bg - txt).abs() < 0.0005 {
        return 0.0;
    }
    let lc = if bg > txt {
        let sapc = (bg.powf(0.56) - txt.powf(0.57)) * 1.14;
        if sapc < 0.1 { 0.0 } else { sapc - 0.027 }
    } else {
        let sapc = (bg.powf(0.65) - txt.powf(0.62)) * 1.14;
        if sapc > -0.1 { 0.0 } else { sapc + 0.027 }
    };
    lc * 100.0
}

fn linear(v: f64) -> f64 {
    if v <= 0.040_45 { v / 12.92 } else { ((v + 0.055) / 1.055).powf(2.4) }
}

fn encode(v: f64) -> f64 {
    let v = v.clamp(0.0, 1.0);
    if v <= 0.003_130_8 { v * 12.92 } else { 1.055 * v.powf(1.0 / 2.4) - 0.055 }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Vision {
    Normal,
    Protanopia,
    Deuteranopia,
    Tritanopia,
}

impl Vision {
    pub const ALL: [Vision; 4] = [Vision::Normal, Vision::Protanopia, Vision::Deuteranopia, Vision::Tritanopia];

    pub fn name(self) -> &'static str {
        match self {
            Vision::Normal => "normal",
            Vision::Protanopia => "protanopia",
            Vision::Deuteranopia => "deuteranopia",
            Vision::Tritanopia => "tritanopia",
        }
    }

    /// Machado et al. 2009, severity 1.0, applied to linear RGB.
    fn matrix(self) -> [[f64; 3]; 3] {
        match self {
            Vision::Normal => [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            Vision::Protanopia => [[0.152_286, 1.052_583, -0.204_868], [0.114_503, 0.786_281, 0.099_216], [-0.003_882, -0.048_116, 1.051_998]],
            Vision::Deuteranopia => [[0.367_322, 0.860_646, -0.227_968], [0.280_085, 0.672_501, 0.047_413], [-0.011_820, 0.042_940, 0.968_881]],
            Vision::Tritanopia => [[1.255_528, -0.076_749, -0.178_779], [-0.078_411, 0.930_809, 0.147_602], [0.004_733, 0.691_367, 0.303_900]],
        }
    }
}

/// How `color` looks with `vision`.
pub fn simulate(color: &str, vision: Vision) -> String {
    let (r, g, b) = rgb(color).unwrap_or((0.0, 0.0, 0.0));
    let (r, g, b) = (linear(r), linear(g), linear(b));
    let m = vision.matrix();
    let row = |i: usize| m[i][0] * r + m[i][1] * g + m[i][2] * b;
    hex((encode(row(0)), encode(row(1)), encode(row(2))))
}

fn lab(color: &str) -> (f64, f64, f64) {
    let (r, g, b) = rgb(color).unwrap_or((0.0, 0.0, 0.0));
    let (r, g, b) = (linear(r), linear(g), linear(b));
    let x = (0.412_456_4 * r + 0.357_576_1 * g + 0.180_437_5 * b) / 0.950_47;
    let y = 0.212_672_9 * r + 0.715_152_2 * g + 0.072_175 * b;
    let z = (0.019_333_9 * r + 0.119_192 * g + 0.950_304_1 * b) / 1.088_83;
    let f = |t: f64| if t > 216.0 / 24389.0 { t.cbrt() } else { (24389.0 / 27.0 * t + 16.0) / 116.0 };
    let (fx, fy, fz) = (f(x), f(y), f(z));
    (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))
}

/// CIEDE2000 colour difference (Sharma, Wu & Dalal 2005).
pub fn delta_e(a: &str, b: &str) -> f64 {
    let ((l1, a1, b1), (l2, a2, b2)) = (lab(a), lab(b));
    let c_bar = ((a1.hypot(b1)) + (a2.hypot(b2))) / 2.0;
    let g = 0.5 * (1.0 - (c_bar.powi(7) / (c_bar.powi(7) + 25f64.powi(7))).sqrt());
    let (a1p, a2p) = (a1 * (1.0 + g), a2 * (1.0 + g));
    let (c1p, c2p) = (a1p.hypot(b1), a2p.hypot(b2));
    let hue = |b: f64, a: f64| if b == 0.0 && a == 0.0 { 0.0 } else { b.atan2(a).to_degrees().rem_euclid(360.0) };
    let (h1p, h2p) = (hue(b1, a1p), hue(b2, a2p));
    let dl = l2 - l1;
    let dc = c2p - c1p;
    let dh = if c1p * c2p == 0.0 {
        0.0
    } else if (h2p - h1p).abs() <= 180.0 {
        h2p - h1p
    } else if h2p <= h1p {
        h2p - h1p + 360.0
    } else {
        h2p - h1p - 360.0
    };
    let dh_big = 2.0 * (c1p * c2p).sqrt() * (dh.to_radians() / 2.0).sin();
    let l_bar = (l1 + l2) / 2.0;
    let c_bar_p = (c1p + c2p) / 2.0;
    let h_bar = if c1p * c2p == 0.0 {
        h1p + h2p
    } else if (h1p - h2p).abs() <= 180.0 {
        (h1p + h2p) / 2.0
    } else if h1p + h2p < 360.0 {
        (h1p + h2p + 360.0) / 2.0
    } else {
        (h1p + h2p - 360.0) / 2.0
    };
    let t = 1.0 - 0.17 * (h_bar - 30.0).to_radians().cos() + 0.24 * (2.0 * h_bar).to_radians().cos()
        + 0.32 * (3.0 * h_bar + 6.0).to_radians().cos()
        - 0.20 * (4.0 * h_bar - 63.0).to_radians().cos();
    let d_theta = 30.0 * (-((h_bar - 275.0) / 25.0).powi(2)).exp();
    let rc = 2.0 * (c_bar_p.powi(7) / (c_bar_p.powi(7) + 25f64.powi(7))).sqrt();
    let sl = 1.0 + 0.015 * (l_bar - 50.0).powi(2) / (20.0 + (l_bar - 50.0).powi(2)).sqrt();
    let sc = 1.0 + 0.045 * c_bar_p;
    let sh = 1.0 + 0.015 * c_bar_p * t;
    let rt = -(2.0 * d_theta).to_radians().sin() * rc;
    ((dl / sl).powi(2) + (dc / sc).powi(2) + (dh_big / sh).powi(2) + rt * (dc / sc) * (dh_big / sh)).sqrt()
}

/// What a foreground is, which sets how much contrast it needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    /// Long reading: message prose, code, table cells (15-17px, 400).
    Body,
    /// Headings, names, explorer titles, field text (12-15px, often 600).
    Chrome,
    /// Secondary text people still read: quotes, tool summaries, key-hint
    /// labels, pane titles, previews (11-13px).
    Muted,
    /// Incidental text: timestamps, counts, captions, the copy icon.
    Faint,
    /// Coloured text: links, sender names, key hints, status words, the
    /// error banner, button labels.
    Signal,
    /// Non-text that must be seen: the focus ring and active pane frame.
    Focus,
    /// Component outlines (inactive pane frames, fields, tables).
    Outline,
}

impl Tier {
    /// The minimum |APCA Lc| and WCAG 2 ratio.
    pub fn minimum(self) -> (f64, f64) {
        match self {
            Tier::Body => (75.0, 7.0),
            Tier::Chrome => (75.0, 7.0),
            Tier::Muted => (60.0, 4.5),
            Tier::Faint => (45.0, 4.5),
            Tier::Signal => (60.0, 4.5),
            Tier::Focus => (45.0, 3.0),
            Tier::Outline => (15.0, 1.5),
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Tier::Body => "body",
            Tier::Chrome => "chrome",
            Tier::Muted => "muted",
            Tier::Faint => "faint",
            Tier::Signal => "signal",
            Tier::Focus => "focus",
            Tier::Outline => "outline",
        }
    }
}

/// A surface: a role, or a role tinted with another at an alpha (how the
/// keyboard cursor `accent @ 0.16` and a field selection `accent @ 0.35`
/// are drawn).
#[derive(Debug, Clone, Copy)]
pub enum Surface {
    Role(&'static str),
    Tint(&'static str, f64, &'static str),
}

impl Surface {
    pub fn label(self) -> String {
        match self {
            Surface::Role(role) => role.to_owned(),
            Surface::Tint(top, alpha, under) => format!("{top}@{alpha}/{under}"),
        }
    }
}

use Surface::{Role, Tint};
use Tier::*;

const CURSOR: Surface = Tint("accent", 0.16, "window");
const EXPLORER_CURSOR: Surface = Tint("accent", 0.16, "raised");
const FIELD_SELECTION: Surface = Tint("accent", 0.35, "control");

/// Every foreground/surface pair the Slint UI draws, with its tier.
pub const PAIRS: &[(&str, Surface, Tier)] = &[
    // The transcript (window), a user's bubble, code blocks (sunken), tool
    // cards and table headers (raised), list cursors.
    ("text", Role("window"), Body),
    ("text", Role("bubble"), Body),
    ("text", Role("sunken"), Body),
    ("text", Role("raised"), Body),
    ("text", Role("hover"), Body),
    ("text", Role("control"), Body),
    ("text", CURSOR, Body),
    // Headings, names and titles; the explorer is drawn on raised.
    ("chromeText", Role("window"), Chrome),
    ("chromeText", Role("raised"), Chrome),
    ("chromeText", Role("control"), Chrome),
    ("chromeText", Role("hover"), Chrome),
    ("chromeText", EXPLORER_CURSOR, Chrome),
    ("chromeText", CURSOR, Chrome),
    // Quotes and activity rows (window), previews (raised), key-hint
    // labels on the shortcut bar (sunken), settings rows (cursor).
    ("mutedText", Role("window"), Muted),
    ("mutedText", Role("raised"), Muted),
    ("mutedText", Role("sunken"), Muted),
    ("mutedText", Role("bubble"), Muted),
    ("mutedText", Role("control"), Muted),
    ("mutedText", Role("hover"), Muted),
    ("mutedText", CURSOR, Muted),
    ("mutedText", EXPLORER_CURSOR, Muted),
    // Selected text in a field: transient, so the faint tier (AA still).
    ("text", FIELD_SELECTION, Faint),
    // Timestamps, counts, captions.
    ("faintText", Role("window"), Faint),
    ("faintText", Role("raised"), Faint),
    ("faintText", Role("bubble"), Faint),
    ("faintText", Role("sunken"), Faint),
    ("faintText", EXPLORER_CURSOR, Faint),
    // Links in prose, bubbles and tool cards.
    ("link", Role("window"), Signal),
    ("link", Role("bubble"), Signal),
    ("link", Role("raised"), Signal),
    // Sender names and active pane titles (window, raised), key hints on
    // the shortcut bar (sunken), queue badges.
    ("accent", Role("window"), Signal),
    ("accent", Role("raised"), Signal),
    ("accent", Role("sunken"), Signal),
    ("accentText", Role("accent"), Signal),
    // Status words and the error banner.
    ("warning", Role("window"), Signal),
    ("warning", Role("raised"), Signal),
    ("warning", Role("sunken"), Signal),
    ("danger", Role("window"), Signal),
    ("danger", Role("raised"), Signal),
    ("danger", Role("dangerSurface"), Signal),
    ("success", Role("window"), Signal),
    ("success", Role("raised"), Signal),
    ("success", Role("sunken"), Signal),
    // The focus ring and active frame (accent, 1px), against the regions
    // they surround.
    ("accent", Role("control"), Focus),
    // Outlines of fields, tables and inactive pane frames.
    ("border", Role("window"), Outline),
    ("border", Role("raised"), Outline),
];

/// The status colours that must stay apart from each other under every
/// vision; CIEDE2000 between each pair must reach this.
pub const STATUS: [&str; 3] = ["success", "warning", "danger"];
pub const MIN_STATUS_DELTA_E: f64 = 12.0;
/// Links and the accent (key hints, names, focus) must also stay apart
/// from danger: a red link or key hint reads as an error.
pub const SIGNAL_PAIRS: [(&str, &str); 5] =
    [("success", "warning"), ("success", "danger"), ("warning", "danger"), ("link", "danger"), ("accent", "danger")];

/// Body size bounds (px) and the measure in characters (px / (size * 0.5),
/// the average advance of a proportional face; monospace runs ~0.6).
pub const BODY_SIZE: (f64, f64) = (15.0, 22.0);
pub const MEASURE_CHARS: (f64, f64) = (55.0, 95.0);

/// Every colour role a theme must fill.
pub const ROLES: &[&str] = &[
    "background", "bubble", "text", "mutedText", "faintText", "rule", "selection", "selectedText", "link", "sunken", "window", "raised",
    "control", "hover", "border", "shadow", "scrim", "chromeText", "secondary", "accent", "accentText", "warning", "danger",
    "dangerSurface", "success",
];

#[derive(Debug, Clone)]
pub struct Measured {
    pub foreground: &'static str,
    pub surface: String,
    pub tier: Tier,
    pub fg: String,
    pub bg: String,
    pub lc: f64,
    pub ratio: f64,
}

impl Measured {
    pub fn passes(&self) -> bool {
        let (lc, ratio) = self.tier.minimum();
        self.lc.abs() >= lc && self.ratio >= ratio
    }
}

fn color<'a>(theme: &'a Object, role: &str) -> Option<&'a str> {
    theme.get(role).and_then(Value::as_str)
}

fn surface(theme: &Object, surface: Surface) -> Option<String> {
    match surface {
        Role(role) => color(theme, role).map(str::to_owned),
        Tint(top, alpha, under) => over(color(theme, top)?, alpha, color(theme, under)?),
    }
}

/// Every pair of `theme`, measured.
pub fn measure(theme: &Object) -> Vec<Measured> {
    PAIRS
        .iter()
        .filter_map(|&(fg, on, tier)| {
            let (fgc, bg) = (color(theme, fg)?.to_owned(), surface(theme, on)?);
            Some(Measured { foreground: fg, surface: on.label(), tier, lc: apca(&fgc, &bg), ratio: contrast(&fgc, &bg), fg: fgc, bg })
        })
        .collect()
}

/// The smallest CIEDE2000 distance between two signal colours under each
/// vision: (vision, pair, ΔE).
pub fn signal_separation(theme: &Object) -> Vec<(Vision, (&'static str, &'static str), f64)> {
    let mut out = Vec::new();
    for vision in Vision::ALL {
        for (a, b) in SIGNAL_PAIRS {
            if let (Some(ca), Some(cb)) = (color(theme, a), color(theme, b)) {
                out.push((vision, (a, b), delta_e(&simulate(ca, vision), &simulate(cb, vision))));
            }
        }
    }
    out
}

/// Every way `theme` breaks the rules, as readable lines.
pub fn failures(theme: &Object) -> Vec<String> {
    let id = color(theme, "id").unwrap_or("?");
    let mut out = Vec::new();
    for role in ROLES {
        match color(theme, role) {
            Some(value) if parse(value).is_some() => {}
            _ => out.push(format!("{id}: role {role} is missing or not a colour")),
        }
    }
    for m in measure(theme) {
        if !m.passes() {
            let (lc, ratio) = m.tier.minimum();
            out.push(format!(
                "{id}: {} on {} ({} on {}) is Lc {:.1} / {:.2}:1, {} needs Lc {lc} / {ratio}:1",
                m.foreground, m.surface, m.fg, m.bg, m.lc.abs(), m.ratio, m.tier.name()
            ));
        }
    }
    for (vision, (a, b), de) in signal_separation(theme) {
        if de < MIN_STATUS_DELTA_E {
            out.push(format!("{id}: {a} and {b} are ΔE00 {de:.1} apart with {}, needs {MIN_STATUS_DELTA_E}", vision.name()));
        }
    }
    let size = theme.get("fontPixelSize").and_then(Value::as_f64).unwrap_or(0.0);
    if !(BODY_SIZE.0..=BODY_SIZE.1).contains(&size) {
        out.push(format!("{id}: body size {size}px is outside {}-{}px", BODY_SIZE.0, BODY_SIZE.1));
    }
    let measure = theme.get("measure").and_then(Value::as_f64).unwrap_or(0.0);
    let mono = theme.get("fontFamilies").and_then(Value::as_array).and_then(|f| f.first()).and_then(Value::as_str).is_some_and(|f| f.contains("Mono"));
    let chars = measure / (size * if mono { 0.6 } else { 0.5 });
    if !(MEASURE_CHARS.0..=MEASURE_CHARS.1).contains(&chars) {
        out.push(format!("{id}: measure {measure}px is ~{chars:.0} characters, outside {}-{}", MEASURE_CHARS.0, MEASURE_CHARS.1));
    }
    out
}

/// Every failure of every bundled theme.
pub fn all_failures() -> Vec<String> {
    themes().iter().flat_map(failures).collect()
}
