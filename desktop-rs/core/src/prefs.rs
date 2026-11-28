//! Every user preference the desktop has, as one typed registry: its short
//! name (`:set fontsize=16`), its key in the settings file, a label, a
//! one-line description, the words people search for it by, its section,
//! its kind (switch, number, choice, colour, text) and its default. Ctrl+K,
//! the Settings view, the settings file's schema and reference, export and
//! import are all made from this list, so a setting cannot exist in one and
//! be missing from another.
//!
//! Values live in [`Settings`] under the keys below. Two kinds belong to the
//! reading theme rather than the window: the body font size (in
//! `appearance/fontOverrides`, `{theme: {"size"}}`, shared with the per-theme
//! font picker) and the colour overrides (`appearance/colorOverrides`,
//! `{theme: {role: "#rrggbb"}}`). Their defaults come from the theme.
//!
//! A stored value that is not valid (wrong type, out of range, unknown
//! choice, not a colour) is reported by [`problems`] and read as the default:
//! hand-editing the file can never break the window.

use serde_json::{Map, Value, json};

use crate::reading_theme;
use crate::settings::Settings;
use crate::tr;

/// English kept in the static registry and marked for the catalog:
/// `en::tr!("Text")` is the text itself, and `tools/i18n-export` reads it as
/// it reads `tr!`. The registry stays English (the settings file's schema
/// and reference, search); the apps translate a label where they draw it
/// (`translated`).
mod en {
    macro_rules! marked {
        ($text:literal $(,)?) => {
            $text
        };
    }
    pub(crate) use marked as tr;
}

/// English from the registry in the app's language now.
pub fn translated(english: &str) -> String {
    if english.is_empty() {
        return String::new();
    }
    crate::i18n::translate("", english, "", None, &[])
}

/// What kind of value a setting holds.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    /// On or off. `inverted` stores the opposite (`audio/muted` for
    /// "Spoken replies").
    Toggle { default: bool, inverted: bool },
    /// A number in `min..=max`, moved by `step`. `zero` names 0 when 0 means
    /// something else ("off", "full width"); 0 is then allowed below `min`.
    Number { default: f64, min: f64, max: f64, step: f64, unit: &'static str, zero: Option<&'static str> },
    /// One of `options` (`(id, label)`). `indexed` stores the option's index
    /// rather than its id (older integer settings).
    Choice { default: &'static str, options: &'static [(&'static str, &'static str)], indexed: bool },
    /// A colour role of the reading theme, overridden per theme.
    Color { role: &'static str },
    /// Free text (a font family).
    Text { default: &'static str },
}

/// Where a value is kept.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    /// Under its own key.
    Global,
    /// Inside the object at `key`, per reading theme, under `field`.
    Theme { field: &'static str },
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Spec {
    /// The short name: `:set name=value`, the file's reference, the tests.
    pub name: &'static str,
    /// The settings-file key (for theme-scoped values, the object holding
    /// every theme's).
    pub key: &'static str,
    pub label: &'static str,
    /// One line: what it does.
    pub description: &'static str,
    /// Other words for it (synonyms, old names, British and American).
    pub aliases: &'static [&'static str],
    pub section: &'static str,
    pub kind: Kind,
    pub scope: Scope,
}

pub const TYPOGRAPHY: &str = "Typography";
pub const LAYOUT: &str = "Layout";
pub const CHAT: &str = "Chat";
pub const COLOURS: &str = "Colours";
pub const BEHAVIOUR: &str = "Behaviour";
pub const NOTIFICATIONS: &str = "Notifications";

/// The sections in the order the Settings view shows them.
pub const SECTIONS: &[&str] = &[TYPOGRAPHY, LAYOUT, CHAT, COLOURS, NOTIFICATIONS, BEHAVIOUR];

pub const FONT_OVERRIDES_KEY: &str = "appearance/fontOverrides";
pub const COLOR_OVERRIDES_KEY: &str = "appearance/colorOverrides";
pub const THEME_KEY: &str = "appearance/readingTheme";

const fn toggle(default: bool) -> Kind {
    Kind::Toggle { default, inverted: false }
}
const fn number(default: f64, min: f64, max: f64, step: f64, unit: &'static str) -> Kind {
    Kind::Number { default, min, max, step, unit, zero: None }
}
const G: Scope = Scope::Global;

const WEIGHTS: &[(&str, &str)] = &[
    // i18n: Settings > Text weight, an option (adjective): a font weight
    ("300", en::tr!("Light")),
    // i18n: Settings > Text weight, an option (adjective): a font weight
    ("400", en::tr!("Regular")),
    // i18n: Settings > Text weight, an option (adjective): a font weight
    ("500", en::tr!("Medium")),
    // i18n: Settings > Text weight, an option (adjective): a font weight
    ("600", en::tr!("Semibold")),
];
const DENSITIES: &[(&str, &str)] = &[
    // i18n: Settings > Density, an option (adjective): how tightly rows are spaced
    ("compact", en::tr!("Compact")),
    // i18n: Settings > Density, an option (adjective): how tightly rows are spaced
    ("comfortable", en::tr!("Comfortable")),
    // i18n: Settings > Density, an option (adjective): how tightly rows are spaced
    ("spacious", en::tr!("Spacious")),
];
const CLOCKS: &[(&str, &str)] = &[
    // i18n: Settings > Time format, an option: how times are written; keep the example time as it is
    ("12h", en::tr!("12-hour (6:10 PM)")),
    // i18n: Settings > Time format, an option: how times are written; keep the example time as it is
    ("24h", en::tr!("24-hour (18:10)")),
];
const ACTIVITY: &[(&str, &str)] = &[
    // i18n: Settings > Tool activity, an option: how tool calls show in the chat
    ("grouped", en::tr!("Grouped")),
    // i18n: Settings > Tool activity, an option: how tool calls show in the chat
    ("visible", en::tr!("Always visible")),
    // i18n: Settings > Tool activity, an option: how tool calls show in the chat
    ("group-old", en::tr!("Group old")),
    // i18n: Settings > Tool activity, an option: how tool calls show in the chat
    ("hide-when-done", en::tr!("Hide when done")),
];
const DETAIL: &[(&str, &str)] = &[
    // i18n: Settings > Tool detail, an option: how much detail tool explanations have
    ("raw", en::tr!("Raw (no AI)")),
    // i18n: Settings > Tool detail, an option: how much detail tool explanations have
    ("brief", en::tr!("Brief")),
    // i18n: Settings > Tool detail, an option: how much detail tool explanations have
    ("plain", en::tr!("Plain English")),
    // i18n: Settings > Tool detail, an option: how much detail tool explanations have
    ("explained", en::tr!("Explained")),
    // i18n: Settings > Tool detail, an option: how much detail tool explanations have
    ("teaching", en::tr!("Teaching")),
];
const LINKS: &[(&str, &str)] = &[
    // i18n: Settings > Opening links, an option (an action, verb phrase): what clicking a link does
    ("browser", en::tr!("Open in the browser")),
    // i18n: Settings > Opening links, an option (an action, verb phrase): what clicking a link does
    ("copy", en::tr!("Copy the address")),
];
const CREDENTIAL_BACKENDS: &[(&str, &str)] = &[
    // i18n: Settings > Credential store, an option (noun): where device tokens are kept; 0600 is a file permission, keep it
    ("keyring", en::tr!("OS keyring")),
    // i18n: Settings > Credential store, an option (noun): where device tokens are kept; 0600 is a file permission, keep it
    ("file", en::tr!("Private file (0600)")),
    // i18n: Settings > Credential store, an option (noun): where device tokens are kept; 0600 is a file permission, keep it
    ("command", en::tr!("Custom command")),
];
/// The avatar view's sizes (slint-app `avatar_view::CHOICES`).
const AVATARS: &[(&str, &str)] = &[
    // i18n: Settings > Agent picture size, an option: the size's name and its size in pixels
    ("small", en::tr!("Small · 36 px")),
    // i18n: Settings > Agent picture size, an option: the size's name and its size in pixels
    ("medium", en::tr!("Medium · 44 px")),
    // i18n: Settings > Agent picture size, an option: the size's name and its size in pixels
    ("large", en::tr!("Large · 52 px")),
];
const SCROLLBARS: &[(&str, &str)] = &[
    // i18n: Settings > Scrollbar, an option: when scroll bars show
    ("auto", en::tr!("When scrollable")),
    // i18n: Settings > Scrollbar, an option: when scroll bars show
    ("always", en::tr!("Always")),
    // i18n: Settings > Scrollbar, an option: when scroll bars show
    ("never", en::tr!("Never")),
];

macro_rules! color {
    ($name:literal, $role:literal, $label:expr, $description:expr, [$($alias:literal),*]) => {
        Spec {
            name: $name,
            key: COLOR_OVERRIDES_KEY,
            label: $label,
            description: $description,
            aliases: &[$($alias,)* "colour", "color", "theme colour", "palette"],
            section: COLOURS,
            kind: Kind::Color { role: $role },
            scope: Scope::Theme { field: $role },
        }
    };
}

/// Every setting, in the order the Settings view shows them.
pub static SPECS: &[Spec] = &[
    // ---- typography
    Spec {
        name: "fontsize",
        key: FONT_OVERRIDES_KEY,
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Size of message text in the chat, for the current reading theme
        label: en::tr!("Font size"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Font size” saying what it does.
        description: en::tr!("Size of message text in the chat, for the current reading theme"),
        aliases: &["text size", "bigger", "smaller", "larger", "font-size", "body size", "reading size", "type size"],
        section: TYPOGRAPHY,
        kind: Kind::Number { default: 15.0, min: 9.0, max: 40.0, step: 1.0, unit: "px", zero: None },
        scope: Scope::Theme { field: "size" },
    },
    Spec {
        name: "chromesize",
        key: "typography/chromeScale",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Size of labels, lists, titles and bars around the chat, relative to normal
        label: en::tr!("Interface label size"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Interface label size” saying what it does.
        description: en::tr!("Size of labels, lists, titles and bars around the chat, relative to normal"),
        aliases: &["ui text", "chrome text", "label size", "menu text", "sidebar text", "small text"],
        section: TYPOGRAPHY,
        kind: number(100.0, 70.0, 180.0, 5.0, "%"),
        scope: G,
    },
    Spec {
        name: "codefont",
        key: "typography/codeFamily",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Font family for code blocks, commands and tool output
        label: en::tr!("Code font"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Code font” saying what it does.
        description: en::tr!("Font family for code blocks, commands and tool output"),
        aliases: &["monospace", "mono font", "code typeface", "terminal font", "fixed width"],
        section: TYPOGRAPHY,
        kind: Kind::Text { default: "JetBrains Mono" },
        scope: G,
    },
    Spec {
        name: "codesize",
        key: "typography/codeScale",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Size of code blocks relative to the message text
        label: en::tr!("Code size"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Code size” saying what it does.
        description: en::tr!("Size of code blocks relative to the message text"),
        aliases: &["code text size", "monospace size", "snippet size", "code zoom"],
        section: TYPOGRAPHY,
        kind: number(90.0, 60.0, 150.0, 5.0, "%"),
        scope: G,
    },
    Spec {
        name: "paragraphspacing",
        key: "typography/paragraphSpacing",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Space between paragraphs, lists, code and headings in a message (lines inside a paragraph keep the font's own spacing)
        label: en::tr!("Paragraph spacing"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Paragraph spacing” saying what it does.
        description: en::tr!("Space between paragraphs, lists, code and headings in a message (lines inside a paragraph keep the font's own spacing)"),
        aliases: &["line spacing", "linespacing", "line height", "leading", "block spacing", "paragraph gap"],
        section: TYPOGRAPHY,
        kind: number(8.0, 0.0, 40.0, 2.0, "px"),
        scope: G,
    },
    Spec {
        name: "headingscale",
        key: "typography/headingScale",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Size of a message's top-level headings relative to its text (smaller headings follow)
        label: en::tr!("Heading size"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Heading size” saying what it does.
        description: en::tr!("Size of a message's top-level headings relative to its text (smaller headings follow)"),
        aliases: &["heading scale", "title size", "h1", "headline size", "header size"],
        section: TYPOGRAPHY,
        kind: number(140.0, 100.0, 250.0, 10.0, "%"),
        scope: G,
    },
    Spec {
        name: "measure",
        key: "typography/measure",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Longest line of message text, in characters; 0 uses the pane's full width
        label: en::tr!("Line length"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Line length” saying what it does.
        description: en::tr!("Longest line of message text, in characters; 0 uses the pane's full width"),
        aliases: &["measure", "column width", "text width", "max width", "reading width", "characters per line"],
        section: TYPOGRAPHY,
        kind: Kind::Number { default: 0.0, min: 40.0, max: 240.0, step: 10.0, unit: "ch", zero: Some(en::tr!("Full width")) }, // i18n: Settings > Line length, the value shown for 0: lines use the pane's full width
        scope: G,
    },
    Spec {
        name: "fontweight",
        key: "typography/weight",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Default weight of interface text without its own weight (Slint's rich message text keeps the face's regular weight)
        label: en::tr!("Text weight"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Text weight” saying what it does.
        description: en::tr!("Default weight of interface text without its own weight (Slint's rich message text keeps the face's regular weight)"),
        aliases: &["font weight", "boldness", "bold", "light", "thickness", "heaviness"],
        section: TYPOGRAPHY,
        kind: Kind::Choice { default: "400", options: WEIGHTS, indexed: false },
        scope: G,
    },
    // ---- layout
    Spec {
        name: "uiscale",
        key: "appearance/uiScale",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Scale of the whole window, text, icons and spacing alike (Ctrl+= / Ctrl+-)
        label: en::tr!("Interface scale"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Interface scale” saying what it does.
        description: en::tr!("Scale of the whole window, text, icons and spacing alike (Ctrl+= / Ctrl+-)"),
        aliases: &["ui scale", "scale", "dpi", "window size", "interface size", "larger interface", "smaller interface"],
        section: LAYOUT,
        kind: number(1.15, 0.5, 3.0, 0.05, "×"),
        scope: G,
    },
    Spec {
        name: "density",
        key: "layout/density",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: How much padding rows and lists get: compact fits more, spacious breathes
        label: en::tr!("Density"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Density” saying what it does.
        description: en::tr!("How much padding rows and lists get: compact fits more, spacious breathes"),
        aliases: &["compact", "padding", "row padding", "spacing", "tight", "roomy", "comfortable"],
        section: LAYOUT,
        kind: Kind::Choice { default: "comfortable", options: DENSITIES, indexed: false },
        scope: G,
    },
    Spec {
        name: "explorerwidth",
        key: "layout/explorerWidth",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Width of the chat list on the left, when it shows names and previews
        label: en::tr!("Explorer width"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Explorer width” saying what it does.
        description: en::tr!("Width of the chat list on the left, when it shows names and previews"),
        aliases: &["sidebar width", "chat list width", "left panel width", "agents list width", "navigator width"],
        section: LAYOUT,
        kind: number(320.0, 180.0, 720.0, 20.0, "px"),
        scope: G,
    },
    Spec {
        name: "compactexplorer",
        key: "explorer/compact",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Show only each agent's avatar and name in the chat list
        label: en::tr!("Compact explorer"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Compact explorer” saying what it does.
        description: en::tr!("Show only each agent's avatar and name in the chat list"),
        aliases: &["slim sidebar", "narrow sidebar", "names only", "compact sidebar", "collapse previews"],
        section: LAYOUT,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "avatarsize",
        key: "appearance/avatarSize",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Size of the agents' portraits in the explorer, compact rows and cards
        label: en::tr!("Agent picture size"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Agent picture size” saying what it does.
        description: en::tr!("Size of the agents' portraits in the explorer, compact rows and cards"),
        aliases: &["avatar size", "portrait size", "picture size", "profile picture", "face size", "photo"],
        section: LAYOUT,
        kind: Kind::Choice { default: "medium", options: AVATARS, indexed: false },
        scope: G,
    },
    Spec {
        name: "bubblewidth",
        key: "layout/bubbleWidth",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Widest your own messages get, as a share of the chat's width
        label: en::tr!("Bubble width"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Bubble width” saying what it does.
        description: en::tr!("Widest your own messages get, as a share of the chat's width"),
        aliases: &["message width", "user bubble", "my messages width", "balloon width"],
        section: LAYOUT,
        kind: number(75.0, 30.0, 100.0, 5.0, "%"),
        scope: G,
    },
    Spec {
        name: "messagespacing",
        key: "layout/messageSpacing",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Space between one message and the next
        label: en::tr!("Message spacing"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Message spacing” saying what it does.
        description: en::tr!("Space between one message and the next"),
        aliases: &["message gap", "row spacing", "gap between messages", "message padding", "vertical spacing"],
        section: LAYOUT,
        kind: number(14.0, 0.0, 48.0, 2.0, "px"),
        scope: G,
    },
    Spec {
        name: "panegap",
        key: "layout/paneGap",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Space between split panes
        label: en::tr!("Pane gap"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Pane gap” saying what it does.
        description: en::tr!("Space between split panes"),
        aliases: &["split gap", "gutter", "pane spacing", "gap between panes", "split spacing"],
        section: LAYOUT,
        kind: number(4.0, 0.0, 32.0, 1.0, "px"),
        scope: G,
    },
    Spec {
        name: "borderwidth",
        key: "layout/borderWidth",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Thickness of the outline around panes that do not have the keyboard
        label: en::tr!("Border thickness"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Border thickness” saying what it does.
        description: en::tr!("Thickness of the outline around panes that do not have the keyboard"),
        aliases: &["border", "outline", "frame width", "stroke", "line thickness", "unfocused border"],
        section: LAYOUT,
        kind: number(1.5, 0.0, 6.0, 0.5, "px"),
        scope: G,
    },
    Spec {
        name: "focusborderwidth",
        key: "layout/focusBorderWidth",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Thickness of the outline around the pane that has the keyboard
        label: en::tr!("Focused border thickness"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Focused border thickness” saying what it does.
        description: en::tr!("Thickness of the outline around the pane that has the keyboard"),
        aliases: &["focus ring", "active border", "focused outline", "focus width", "keyboard outline"],
        section: LAYOUT,
        kind: number(2.5, 0.5, 8.0, 0.5, "px"),
        scope: G,
    },
    Spec {
        name: "radius",
        key: "layout/radius",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Roundness of pane outlines, bubbles and cards; 0 is square
        label: en::tr!("Corner radius"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Corner radius” saying what it does.
        description: en::tr!("Roundness of pane outlines, bubbles and cards; 0 is square"),
        aliases: &["rounding", "rounded corners", "border radius", "corners", "roundness"],
        section: LAYOUT,
        kind: number(6.0, 0.0, 20.0, 1.0, "px"),
        scope: G,
    },
    Spec {
        name: "scrollbarwidth",
        key: "layout/scrollbarWidth",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Thickness of the chat's scrollbar thumb (it doubles under the pointer)
        label: en::tr!("Scrollbar width"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Scrollbar width” saying what it does.
        description: en::tr!("Thickness of the chat's scrollbar thumb (it doubles under the pointer)"),
        aliases: &["scroll bar", "scroller", "thumb width", "scroll thumb"],
        section: LAYOUT,
        kind: number(3.0, 1.0, 16.0, 1.0, "px"),
        scope: G,
    },
    Spec {
        name: "scrollbar",
        key: "layout/scrollbar",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: When the chat shows its scrollbar
        label: en::tr!("Scrollbar"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Scrollbar” saying what it does.
        description: en::tr!("When the chat shows its scrollbar"),
        aliases: &["show scrollbar", "hide scrollbar", "scroll bar visibility", "scroller"],
        section: LAYOUT,
        kind: Kind::Choice { default: "auto", options: SCROLLBARS, indexed: false },
        scope: G,
    },
    Spec {
        name: "navrail",
        key: "appearance/navRail",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: The icon rail on the far left (Chats, Updates, Teams, Settings)
        label: en::tr!("Activity bar"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Activity bar” saying what it does.
        description: en::tr!("The icon rail on the far left (Chats, Updates, Teams, Settings)"),
        aliases: &["navigation rail", "nav rail", "left bar", "icons", "rail", "activity bar", "hide", "show", "toggle"],
        section: LAYOUT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "workspacebar",
        key: "appearance/workspaceBar",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: The strip of workspace tabs across the top
        label: en::tr!("Workspace bar"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Workspace bar” saying what it does.
        description: en::tr!("The strip of workspace tabs across the top"),
        aliases: &["workspaces", "tabs", "top strip", "tab bar", "hide", "show"],
        section: LAYOUT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "shortcutbar",
        key: "appearance/shortcutsVisible",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: The bar along the bottom with the keys that work where the keyboard is, and the connection
        label: en::tr!("Shortcut bar"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Shortcut bar” saying what it does.
        description: en::tr!("The bar along the bottom with the keys that work where the keyboard is, and the connection"),
        aliases: &["key hints", "keybindings bar", "status line", "status bar", "footer", "hints", "hide", "show"],
        section: LAYOUT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "minimalui",
        key: "appearance/minimalUi",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Hide the chrome around the chat: titles, frames and bars
        label: en::tr!("Minimal UI"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Minimal UI” saying what it does.
        description: en::tr!("Hide the chrome around the chat: titles, frames and bars"),
        aliases: &["zen mode", "distraction free", "focus mode", "clean", "hide chrome"],
        section: LAYOUT,
        kind: toggle(false),
        scope: G,
    },
    // ---- chat
    Spec {
        name: "timestamps",
        key: "conversation/timestampsVisible",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Show the time under each message
        label: en::tr!("Timestamps"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Timestamps” saying what it does.
        description: en::tr!("Show the time under each message"),
        aliases: &["times", "dates", "message time", "clock", "show time"],
        section: CHAT,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "chatzoom",
        key: "conversation/chatZoom",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Scale chat text, cards and spacing.
        label: en::tr!("Chat zoom"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Chat zoom” saying what it does.
        description: en::tr!("Scale chat text, cards and spacing."),
        aliases: &["zoom", "zoom in", "zoom out", "magnify", "bigger", "smaller", "text size", "chat size", "message size", "enlarge"],
        section: CHAT,
        kind: number(100.0, 60.0, 250.0, 10.0, "%"),
        scope: G,
    },
    Spec {
        name: "timeformat",
        key: "conversation/clock",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: 12- or 24-hour clock for message and chat-list times
        label: en::tr!("Time format"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Time format” saying what it does.
        description: en::tr!("12- or 24-hour clock for message and chat-list times"),
        aliases: &["clock", "24 hour", "12 hour", "am pm", "military time", "time style"],
        section: CHAT,
        kind: Kind::Choice { default: "12h", options: CLOCKS, indexed: false },
        scope: G,
    },
    Spec {
        name: "foldprompts",
        key: "conversation/foldAgentPrompts",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Show prompts from other agents as one line until opened
        label: en::tr!("Fold agent prompts"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Fold agent prompts” saying what it does.
        description: en::tr!("Show prompts from other agents as one line until opened"),
        aliases: &["collapse prompts", "agent prompts folded", "fold handoffs", "expand prompts", "collapse"],
        section: CHAT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "toolactivity",
        key: "conversation/activityDisplayMode",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: How tool calls show: one work row per turn, always visible, only old ones grouped, or the work row only while the turn runs
        label: en::tr!("Tool activity"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Tool activity” saying what it does.
        description: en::tr!("How tool calls show: one work row per turn, always visible, only old ones grouped, or the work row only while the turn runs"),
        aliases: &["tool calls", "worked for", "fold tools", "collapse tools", "expand tools", "grouping", "hide tools", "no tools", "hide when done", "quiet chat"],
        section: CHAT,
        kind: Kind::Choice { default: "grouped", options: ACTIVITY, indexed: true },
        scope: G,
    },
    Spec {
        name: "tooldetail",
        key: "experiments/toolDetailLevel",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: How tool rows are explained: raw commands, or plain English at several depths (uses AI)
        label: en::tr!("Tool detail"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Tool detail” saying what it does.
        description: en::tr!("How tool rows are explained: raw commands, or plain English at several depths (uses AI)"),
        aliases: &["narration", "explanations", "plain english", "tool rows detail", "audience"],
        section: CHAT,
        kind: Kind::Choice { default: "raw", options: DETAIL, indexed: true },
        scope: G,
    },
    Spec {
        name: "showwhenready",
        key: "conversation/showWhenReady",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Show an answer once it is complete instead of as it streams
        label: en::tr!("Show when ready"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Show when ready” saying what it does.
        description: en::tr!("Show an answer once it is complete instead of as it streams"),
        aliases: &["streaming", "typing", "wait for answer", "no streaming"],
        section: CHAT,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "readingtheme",
        key: THEME_KEY,
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: The colours and font the chat is drawn in
        label: en::tr!("Reading theme"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Reading theme” saying what it does.
        description: en::tr!("The colours and font the chat is drawn in"),
        aliases: &["theme", "colour scheme", "color scheme", "dark mode", "light mode", "appearance", "skin"],
        section: COLOURS,
        kind: Kind::Choice { default: "terminal", options: &[], indexed: false },
        scope: G,
    },
    // ---- colours: every role the window draws, per reading theme
    color!(
        "color.text",
        "text",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of message text
        en::tr!("Message text"),
        // i18n: Settings > Colours, the line under the colour setting “Message text” saying what it colours
        en::tr!("Colour of message text"),
        ["text colour", "foreground", "font colour"]
    ),
    color!(
        "color.window",
        "window",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of the chat's and panes' background
        en::tr!("Background"),
        // i18n: Settings > Colours, the line under the colour setting “Background” saying what it colours
        en::tr!("Colour of the chat's and panes' background"),
        ["background", "bg", "backdrop"]
    ),
    color!(
        "color.raised",
        "raised",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of the explorer, dialogs and bars
        en::tr!("Raised surface"),
        // i18n: Settings > Colours, the line under the colour setting “Raised surface” saying what it colours
        en::tr!("Colour of the explorer, dialogs and bars"),
        ["panel", "sidebar background", "surface"]
    ),
    color!(
        "color.sunken",
        "sunken",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour behind code blocks and fields
        en::tr!("Sunken surface"),
        // i18n: Settings > Colours, the line under the colour setting “Sunken surface” saying what it colours
        en::tr!("Colour behind code blocks and fields"),
        ["code background", "inset", "well"]
    ),
    color!(
        "color.control",
        "control",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of buttons, fields and the selected chat
        en::tr!("Controls"),
        // i18n: Settings > Colours, the line under the colour setting “Controls” saying what it colours
        en::tr!("Colour of buttons, fields and the selected chat"),
        ["button", "field", "selected row"]
    ),
    color!(
        "color.hover",
        "hover",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of a row under the pointer
        en::tr!("Hover highlight"),
        // i18n: Settings > Colours, the line under the colour setting “Hover highlight” saying what it colours
        en::tr!("Colour of a row under the pointer"),
        ["hover background", "mouse over", "highlight"]
    ),
    color!(
        "color.border",
        "border",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of outlines around panes and controls
        en::tr!("Borders"),
        // i18n: Settings > Colours, the line under the colour setting “Borders” saying what it colours
        en::tr!("Colour of outlines around panes and controls"),
        ["outline", "frame", "stroke"]
    ),
    color!(
        "color.rule",
        "rule",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of separators and table lines
        en::tr!("Rules and separators"),
        // i18n: Settings > Colours, the line under the colour setting “Rules and separators” saying what it colours
        en::tr!("Colour of separators and table lines"),
        ["separator", "divider", "lines"]
    ),
    color!(
        "color.chrometext",
        "chromeText",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of names, titles and labels around the chat
        en::tr!("Interface text"),
        // i18n: Settings > Colours, the line under the colour setting “Interface text” saying what it colours
        en::tr!("Colour of names, titles and labels around the chat"),
        ["chrome text", "labels", "titles"]
    ),
    color!(
        "color.muted",
        "mutedText",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of secondary text: previews, details, quotes
        en::tr!("Secondary text"),
        // i18n: Settings > Colours, the line under the colour setting “Secondary text” saying what it colours
        en::tr!("Colour of secondary text: previews, details, quotes"),
        ["muted text", "grey text", "dim text"]
    ),
    color!(
        "color.faint",
        "faintText",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of the quietest text: stamps, hints
        en::tr!("Faint text"),
        // i18n: Settings > Colours, the line under the colour setting “Faint text” saying what it colours
        en::tr!("Colour of the quietest text: stamps, hints"),
        ["faint text", "hint text", "timestamp colour"]
    ),
    color!(
        "color.accent",
        "accent",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of highlights, the cursor, switches and section titles
        en::tr!("Accent"),
        // i18n: Settings > Colours, the line under the colour setting “Accent” saying what it colours
        en::tr!("Colour of highlights, the cursor, switches and section titles"),
        ["highlight", "primary", "brand"]
    ),
    color!(
        "color.accenttext",
        "accentText",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of text drawn on the accent
        en::tr!("Text on accent"),
        // i18n: Settings > Colours, the line under the colour setting “Text on accent” saying what it colours
        en::tr!("Colour of text drawn on the accent"),
        ["on accent", "badge text", "contrast text"]
    ),
    color!(
        "color.focus",
        "focus",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of the outline of the pane that has the keyboard
        en::tr!("Focused pane outline"),
        // i18n: Settings > Colours, the line under the colour setting “Focused pane outline” saying what it colours
        en::tr!("Colour of the outline of the pane that has the keyboard"),
        ["focus ring", "active pane", "keyboard outline"]
    ),
    color!(
        "color.bubble",
        "bubble",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour behind your own messages
        en::tr!("Your bubble"),
        // i18n: Settings > Colours, the line under the colour setting “Your bubble” saying what it colours
        en::tr!("Colour behind your own messages"),
        ["user bubble", "my messages", "balloon"]
    ),
    color!(
        "color.link",
        "link",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of links in messages
        en::tr!("Links"),
        // i18n: Settings > Colours, the line under the colour setting “Links” saying what it colours
        en::tr!("Colour of links in messages"),
        ["hyperlink", "url", "anchor"]
    ),
    color!(
        "color.success",
        "success",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of working agents and things that went well
        en::tr!("Success"),
        // i18n: Settings > Colours, the line under the colour setting “Success” saying what it colours
        en::tr!("Colour of working agents and things that went well"),
        ["green", "ok", "busy"]
    ),
    color!(
        "color.warning",
        "warning",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of warnings and stopped answers
        en::tr!("Warning"),
        // i18n: Settings > Colours, the line under the colour setting “Warning” saying what it colours
        en::tr!("Colour of warnings and stopped answers"),
        ["yellow", "caution", "orange"]
    ),
    color!(
        "color.danger",
        "danger",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour of errors and failed messages
        en::tr!("Danger"),
        // i18n: Settings > Colours, the line under the colour setting “Danger” saying what it colours
        en::tr!("Colour of errors and failed messages"),
        ["red", "error", "failure"]
    ),
    color!(
        "color.dangersurface",
        "dangerSurface",
        // i18n: Settings > Colours, a colour setting's name, the row label (noun). What it colours: Colour behind error banners
        en::tr!("Error background"),
        // i18n: Settings > Colours, the line under the colour setting “Error background” saying what it colours
        en::tr!("Colour behind error banners"),
        ["error background", "alert surface", "danger surface"]
    ),
    // ---- behaviour
    Spec {
        name: "follow",
        key: "conversation/followNewMessages",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Keep the chat scrolled to the newest message as answers arrive, while you are at the bottom
        label: en::tr!("Follow new messages"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Follow new messages” saying what it does.
        description: en::tr!("Keep the chat scrolled to the newest message as answers arrive, while you are at the bottom"),
        aliases: &["autoscroll", "auto scroll", "stick to bottom", "tail", "jump to new", "scroll lock"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "scrollspeed",
        key: "behaviour/scrollSpeed",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: How far the arrow keys, J/K and Page Up/Down move the chat, relative to normal
        label: en::tr!("Scroll speed"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Scroll speed” saying what it does.
        description: en::tr!("How far the arrow keys, J/K and Page Up/Down move the chat, relative to normal"),
        aliases: &["scroll step", "scrolling speed", "page speed", "line step", "behavior"],
        section: BEHAVIOUR,
        kind: number(100.0, 25.0, 400.0, 25.0, "%"),
        scope: G,
    },
    Spec {
        name: "vim",
        key: "keymap/vim",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Use the window without Ctrl: Esc for Normal mode, then j/k, gg, /, : and Space for every action
        label: en::tr!("Vim mode"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Vim mode” saying what it does.
        description: en::tr!("Use the window without Ctrl: Esc for Normal mode, then j/k, gg, /, : and Space for every action"),
        aliases: &["vi", "vim keys", "modal keys", "normal mode", "no ctrl", "leader key", "hjkl"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "doublepress",
        key: "keymap/doublePressMs",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: How quickly a key must be pressed twice to count as a double press
        label: en::tr!("Double-press window"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Double-press window” saying what it does.
        description: en::tr!("How quickly a key must be pressed twice to count as a double press"),
        aliases: &["double tap", "double click", "key repeat window", "chord timeout"],
        section: BEHAVIOUR,
        kind: number(300.0, 150.0, 1000.0, 50.0, "ms"),
        scope: G,
    },
    Spec {
        name: "previewonmove",
        key: "explorer/livePreview",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Show a chat as the explorer's cursor moves onto it, before Enter
        label: en::tr!("Preview on move"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Preview on move” saying what it does.
        description: en::tr!("Show a chat as the explorer's cursor moves onto it, before Enter"),
        aliases: &["live preview", "preview while browsing", "peek", "follow cursor"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "notifyreplies",
        key: "notifications/replies",
        label: crate::notify_events::DESKTOP_LABEL,
        description: crate::notify_events::DESKTOP_ABOUT,
        aliases: &["notifications", "alerts", "notify", "popups", "toasts", "reply notifications"],
        section: NOTIFICATIONS,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "notifysound",
        key: "notifications/sound",
        label: crate::notify_events::SOUND_LABEL,
        description: crate::notify_events::SOUND_ABOUT,
        aliases: &["sounds", "chime", "ding", "audio alert", "beep", "notification sound"],
        section: NOTIFICATIONS,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "pausemobile",
        key: "notifications/pauseMobileWhileDesktopActive",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Hold back phone notifications while you are active at this desktop
        label: en::tr!("Pause phone alerts"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Pause phone alerts” saying what it does.
        description: en::tr!("Hold back phone notifications while you are active at this desktop"),
        aliases: &["mobile push", "phone notifications", "quiet phone", "push alerts"],
        section: NOTIFICATIONS,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "spokenreplies",
        key: "audio/muted",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Read agents' answers aloud
        label: en::tr!("Spoken replies"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Spoken replies” saying what it does.
        description: en::tr!("Read agents' answers aloud"),
        aliases: &["voice", "speech", "mute", "unmute", "tts", "read aloud", "sound"],
        section: BEHAVIOUR,
        kind: Kind::Toggle { default: true, inverted: true },
        scope: G,
    },
    Spec {
        name: "confirmstop",
        key: "behaviour/confirmStop",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Ask before stopping (Ctrl+.) or releasing an agent
        label: en::tr!("Confirm stop and release"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Confirm stop and release” saying what it does.
        description: en::tr!("Ask before stopping (Ctrl+.) or releasing an agent"),
        aliases: &["confirm dialogs", "are you sure", "confirmation", "safety", "prompt before stop"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "linkopen",
        key: "behaviour/linkOpen",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: What a click on a link (or a link hint) does
        label: en::tr!("Opening links"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Opening links” saying what it does.
        description: en::tr!("What a click on a link (or a link hint) does"),
        aliases: &["links", "browser", "open urls", "copy links", "hyperlinks"],
        section: BEHAVIOUR,
        kind: Kind::Choice { default: "browser", options: LINKS, indexed: false },
        scope: G,
    },
    Spec {
        name: "startupnewagent",
        key: "launch/newAgentOnStartup",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Start a new agent when Clarp opens, instead of reopening the last chat
        label: en::tr!("New agent on startup"),
        // i18n: Settings page and Ctrl+K, the line under the preference “New agent on startup” saying what it does.
        description: en::tr!("Start a new agent when Clarp opens, instead of reopening the last chat"),
        aliases: &["startup chat", "on launch", "open with new chat", "startup"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "anonymousagents",
        key: "launch/anonymousAgents",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Start new agents without a persona by default
        label: en::tr!("Anonymous agents"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Anonymous agents” saying what it does.
        description: en::tr!("Start new agents without a persona by default"),
        aliases: &["no persona", "nameless", "plain agents", "agent identity"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "reducedmotion",
        key: "appearance/reducedMotion",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Stop the shimmer, breathing rings and other animations
        label: en::tr!("Reduce motion"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Reduce motion” saying what it does.
        description: en::tr!("Stop the shimmer, breathing rings and other animations"),
        aliases: &["animations", "motion", "no animation", "accessibility", "still"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "sendanimation",
        key: "appearance/sendAnimation",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: A sent message flies from the composer to its place in the chat
        label: en::tr!("Send animation"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Send animation” saying what it does.
        description: en::tr!("A sent message flies from the composer to its place in the chat"),
        aliases: &["send", "sending", "fly", "flight", "telegram", "bubble animation"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "credentialsbackend",
        key: "credentials/backend",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Where device tokens are kept and read from: the OS keyring, a private 0600 file, or a custom shell command
        label: en::tr!("Credential store"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Credential store” saying what it does.
        description: en::tr!("Where device tokens are kept and read from: the OS keyring, a private 0600 file, or a custom shell command"),
        aliases: &["credential backend", "keyring", "unlock", "token store", "password manager", "secrets"],
        section: BEHAVIOUR,
        kind: Kind::Choice { default: "keyring", options: CREDENTIAL_BACKENDS, indexed: false },
        scope: G,
    },
    Spec {
        name: "credentialscommand",
        key: "credentials/command",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Shell command that prints the device token when Credential store is Custom command (uses {url}, {host}, CLARP_SERVER_URL, CLARP_SERVER_HOST)
        label: en::tr!("Credential lookup command"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Credential lookup command” saying what it does. {host}, {url} are written as they are (names the command receives)
        description: en::tr!("Shell command that prints the device token when Credential store is Custom command (uses {url}, {host}, CLARP_SERVER_URL, CLARP_SERVER_HOST)"),
        aliases: &["credential command", "unlock command", "token command", "lookup command", "secret command"],
        section: BEHAVIOUR,
        kind: Kind::Text { default: "" },
        scope: G,
    },
    Spec {
        name: "credentialsstorecommand",
        key: "credentials/storeCommand",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Optional shell command run when pairing to save a token; reads the token from standard input and receives {url}, {host}, CLARP_SERVER_URL, CLARP_SERVER_HOST
        label: en::tr!("Credential store command"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Credential store command” saying what it does. {host}, {url} are written as they are (names the command receives)
        description: en::tr!("Optional shell command run when pairing to save a token; reads the token from standard input and receives {url}, {host}, CLARP_SERVER_URL, CLARP_SERVER_HOST"),
        aliases: &["store command", "save token command", "credential save command"],
        section: BEHAVIOUR,
        kind: Kind::Text { default: "" },
        scope: G,
    },
    Spec {
        name: "credentialsremovecommand",
        key: "credentials/removeCommand",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Optional shell command run when forgetting a Host's token (uses {url}, {host}, CLARP_SERVER_URL, CLARP_SERVER_HOST)
        label: en::tr!("Credential remove command"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Credential remove command” saying what it does. {host}, {url} are written as they are (names the command receives)
        description: en::tr!("Optional shell command run when forgetting a Host's token (uses {url}, {host}, CLARP_SERVER_URL, CLARP_SERVER_HOST)"),
        aliases: &["remove command", "delete token command", "forget token command"],
        section: BEHAVIOUR,
        kind: Kind::Text { default: "" },
        scope: G,
    },
    Spec {
        name: "credentialsfile",
        key: "credentials/file",
        // i18n: Settings page and Ctrl+K, a preference's name, the row label. What it does: Optional path for the private 0600 token file when Credential store is Private file (defaults to credentials.json in the config folder)
        label: en::tr!("Credential file path"),
        // i18n: Settings page and Ctrl+K, the line under the preference “Credential file path” saying what it does.
        description: en::tr!("Optional path for the private 0600 token file when Credential store is Private file (defaults to credentials.json in the config folder)"),
        aliases: &["credential file", "token file", "credentials path"],
        section: BEHAVIOUR,
        kind: Kind::Text { default: "" },
        scope: G,
    },
];

pub fn specs() -> &'static [Spec] {
    SPECS
}

fn squash(text: &str) -> String {
    text.chars().filter(|c| c.is_alphanumeric()).flat_map(char::to_lowercase).collect()
}

/// The setting called `name`: its short name, its key, its label or one of
/// its aliases, ignoring case, spaces, dashes and dots ("font-size",
/// "Font size", "fontsize" all find it). Names, keys and labels win over
/// aliases, which several settings may share.
pub fn find(name: &str) -> Option<&'static Spec> {
    let wanted = squash(name);
    if wanted.is_empty() {
        return None;
    }
    SPECS
        .iter()
        .find(|s| squash(s.name) == wanted || (s.scope == Scope::Global && squash(s.key) == wanted) || squash(s.label) == wanted)
        .or_else(|| SPECS.iter().find(|s| s.aliases.iter().any(|a| squash(a) == wanted)))
}

pub fn section(name: &str) -> Vec<&'static Spec> {
    SPECS.iter().filter(|s| s.section.eq_ignore_ascii_case(name)).collect()
}

/// The reading theme the settings choose (normalized).
pub fn theme_of(settings: &Settings) -> String {
    reading_theme::normalized_theme_id(&settings.string(THEME_KEY, reading_theme::default_theme_id()))
}

/// The theme's options as (id, label), for the reading theme's choice.
fn options_of(spec: &Spec) -> Vec<(String, String)> {
    match spec.kind {
        Kind::Choice { options, .. } if spec.name == "readingtheme" && options.is_empty() => reading_theme::themes()
            .iter()
            .map(|t| {
                let text = |k: &str| t.get(k).and_then(Value::as_str).unwrap_or_default().to_owned();
                (text("id"), text("label"))
            })
            .collect(),
        Kind::Choice { options, .. } => options.iter().map(|(id, label)| ((*id).to_owned(), (*label).to_owned())).collect(),
        _ => Vec::new(),
    }
}

/// The default, which for theme-scoped values is the theme's own.
pub fn default_value(spec: &Spec, theme: &str) -> Value {
    let theme_object = reading_theme::theme(theme);
    match spec.kind {
        Kind::Toggle { default, .. } => Value::Bool(default),
        Kind::Number { default, .. } if spec.name == "fontsize" => {
            json!(theme_object.get("fontPixelSize").and_then(Value::as_f64).unwrap_or(default))
        }
        Kind::Number { default, .. } => json!(default),
        Kind::Choice { default, .. } => Value::from(default),
        Kind::Color { role } => {
            let own = theme_object.get(role).and_then(Value::as_str);
            // The focused frame is the theme's own colour, else its accent.
            let own = if role == "focus" { own.or_else(|| theme_object.get("accent").and_then(Value::as_str)) } else { own };
            Value::from(own.unwrap_or("#000000"))
        }
        Kind::Text { default } => Value::from(default),
    }
}

/// The stored value as it is in the file (before validation), if any.
pub fn stored(settings: &Settings, spec: &Spec, theme: &str) -> Option<Value> {
    match spec.scope {
        Scope::Global => settings.get(spec.key).cloned(),
        Scope::Theme { field } => settings.get(spec.key)?.get(theme)?.get(field).cloned(),
    }
}

/// Whether `color` is `#rgb`, `#rrggbb` or `#aarrggbb`.
pub fn is_color(color: &str) -> bool {
    color.strip_prefix('#').is_some_and(|hex| matches!(hex.len(), 3 | 6 | 8) && hex.chars().all(|c| c.is_ascii_hexdigit()))
}

/// `#rgb` as `#rrggbb`, lower-cased.
pub fn normalized_color(color: &str) -> Option<String> {
    let hex = color.trim().strip_prefix('#')?;
    if !is_color(&format!("#{hex}")) {
        return None;
    }
    let hex = hex.to_ascii_lowercase();
    Some(if hex.len() == 3 { format!("#{}", hex.chars().flat_map(|c| [c, c]).collect::<String>()) } else { format!("#{hex}") })
}

/// `value` as the stored form of `spec`, or why it is not one. Toggles take
/// the value the user means (not the inverted stored form).
pub fn validate(spec: &Spec, value: &Value) -> Result<Value, String> {
    match spec.kind {
        Kind::Toggle { .. } => value.as_bool().map(Value::Bool).ok_or_else(|| {
            // i18n: Settings file problems and :set errors, shown on the settings page; {0} is the setting's short name (code), {1} the value given; true and false are written as they are
            tr!("{0} must be true or false, not {1}", spec.name, value)
        }),
        Kind::Number { min, max, unit, zero, .. } => {
            let n = value.as_f64().ok_or_else(|| {
                // i18n: Settings file problems and :set errors, shown on the settings page; {0} is the setting's short name (code), {1} the value given
                tr!("{0} must be a number, not {1}", spec.name, value)
            })?;
            if zero.is_some() && n == 0.0 {
                return Ok(json!(0.0));
            }
            if !(min..=max).contains(&n) {
                let off = zero
                    .map(|z| {
                        // i18n: Settings file problems and :set errors, shown on the settings page; added after the allowed range when 0 also works; {} is what 0 means (e.g. Full width)
                        tr!(" (or 0: {})", translated(z))
                    })
                    .unwrap_or_default();
                // i18n: Settings file problems and :set errors, shown on the settings page; {0} the setting's short name (code), {1} and {2} the least and most allowed, {3} the unit (px, %, ×, may be empty and is written right after {2}), {4} "(or 0: …)" or nothing, {5} the value given
                return Err(tr!("{0} must be between {1} and {2}{3}{4}, not {5}", spec.name, plain(min), plain(max), unit, off, plain(n)));
            }
            Ok(json!(n))
        }
        Kind::Choice { .. } => {
            let options = options_of(spec);
            let mut text = match value {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            };
            // Tool activity's "Hidden" became "Hide when done" (#653): the
            // stored index is the same, and the old word still means it.
            if spec.name == "toolactivity" && text.eq_ignore_ascii_case("hidden") {
                text = "hide-when-done".to_owned();
            }
            options
                .iter()
                .find(|(id, label)| id.eq_ignore_ascii_case(&text) || label.eq_ignore_ascii_case(&text))
                .map(|(id, _)| Value::from(id.clone()))
                .ok_or_else(|| {
                    let ids = options.iter().map(|(id, _)| id.as_str()).collect::<Vec<_>>().join(", ");
                    // i18n: Settings file problems and :set errors, shown on the settings page; {0} the setting's short name (code), {1} the allowed values (code, comma-separated), {2} the value given
                    tr!("{0} must be one of {1}, not {2}", spec.name, ids, value)
                })
        }
        Kind::Color { .. } => value
            .as_str()
            .and_then(normalized_color)
            .map(Value::from)
            .ok_or_else(|| {
                // i18n: Settings file problems and :set errors, shown on the settings page; {0} the setting's short name (code), {1} the value given; #1a2b3c is an example colour code, keep it
                tr!("{0} must be a colour like #1a2b3c, not {1}", spec.name, value)
            }),
        Kind::Text { default } => match value.as_str().map(str::trim) {
            Some(text) if !text.is_empty() || default.is_empty() => Ok(Value::from(text)),
            // i18n: Settings file problems and :set errors, shown on the settings page; {0} the setting's short name (code), {1} the value given
            _ => Err(tr!("{0} must be a non-empty text, not {1}", spec.name, value)),
        },
    }
}

/// The stored JSON as the value it means (an index as its option's id, an
/// inverted toggle as what it shows), if it is valid.
fn decode(spec: &Spec, raw: &Value) -> Result<Value, String> {
    match spec.kind {
        Kind::Toggle { inverted, .. } => validate(spec, raw).map(|v| Value::Bool(v.as_bool() == Some(!inverted))),
        Kind::Choice { indexed: true, .. } => {
            let options = options_of(spec);
            raw.as_i64()
                .and_then(|i| usize::try_from(i).ok())
                .and_then(|i| options.get(i))
                .map(|(id, _)| Value::from(id.clone()))
                .ok_or_else(|| {
                    // i18n: Settings file problems, shown on the settings page; {0} the setting's short name (code), {1} the highest allowed number, {2} the value found
                    tr!("{0} must be 0 to {1}, not {2}", spec.name, options.len().saturating_sub(1), raw)
                })
        }
        _ => validate(spec, raw),
    }
}

/// The meaning of a value as it is stored.
fn encode(spec: &Spec, value: &Value) -> Value {
    match spec.kind {
        Kind::Toggle { inverted: true, .. } => Value::Bool(value.as_bool() != Some(true)),
        Kind::Choice { indexed: true, .. } => {
            let id = value.as_str().unwrap_or_default();
            json!(options_of(spec).iter().position(|(o, _)| o == id).unwrap_or(0))
        }
        _ => value.clone(),
    }
}

/// The setting's current value: the stored one when valid, else the default.
pub fn current(settings: &Settings, spec: &Spec, theme: &str) -> Value {
    stored(settings, spec, theme).and_then(|raw| decode(spec, &raw).ok()).unwrap_or_else(|| default_value(spec, theme))
}

pub fn number_of(settings: &Settings, name: &str) -> f64 {
    find(name).map(|spec| current(settings, spec, &theme_of(settings))).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

pub fn flag(settings: &Settings, name: &str) -> bool {
    find(name).map(|spec| current(settings, spec, &theme_of(settings))).and_then(|v| v.as_bool()).unwrap_or(false)
}

pub fn choice_of(settings: &Settings, name: &str) -> String {
    find(name).map(|spec| current(settings, spec, &theme_of(settings))).and_then(|v| v.as_str().map(str::to_owned)).unwrap_or_default()
}

pub fn text_of(settings: &Settings, name: &str) -> String {
    find(name).map(|spec| current(settings, spec, &theme_of(settings))).and_then(|v| v.as_str().map(str::to_owned)).unwrap_or_default()
}

/// Whether the setting is at its default (nothing stored, or the default).
pub fn is_default(settings: &Settings, spec: &Spec, theme: &str) -> bool {
    current(settings, spec, theme) == default_value(spec, theme)
}

/// A number without a pointless ".0".
fn plain(n: f64) -> String {
    if n.fract() == 0.0 { format!("{n:.0}") } else { format!("{}", (n * 100.0).round() / 100.0) }
}

/// The value as people read it: "On", "16 px", "Comfortable", "#1a2b3c".
pub fn display(spec: &Spec, value: &Value) -> String {
    match spec.kind {
        Kind::Toggle { .. } => (if value.as_bool() == Some(true) { "On" } else { "Off" }).to_owned(),
        Kind::Number { unit, zero, .. } => {
            let n = value.as_f64().unwrap_or(0.0);
            match zero {
                Some(z) if n == 0.0 => z.to_owned(),
                _ if unit == "×" => format!("{}×", plain(n)),
                _ if unit == "%" => format!("{}%", plain(n)),
                _ => format!("{} {unit}", plain(n)),
            }
        }
        Kind::Choice { .. } => {
            let id = value.as_str().unwrap_or_default();
            options_of(spec).into_iter().find(|(o, _)| o == id).map_or_else(|| id.to_owned(), |(_, label)| label)
        }
        Kind::Color { .. } | Kind::Text { .. } => value.as_str().unwrap_or_default().to_owned(),
    }
}

/// The current value as people read it, in the app's language.
pub fn shown(settings: &Settings, spec: &Spec) -> String {
    let theme = theme_of(settings);
    display_local(spec, &current(settings, spec, &theme))
}

/// `display` in the app's language: the words translated, the number with
/// the language's decimal separator. (`display` stays English for the
/// settings file's reference.)
pub fn display_local(spec: &Spec, value: &Value) -> String {
    use crate::i18n::{self, format};
    match spec.kind {
        Kind::Toggle { .. } => {
            if value.as_bool() == Some(true) {
                // i18n: Settings and Ctrl+K, the value of a switch that is on (adjective, not a verb; the same word as the quick switcher's "On" pill)
                tr!("On")
            } else {
                // i18n: Settings and Ctrl+K, the value of a switch that is off (adjective, not a verb)
                tr!("Off")
            }
        }
        Kind::Number { unit, zero, .. } => {
            let n = value.as_f64().unwrap_or(0.0);
            if let (Some(z), true) = (zero, n == 0.0) {
                return translated(z);
            }
            // As many decimals as `plain` writes: none, one or two.
            let rounded = (n * 100.0).round() / 100.0;
            let whole = |x: f64| (x.round() - x).abs() < 1e-9;
            let places = if whole(rounded) { 0 } else if whole(rounded * 10.0) { 1 } else { 2 };
            let number = format::decimal(i18n::current(), rounded, places);
            match unit {
                // i18n: Settings, a size relative to normal; {} is the number, × means times
                "×" => tr!("{}×", number),
                // i18n: Settings, a percentage value; {} is the number
                "%" => tr!("{}%", number),
                // i18n: Settings, a size or distance value; {} is the number, "px" is pixels
                "px" => tr!("{} px", number),
                // i18n: Settings > Line length, a length in characters; {} is the number, "ch" is characters
                "ch" => tr!("{} ch", number),
                // i18n: Settings, a time value; {} is the number, "ms" is milliseconds
                "ms" => tr!("{} ms", number),
                "" => number,
                unit => format!("{number} {unit}"),
            }
        }
        Kind::Choice { .. } => {
            let id = value.as_str().unwrap_or_default();
            match spec.kind {
                // The reading themes' names come from their files.
                Kind::Choice { options, .. } if !options.is_empty() => options.iter().find(|(o, _)| *o == id).map_or_else(|| id.to_owned(), |(_, label)| translated(label)),
                _ => display(spec, value),
            }
        }
        Kind::Color { .. } | Kind::Text { .. } => display(spec, value),
    }
}

/// Stores `value` (already the meaning, e.g. `true` for "Spoken replies"
/// on). Storing the default removes the key, so the file holds only what
/// the user changed. Theme-scoped values touch only `theme`'s entry.
pub fn set(settings: &mut Settings, spec: &Spec, theme: &str, value: Value) -> Result<(), String> {
    let value = validate(spec, &value)?;
    if value == default_value(spec, theme) && spec.scope != Scope::Global {
        reset(settings, spec, theme);
        return Ok(());
    }
    let raw = encode(spec, &value);
    match spec.scope {
        Scope::Global => {
            if value == default_value(spec, theme) {
                settings.remove(spec.key);
            } else {
                settings.set(spec.key, raw);
            }
        }
        Scope::Theme { field } => {
            let mut all = settings.get(spec.key).and_then(Value::as_object).cloned().unwrap_or_default();
            let entry = all.entry(theme.to_owned()).or_insert_with(|| Value::Object(Map::new()));
            if !entry.is_object() {
                *entry = Value::Object(Map::new());
            }
            if let Some(entry) = entry.as_object_mut() {
                entry.insert(field.to_owned(), raw);
            }
            settings.set(spec.key, Value::Object(all));
        }
    }
    Ok(())
}

/// Back to the default (for a theme-scoped value: this theme's only).
pub fn reset(settings: &mut Settings, spec: &Spec, theme: &str) {
    match spec.scope {
        Scope::Global => {
            if settings.get(spec.key).is_some() {
                settings.remove(spec.key);
            }
        }
        Scope::Theme { field } => {
            let Some(mut all) = settings.get(spec.key).and_then(Value::as_object).cloned() else { return };
            let Some(entry) = all.get_mut(theme).and_then(Value::as_object_mut) else { return };
            if entry.remove(field).is_none() {
                return;
            }
            if entry.is_empty() {
                all.remove(theme);
            }
            if all.is_empty() {
                settings.remove(spec.key);
            } else {
                settings.set(spec.key, Value::Object(all));
            }
        }
    }
}

/// Every setting of `section` back to its default; returns how many changed.
pub fn reset_section(settings: &mut Settings, section_name: &str, theme: &str) -> usize {
    let mut changed = 0;
    for spec in section(section_name) {
        changed += usize::from(!is_default(settings, spec, theme));
        reset(settings, spec, theme);
    }
    changed
}

/// Every setting back to its default; returns how many changed.
pub fn reset_all(settings: &mut Settings, theme: &str) -> usize {
    SECTIONS.iter().map(|s| reset_section(settings, s, theme)).sum()
}

/// Typed text as a value of `spec`: on/off/yes/no/true/false/toggle for a
/// switch, a number (or `+`/`-` to step), a choice's id or label, a colour.
pub fn parse(spec: &Spec, current_value: &Value, text: &str) -> Result<Value, String> {
    let text = text.trim();
    match spec.kind {
        Kind::Toggle { .. } => match text.to_ascii_lowercase().as_str() {
            "on" | "yes" | "true" | "1" | "show" | "enable" | "enabled" => Ok(Value::Bool(true)),
            "off" | "no" | "false" | "0" | "hide" | "disable" | "disabled" => Ok(Value::Bool(false)),
            "toggle" | "!" | "" => Ok(Value::Bool(current_value.as_bool() != Some(true))),
            // i18n: Settings file problems and :set errors, shown on the settings page; typed after :set for a switch; {0} the setting's short name (code), {1} what was typed; "on" and "off" are the words to type, keep them
            other => Err(tr!("{0}: say on or off, not {1}", spec.name, other)),
        },
        Kind::Number { step, .. } => {
            let n = current_value.as_f64().unwrap_or(0.0);
            let trimmed = text.trim_end_matches(|c: char| c.is_alphabetic() || c == '%' || c == '×' || c.is_whitespace());
            let value = match trimmed {
                "+" => n + step,
                "-" => n - step,
                _ => match trimmed.strip_prefix("+=").or_else(|| trimmed.strip_prefix("-=")) {
                    Some(delta) => {
                        let d: f64 = delta.trim().parse().map_err(|_| {
                            // i18n: Settings file problems and :set errors, shown on the settings page; {0} the setting's short name (code), {1} what was typed
                            tr!("{0}: {1} is not a number", spec.name, text)
                        })?;
                        if trimmed.starts_with('+') { n + d } else { n - d }
                    }
                    None => trimmed.parse().map_err(|_| {
                        // i18n: Settings file problems and :set errors, shown on the settings page; {0} the setting's short name (code), {1} what was typed
                        tr!("{0}: {1} is not a number", spec.name, text)
                    })?,
                },
            };
            validate(spec, &json!(value))
        }
        Kind::Choice { .. } => validate(spec, &Value::from(text)),
        Kind::Color { .. } | Kind::Text { .. } => validate(spec, &Value::from(text)),
    }
}

/// Typed text to the setting called `name`, stored (`:set fontsize=16`,
/// `:set timestamps`, `:set fontsize+=2`). Returns the setting.
pub fn set_text(settings: &mut Settings, name: &str, text: &str) -> Result<&'static Spec, String> {
    let spec = find(name).ok_or_else(|| {
        // i18n: :set command error, shown in the window; {} is the setting name typed (starts lower case, as an error line)
        tr!("no setting called {}", name)
    })?;
    let theme = theme_of(settings);
    let value = parse(spec, &current(settings, spec, &theme), text)?;
    set(settings, spec, &theme, value)?;
    Ok(spec)
}

/// `name=value`, `name+=n`, `name-=n`, `name!`, `noname`, `name` (as vim's
/// `:set` reads them) into the setting and its text.
pub fn split_assignment(line: &str) -> (String, String) {
    let line = line.trim();
    for op in ["+=", "-="] {
        if let Some((name, value)) = line.split_once(op) {
            return (name.trim().to_owned(), format!("{op}{}", value.trim()));
        }
    }
    if let Some((name, value)) = line.split_once(['=', ':']) {
        return (name.trim().to_owned(), value.trim().to_owned());
    }
    if let Some(name) = line.strip_suffix('!') {
        return (name.trim().to_owned(), "toggle".into());
    }
    if let Some(name) = line.strip_prefix("no").filter(|n| find(line).is_none() && find(n).is_some()) {
        return (name.to_owned(), "off".into());
    }
    match find(line).map(|s| s.kind) {
        Some(Kind::Toggle { .. }) => (line.to_owned(), "on".into()),
        _ => (line.to_owned(), String::new()),
    }
}

/// One step of `spec` in `delta`'s direction: a number moves by its step
/// (clamped), a switch flips, a choice moves to the next or previous
/// option. Colours and text do not step. Returns the new value.
pub fn adjust(settings: &mut Settings, spec: &Spec, theme: &str, delta: i32) -> Option<Value> {
    let now = current(settings, spec, theme);
    let next = match spec.kind {
        Kind::Toggle { .. } => Value::Bool(now.as_bool() != Some(true)),
        Kind::Number { min, max, step, zero, .. } => {
            let n = now.as_f64().unwrap_or(min);
            let moved = if zero.is_some() && n == 0.0 {
                if delta > 0 { min } else { return Some(now) }
            } else if zero.is_some() && n <= min && delta < 0 {
                0.0
            } else {
                // Onto the step grid, so 1.15 + 0.05 is 1.2, not 1.2000000000000002.
                let raw = n + step * f64::from(delta);
                ((raw / step).round() * step * 1000.0).round() / 1000.0
            };
            json!(if zero.is_some() && moved == 0.0 { 0.0 } else { moved.clamp(min, max) })
        }
        Kind::Choice { .. } => {
            let options = options_of(spec);
            if options.is_empty() {
                return None;
            }
            let id = now.as_str().unwrap_or_default();
            let at = options.iter().position(|(o, _)| o == id).unwrap_or(0) as i32;
            Value::from(options[(at + delta).rem_euclid(options.len() as i32) as usize].0.clone())
        }
        Kind::Color { .. } | Kind::Text { .. } => return None,
    };
    match set(settings, spec, theme, next.clone()) {
        Ok(()) => Some(next),
        Err(error) => {
            eprintln!("prefs: {error}");
            None
        }
    }
}

/// What is wrong with the stored settings, one line each; the window reads
/// each of these as its default meanwhile.
pub fn problems(settings: &Settings) -> Vec<String> {
    let theme = theme_of(settings);
    let mut out = Vec::new();
    for spec in SPECS {
        match spec.scope {
            Scope::Global => {
                if let Some(Err(error)) = settings.get(spec.key).map(|raw| decode(spec, raw)) {
                    // i18n: Settings file problems and :set errors, shown on the settings page; {0} is the problem (one of the "must be" lines), {1} the default value used instead
                    out.push(tr!("{0}; using {1}", error, display_local(spec, &default_value(spec, &theme))));
                }
            }
            Scope::Theme { .. } => {}
        }
    }
    for key in [FONT_OVERRIDES_KEY, COLOR_OVERRIDES_KEY] {
        let Some(all) = settings.get(key) else { continue };
        let Some(all) = all.as_object() else {
            // i18n: Settings file problems and :set errors, shown on the settings page; {0} a settings-file key (code), {1} the value found; "object of themes" is a JSON object keyed by reading theme
            out.push(tr!("{0} must be an object of themes, not {1}", key, all));
            continue;
        };
        for (theme_id, entry) in all {
            if reading_theme::normalized_theme_id(theme_id) != *theme_id {
                // i18n: Settings file problems and :set errors, shown on the settings page; {0} a settings-file key (code), {1} a reading theme's id that does not exist
                out.push(tr!("{0}: there is no reading theme called {1}", key, theme_id));
            }
            let Some(entry) = entry.as_object() else {
                // i18n: Settings file problems and :set errors, shown on the settings page; {0} a settings-file key and theme (code), {1} the value found; "object" is a JSON object
                out.push(tr!("{0} must be an object, not {1}", format!("{key}.{theme_id}"), entry));
                continue;
            };
            for (field, raw) in entry {
                let spec = SPECS.iter().find(|s| s.key == key && s.scope == Scope::Theme { field: leak_field(field) });
                match spec {
                    Some(spec) => {
                        if let Err(error) = validate(spec, raw) {
                            // i18n: Settings file problems and :set errors, shown on the settings page; {0} is the problem (one of the "must be" lines), {1} the reading theme's id; the theme's own value is used instead
                            out.push(tr!("{0} (theme {1}); using the theme's", error, theme_id));
                        }
                    }
                    // The font picker keeps the family here too.
                    None if key == FONT_OVERRIDES_KEY && field == "family" => {
                        if !raw.is_string() {
                            // i18n: Settings file problems and :set errors, shown on the settings page; {0} a settings-file key (code), {1} the value found
                            out.push(tr!("{0} must be a font name, not {1}", format!("{key}.{theme_id}.family"), raw));
                        }
                    }
                    // i18n: Settings file problems and :set errors, shown on the settings page; {0} a settings-file key and theme (code), {1} the entry's name that is not known
                    None => out.push(tr!("{0}: unknown entry {1}", format!("{key}.{theme_id}"), field)),
                }
            }
        }
    }
    out
}

/// The `&'static` field name equal to `field`, among the theme-scoped specs.
fn leak_field(field: &str) -> &'static str {
    SPECS
        .iter()
        .find_map(|s| match s.scope {
            Scope::Theme { field: f } if f == field => Some(f),
            _ => None,
        })
        .unwrap_or("")
}

/// The keys this registry owns (the rest of the file is the window's
/// state: drafts, layouts, key bindings, the connection).
pub fn keys() -> Vec<&'static str> {
    let mut keys: Vec<&'static str> = SPECS.iter().map(|s| s.key).collect();
    keys.sort_unstable();
    keys.dedup();
    keys
}

/// The preferences as a file to keep or share: every key this registry
/// owns that is set, plus a version and where it came from.
pub fn export(settings: &Settings) -> Value {
    let mut out = Map::new();
    out.insert("clarpSettingsVersion".into(), json!(1));
    for key in keys() {
        if let Some(value) = settings.get(key) {
            out.insert(key.to_owned(), value.clone());
        }
    }
    Value::Object(out)
}

/// Applies an exported file: its keys this registry owns that hold valid
/// values (for the per-theme objects, each valid entry); the rest is
/// reported, not applied. Keys missing from it keep their value. Returns
/// (applied values, problems).
pub fn import(settings: &mut Settings, file: &Value) -> (usize, Vec<String>) {
    let Some(object) = file.as_object() else {
        // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; the whole file was not read
        return (0, vec![tr!("the file is not a JSON object of settings")]);
    };
    let mut problems = Vec::new();
    let mut applied = 0;
    for (key, value) in object {
        if key == "clarpSettingsVersion" || key == "$schema" {
            continue;
        }
        if !keys().contains(&key.as_str()) {
            // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {} is a key in the file (code) that is no setting
            problems.push(tr!("{} is not a setting; skipped", key));
            continue;
        }
        if key == FONT_OVERRIDES_KEY || key == COLOR_OVERRIDES_KEY {
            let Some(themes) = value.as_object() else {
                // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {} a settings-file key (code); "object of themes" is a JSON object keyed by reading theme
                problems.push(tr!("{} must be an object of themes; skipped", key));
                continue;
            };
            let mut merged = settings.get(key).and_then(Value::as_object).cloned().unwrap_or_default();
            for (theme_id, entry) in themes {
                if reading_theme::normalized_theme_id(theme_id) != *theme_id {
                    // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {0} a settings-file key (code), {1} a reading theme's id that does not exist
                    problems.push(tr!("{0}: there is no reading theme called {1}; skipped", key, theme_id));
                    continue;
                }
                let Some(entry) = entry.as_object() else {
                    // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {} a settings-file key and theme (code); "object" is a JSON object
                    problems.push(tr!("{} must be an object; skipped", format!("{key}.{theme_id}")));
                    continue;
                };
                let target = merged.entry(theme_id.clone()).or_insert_with(|| Value::Object(Map::new()));
                if !target.is_object() {
                    *target = Value::Object(Map::new());
                }
                for (field, raw) in entry {
                    let spec = SPECS.iter().find(|s| s.key == key && s.scope == Scope::Theme { field: leak_field(field) });
                    let checked = match spec {
                        Some(spec) => validate(spec, raw),
                        None if key == FONT_OVERRIDES_KEY && field == "family" && raw.is_string() => Ok(raw.clone()),
                        // i18n: Settings file problems and :set errors, shown on the settings page; {0} a settings-file key and theme (code), {1} the entry's name that is not known
                        None => Err(tr!("{0}: unknown entry {1}", format!("{key}.{theme_id}"), field)),
                    };
                    match checked {
                        Ok(valid) => {
                            if let Some(target) = target.as_object_mut() {
                                target.insert(field.clone(), valid);
                                applied += 1;
                            }
                        }
                        Err(error) => {
                            // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {} is the problem (one of the "must be" lines); that value was not applied
                            problems.push(tr!("{}; skipped", error))
                        }
                    }
                }
            }
            merged.retain(|_, entry| entry.as_object().is_some_and(|e| !e.is_empty()));
            if !merged.is_empty() {
                settings.set(key, Value::Object(merged));
            }
            continue;
        }
        let spec = SPECS.iter().find(|s| s.key == key && s.scope == Scope::Global);
        match spec.map(|spec| decode(spec, value)) {
            Some(Ok(_)) => {
                settings.set(key, value.clone());
                applied += 1;
            }
            Some(Err(error)) => {
                // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {} is the problem (one of the "must be" lines); that value was not applied
                problems.push(tr!("{}; skipped", error))
            }
            // i18n: Settings > Import settings…, a problem with the imported file, listed on the settings page; {} is a key in the file (code) that is no setting
            None => problems.push(tr!("{} is not a setting; skipped", key)),
        }
    }
    (applied, problems)
}

/// A JSON Schema of the settings file, so an editor that reads
/// `"$schema"` explains, completes and checks every setting.
pub fn schema() -> Value {
    let mut properties = Map::new();
    for spec in SPECS {
        let doc = format!("{}: {} (:set {}; aliases: {})", spec.label, spec.description, spec.name, spec.aliases.join(", "));
        let theme = reading_theme::default_theme_id();
        let property = match (spec.scope, spec.kind) {
            (Scope::Theme { .. }, _) => continue,
            (_, Kind::Toggle { default, inverted }) => json!({"type": "boolean", "default": default != inverted}),
            (_, Kind::Number { default, min, max, zero, .. }) => {
                let minimum = if zero.is_some() { 0.0 } else { min };
                json!({"type": "number", "minimum": minimum, "maximum": max, "default": default})
            }
            (_, Kind::Choice { indexed: true, .. }) => {
                json!({"type": "integer", "minimum": 0, "maximum": options_of(spec).len().saturating_sub(1), "default": encode(spec, &default_value(spec, theme))})
            }
            (_, Kind::Choice { .. }) => {
                json!({"type": "string", "enum": options_of(spec).into_iter().map(|(id, _)| id).collect::<Vec<_>>(), "default": default_value(spec, theme)})
            }
            (_, Kind::Color { .. }) => json!({"type": "string", "pattern": "^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"}),
            (_, Kind::Text { default }) if default.is_empty() => json!({"type": "string", "default": default}),
            (_, Kind::Text { default }) => json!({"type": "string", "minLength": 1, "default": default}),
        };
        let mut property = property;
        if let Some(object) = property.as_object_mut() {
            object.insert("description".into(), Value::from(doc));
        }
        properties.insert(spec.key.to_owned(), property);
    }
    let roles: Map<String, Value> = SPECS
        .iter()
        .filter_map(|s| match s.kind {
            Kind::Color { role } => Some((
                role.to_owned(),
                json!({"type": "string", "pattern": "^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", "description": format!("{}: {}", s.label, s.description)}),
            )),
            _ => None,
        })
        .collect();
    properties.insert(
        COLOR_OVERRIDES_KEY.into(),
        json!({"type": "object", "description": "Colour overrides per reading theme: {\"terminal\": {\"text\": \"#ffffff\"}}",
               "additionalProperties": {"type": "object", "properties": roles, "additionalProperties": false}}),
    );
    properties.insert(
        FONT_OVERRIDES_KEY.into(),
        json!({"type": "object", "description": "Font per reading theme: {\"terminal\": {\"family\": \"Iosevka\", \"size\": 16}}",
               "additionalProperties": {"type": "object", "properties": {
                   "family": {"type": "string"},
                   "size": {"type": "number", "minimum": 9, "maximum": 40, "description": "Font size: Size of message text in the chat (:set fontsize)"}}}}),
    );
    json!({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Clarp desktop settings",
        "description": "Every key is optional; a missing key is its default. The window reloads this file when it changes and reports invalid values without applying them. Keys not listed here are the window's own state (drafts, layouts, key bindings).",
        "type": "object",
        "properties": properties,
    })
}

/// The settings as a readable reference (Markdown): every setting with its
/// name, key, default, range and aliases.
pub fn reference() -> String {
    let theme = reading_theme::default_theme_id();
    let mut out = String::from(
        "# Clarp desktop settings\n\nEvery setting is in Ctrl+K and in Settings (Ctrl+,), and can be set by name with `:set name=value`. \
         The settings file (`settings.json`) holds only what you changed; delete a key to go back to its default. \
         The window reloads the file when it changes and reports invalid values instead of applying them.\n\n\
         Colours and the font size belong to the reading theme: they are kept per theme under `appearance/colorOverrides` and `appearance/fontOverrides`.\n",
    );
    for section_name in SECTIONS {
        out.push_str(&format!("\n## {section_name}\n\n| Name | Setting | What it does | Default | Values | Key | Aliases |\n|---|---|---|---|---|---|---|\n"));
        for spec in section(section_name) {
            let values = match spec.kind {
                Kind::Toggle { .. } => "on / off".to_owned(),
                Kind::Number { min, max, step, unit, zero, .. } => {
                    format!("{}–{} {unit}, step {}{}", plain(min), plain(max), plain(step), zero.map(|z| format!("; 0 = {z}")).unwrap_or_default())
                }
                Kind::Choice { .. } => options_of(spec).into_iter().map(|(id, _)| id).collect::<Vec<_>>().join(", "),
                Kind::Color { .. } => "#rrggbb".into(),
                Kind::Text { .. } => "text".into(),
            };
            let key = match spec.scope {
                Scope::Global => format!("`{}`", spec.key),
                Scope::Theme { field } => format!("`{}.<theme>.{field}`", spec.key),
            };
            let default = match spec.kind {
                Kind::Color { .. } => "theme's".to_owned(),
                _ if spec.name == "fontsize" => "theme's".to_owned(),
                _ => display(spec, &default_value(spec, theme)),
            };
            out.push_str(&format!(
                "| `{}` | {} | {} | {} | {} | {} | {} |\n",
                spec.name,
                spec.label,
                spec.description,
                default,
                values,
                key,
                spec.aliases.join(", ")
            ));
        }
    }
    out
}

/// Rows of a colour picker for `role`: the theme's own palette, each
/// distinct colour once (role, hex).
pub fn palette(theme: &str) -> Vec<(String, String)> {
    let theme_object = reading_theme::theme(theme);
    let mut seen = std::collections::HashSet::new();
    SPECS
        .iter()
        .filter_map(|s| match s.kind {
            Kind::Color { role } => {
                let value = default_value(s, theme);
                let hex = value.as_str()?.to_owned();
                seen.insert(hex.clone()).then(|| (role.to_owned(), hex))
            }
            _ => None,
        })
        .chain(
            ["selection", "secondary"]
                .iter()
                .filter_map(|role| theme_object.get(*role).and_then(Value::as_str).map(|hex| ((*role).to_owned(), hex.to_owned()))),
        )
        .collect()
}

/// The theme with the user's colour overrides applied.
pub fn themed(settings: &Settings, theme: &str) -> crate::json::Object {
    let mut object = reading_theme::theme(theme).clone();
    for spec in SPECS {
        if let Kind::Color { role } = spec.kind {
            if let Some(value) = stored(settings, spec, theme).and_then(|v| validate(spec, &v).ok()) {
                object.insert(role.to_owned(), value);
            }
        }
    }
    object
}

/// The readability rules `role`'s colour now breaks in `theme` with the
/// user's overrides (a warning; the colour is still applied).
pub fn readability_warnings(settings: &Settings, theme: &str, role: &str) -> Vec<String> {
    use crate::readability::{MIN_STATUS_DELTA_E, measure, signal_separation};
    let object = themed(settings, theme);
    let mut out = Vec::new();
    for m in measure(&object) {
        if (m.foreground == role || m.surface.split(|c: char| !c.is_alphanumeric()).any(|w| w == role)) && !m.passes() {
            let (lc, ratio) = m.tier.minimum();
            // i18n: Settings > Colours, warning under a colour that is hard to read; {0} and {1} are colour role names (code), {2} and {5} APCA contrast (Lc) values, {3} and {6} contrast ratios, {4} the kind of text (e.g. body); keep "Lc" and ":1"
            out.push(tr!(
                "{0} on {1} is Lc {2} / {3}:1; {4} text needs Lc {5} / {6}:1",
                m.foreground,
                m.surface,
                format!("{:.0}", m.lc.abs()),
                format!("{:.1}", m.ratio),
                m.tier.name(),
                lc,
                ratio
            ));
        }
    }
    for (vision, (a, b), de) in signal_separation(&object) {
        if (a == role || b == role) && de < MIN_STATUS_DELTA_E {
            // i18n: Settings > Colours, warning under a colour; {0} and {1} are colour role names (code), {2} a kind of colour blindness, {3} the colour difference found, {4} the least needed; keep "ΔE"
            out.push(tr!("{0} and {1} are hard to tell apart with {2} (ΔE {3}, needs {4})", a, b, vision.name(), format!("{de:.0}"), MIN_STATUS_DELTA_E));
        }
    }
    out
}

/// A colour typed into the picker as a stored override for the current
/// theme, and the readability warnings it brings.
pub fn set_color(settings: &mut Settings, role: &str, text: &str) -> Result<Vec<String>, String> {
    let spec = SPECS.iter().find(|s| s.kind == Kind::Color { role: leak_role(role) }).ok_or_else(|| format!("no colour role {role}"))?;
    let theme = theme_of(settings);
    set(settings, spec, &theme, Value::from(text))?;
    Ok(readability_warnings(settings, &theme, role))
}

fn leak_role(role: &str) -> &'static str {
    SPECS
        .iter()
        .find_map(|s| match s.kind {
            Kind::Color { role: r } if r == role => Some(r),
            _ => None,
        })
        .unwrap_or("")
}

/// The words a search can match a setting by: label, name, aliases,
/// section and description.
pub fn search_words(spec: &Spec) -> String {
    format!("{} {} {} {} {}", spec.label, spec.name, spec.aliases.join(" "), spec.section, spec.description).to_lowercase()
}
