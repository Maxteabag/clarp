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

/// The sections in the order the Settings view shows them.
pub const SECTIONS: &[&str] = &[TYPOGRAPHY, LAYOUT, CHAT, COLOURS, BEHAVIOUR];

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

const WEIGHTS: &[(&str, &str)] = &[("300", "Light"), ("400", "Regular"), ("500", "Medium"), ("600", "Semibold")];
const DENSITIES: &[(&str, &str)] = &[("compact", "Compact"), ("comfortable", "Comfortable"), ("spacious", "Spacious")];
const CLOCKS: &[(&str, &str)] = &[("12h", "12-hour (6:10 PM)"), ("24h", "24-hour (18:10)")];
const ACTIVITY: &[(&str, &str)] = &[("grouped", "Grouped"), ("visible", "Always visible"), ("group-old", "Group old")];
const DETAIL: &[(&str, &str)] =
    &[("raw", "Raw (no AI)"), ("brief", "Brief"), ("plain", "Plain English"), ("explained", "Explained"), ("teaching", "Teaching")];
const LINKS: &[(&str, &str)] = &[("browser", "Open in the browser"), ("copy", "Copy the address")];
/// The avatar view's sizes (slint-app `avatar_view::CHOICES`).
const AVATARS: &[(&str, &str)] = &[("small", "Small · 36 px"), ("medium", "Medium · 44 px"), ("large", "Large · 52 px")];
const SCROLLBARS: &[(&str, &str)] = &[("auto", "When scrollable"), ("always", "Always"), ("never", "Never")];

macro_rules! color {
    ($name:literal, $role:literal, $label:literal, $description:literal, [$($alias:literal),*]) => {
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
        label: "Font size",
        description: "Size of message text in the chat, for the current reading theme",
        aliases: &["text size", "bigger", "smaller", "larger", "font-size", "body size", "reading size", "type size"],
        section: TYPOGRAPHY,
        kind: Kind::Number { default: 15.0, min: 9.0, max: 40.0, step: 1.0, unit: "px", zero: None },
        scope: Scope::Theme { field: "size" },
    },
    Spec {
        name: "chromesize",
        key: "typography/chromeScale",
        label: "Interface label size",
        description: "Size of labels, lists, titles and bars around the chat, relative to normal",
        aliases: &["ui text", "chrome text", "label size", "menu text", "sidebar text", "small text"],
        section: TYPOGRAPHY,
        kind: number(100.0, 70.0, 180.0, 5.0, "%"),
        scope: G,
    },
    Spec {
        name: "codefont",
        key: "typography/codeFamily",
        label: "Code font",
        description: "Font family for code blocks, commands and tool output",
        aliases: &["monospace", "mono font", "code typeface", "terminal font", "fixed width"],
        section: TYPOGRAPHY,
        kind: Kind::Text { default: "JetBrains Mono" },
        scope: G,
    },
    Spec {
        name: "codesize",
        key: "typography/codeScale",
        label: "Code size",
        description: "Size of code blocks relative to the message text",
        aliases: &["code text size", "monospace size", "snippet size", "code zoom"],
        section: TYPOGRAPHY,
        kind: number(90.0, 60.0, 150.0, 5.0, "%"),
        scope: G,
    },
    Spec {
        name: "paragraphspacing",
        key: "typography/paragraphSpacing",
        label: "Paragraph spacing",
        description: "Space between paragraphs, lists, code and headings in a message (lines inside a paragraph keep the font's own spacing)",
        aliases: &["line spacing", "linespacing", "line height", "leading", "block spacing", "paragraph gap"],
        section: TYPOGRAPHY,
        kind: number(8.0, 0.0, 40.0, 2.0, "px"),
        scope: G,
    },
    Spec {
        name: "headingscale",
        key: "typography/headingScale",
        label: "Heading size",
        description: "Size of a message's top-level headings relative to its text (smaller headings follow)",
        aliases: &["heading scale", "title size", "h1", "headline size", "header size"],
        section: TYPOGRAPHY,
        kind: number(140.0, 100.0, 250.0, 10.0, "%"),
        scope: G,
    },
    Spec {
        name: "measure",
        key: "typography/measure",
        label: "Line length",
        description: "Longest line of message text, in characters; 0 uses the pane's full width",
        aliases: &["measure", "column width", "text width", "max width", "reading width", "characters per line"],
        section: TYPOGRAPHY,
        kind: Kind::Number { default: 0.0, min: 40.0, max: 240.0, step: 10.0, unit: "ch", zero: Some("Full width") },
        scope: G,
    },
    Spec {
        name: "fontweight",
        key: "typography/weight",
        label: "Text weight",
        description: "Default weight of interface text without its own weight (Slint's rich message text keeps the face's regular weight)",
        aliases: &["font weight", "boldness", "bold", "light", "thickness", "heaviness"],
        section: TYPOGRAPHY,
        kind: Kind::Choice { default: "400", options: WEIGHTS, indexed: false },
        scope: G,
    },
    // ---- layout
    Spec {
        name: "uiscale",
        key: "appearance/uiScale",
        label: "Interface scale",
        description: "Scale of the whole window, text, icons and spacing alike (Ctrl+= / Ctrl+-)",
        aliases: &["ui scale", "scale", "dpi", "window size", "interface size", "larger interface", "smaller interface"],
        section: LAYOUT,
        kind: number(1.15, 0.5, 3.0, 0.05, "×"),
        scope: G,
    },
    Spec {
        name: "density",
        key: "layout/density",
        label: "Density",
        description: "How much padding rows and lists get: compact fits more, spacious breathes",
        aliases: &["compact", "padding", "row padding", "spacing", "tight", "roomy", "comfortable"],
        section: LAYOUT,
        kind: Kind::Choice { default: "comfortable", options: DENSITIES, indexed: false },
        scope: G,
    },
    Spec {
        name: "explorerwidth",
        key: "layout/explorerWidth",
        label: "Explorer width",
        description: "Width of the chat list on the left, when it shows names and previews",
        aliases: &["sidebar width", "chat list width", "left panel width", "agents list width", "navigator width"],
        section: LAYOUT,
        kind: number(320.0, 180.0, 720.0, 20.0, "px"),
        scope: G,
    },
    Spec {
        name: "compactexplorer",
        key: "explorer/compact",
        label: "Compact explorer",
        description: "Show only each agent's avatar and name in the chat list",
        aliases: &["slim sidebar", "narrow sidebar", "names only", "compact sidebar", "collapse previews"],
        section: LAYOUT,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "avatarsize",
        key: "appearance/avatarSize",
        label: "Agent picture size",
        description: "Size of the agents' portraits in the explorer, compact rows and cards",
        aliases: &["avatar size", "portrait size", "picture size", "profile picture", "face size", "photo"],
        section: LAYOUT,
        kind: Kind::Choice { default: "medium", options: AVATARS, indexed: false },
        scope: G,
    },
    Spec {
        name: "bubblewidth",
        key: "layout/bubbleWidth",
        label: "Bubble width",
        description: "Widest your own messages get, as a share of the chat's width",
        aliases: &["message width", "user bubble", "my messages width", "balloon width"],
        section: LAYOUT,
        kind: number(75.0, 30.0, 100.0, 5.0, "%"),
        scope: G,
    },
    Spec {
        name: "messagespacing",
        key: "layout/messageSpacing",
        label: "Message spacing",
        description: "Space between one message and the next",
        aliases: &["message gap", "row spacing", "gap between messages", "message padding", "vertical spacing"],
        section: LAYOUT,
        kind: number(14.0, 0.0, 48.0, 2.0, "px"),
        scope: G,
    },
    Spec {
        name: "panegap",
        key: "layout/paneGap",
        label: "Pane gap",
        description: "Space between split panes",
        aliases: &["split gap", "gutter", "pane spacing", "gap between panes", "split spacing"],
        section: LAYOUT,
        kind: number(4.0, 0.0, 32.0, 1.0, "px"),
        scope: G,
    },
    Spec {
        name: "borderwidth",
        key: "layout/borderWidth",
        label: "Border thickness",
        description: "Thickness of the outline around panes that do not have the keyboard",
        aliases: &["border", "outline", "frame width", "stroke", "line thickness", "unfocused border"],
        section: LAYOUT,
        kind: number(1.5, 0.0, 6.0, 0.5, "px"),
        scope: G,
    },
    Spec {
        name: "focusborderwidth",
        key: "layout/focusBorderWidth",
        label: "Focused border thickness",
        description: "Thickness of the outline around the pane that has the keyboard",
        aliases: &["focus ring", "active border", "focused outline", "focus width", "keyboard outline"],
        section: LAYOUT,
        kind: number(2.5, 0.5, 8.0, 0.5, "px"),
        scope: G,
    },
    Spec {
        name: "radius",
        key: "layout/radius",
        label: "Corner radius",
        description: "Roundness of pane outlines, bubbles and cards; 0 is square",
        aliases: &["rounding", "rounded corners", "border radius", "corners", "roundness"],
        section: LAYOUT,
        kind: number(6.0, 0.0, 20.0, 1.0, "px"),
        scope: G,
    },
    Spec {
        name: "scrollbarwidth",
        key: "layout/scrollbarWidth",
        label: "Scrollbar width",
        description: "Thickness of the chat's scrollbar thumb (it doubles under the pointer)",
        aliases: &["scroll bar", "scroller", "thumb width", "scroll thumb"],
        section: LAYOUT,
        kind: number(3.0, 1.0, 16.0, 1.0, "px"),
        scope: G,
    },
    Spec {
        name: "scrollbar",
        key: "layout/scrollbar",
        label: "Scrollbar",
        description: "When the chat shows its scrollbar",
        aliases: &["show scrollbar", "hide scrollbar", "scroll bar visibility", "scroller"],
        section: LAYOUT,
        kind: Kind::Choice { default: "auto", options: SCROLLBARS, indexed: false },
        scope: G,
    },
    Spec {
        name: "navrail",
        key: "appearance/navRail",
        label: "Activity bar",
        description: "The icon rail on the far left (Chats, Updates, Teams, Settings)",
        aliases: &["navigation rail", "nav rail", "left bar", "icons", "rail", "activity bar", "hide", "show", "toggle"],
        section: LAYOUT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "workspacebar",
        key: "appearance/workspaceBar",
        label: "Workspace bar",
        description: "The strip of workspace tabs across the top",
        aliases: &["workspaces", "tabs", "top strip", "tab bar", "hide", "show"],
        section: LAYOUT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "shortcutbar",
        key: "appearance/shortcutsVisible",
        label: "Shortcut bar",
        description: "The bar along the bottom with the keys that work where the keyboard is, and the connection",
        aliases: &["key hints", "keybindings bar", "status line", "status bar", "footer", "hints", "hide", "show"],
        section: LAYOUT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "minimalui",
        key: "appearance/minimalUi",
        label: "Minimal UI",
        description: "Hide the chrome around the chat: titles, frames and bars",
        aliases: &["zen mode", "distraction free", "focus mode", "clean", "hide chrome"],
        section: LAYOUT,
        kind: toggle(false),
        scope: G,
    },
    // ---- chat
    Spec {
        name: "timestamps",
        key: "conversation/timestampsVisible",
        label: "Timestamps",
        description: "Show the time under each message",
        aliases: &["times", "dates", "message time", "clock", "show time"],
        section: CHAT,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "chatzoom",
        key: "conversation/chatZoom",
        label: "Chat zoom",
        description: "How large everything in the chats is drawn (text, code, cards and spacing), apart from the rest of the window",
        aliases: &["zoom", "zoom in", "zoom out", "magnify", "bigger", "smaller", "text size", "chat size", "message size", "enlarge"],
        section: CHAT,
        kind: number(100.0, 60.0, 250.0, 10.0, "%"),
        scope: G,
    },
    Spec {
        name: "timeformat",
        key: "conversation/clock",
        label: "Time format",
        description: "12- or 24-hour clock for message and chat-list times",
        aliases: &["clock", "24 hour", "12 hour", "am pm", "military time", "time style"],
        section: CHAT,
        kind: Kind::Choice { default: "12h", options: CLOCKS, indexed: false },
        scope: G,
    },
    Spec {
        name: "foldprompts",
        key: "conversation/foldAgentPrompts",
        label: "Fold agent prompts",
        description: "Show prompts from other agents as one line until opened",
        aliases: &["collapse prompts", "agent prompts folded", "fold handoffs", "expand prompts", "collapse"],
        section: CHAT,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "toolactivity",
        key: "conversation/activityDisplayMode",
        label: "Tool activity",
        description: "How tool calls show: grouped under a 'Worked for' line, always visible, or only old ones grouped",
        aliases: &["tool calls", "worked for", "fold tools", "collapse tools", "expand tools", "grouping"],
        section: CHAT,
        kind: Kind::Choice { default: "grouped", options: ACTIVITY, indexed: true },
        scope: G,
    },
    Spec {
        name: "tooldetail",
        key: "experiments/toolDetailLevel",
        label: "Tool detail",
        description: "How tool rows are explained: raw commands, or plain English at several depths (uses AI)",
        aliases: &["narration", "explanations", "plain english", "tool rows detail", "audience"],
        section: CHAT,
        kind: Kind::Choice { default: "raw", options: DETAIL, indexed: true },
        scope: G,
    },
    Spec {
        name: "showwhenready",
        key: "conversation/showWhenReady",
        label: "Show when ready",
        description: "Show an answer once it is complete instead of as it streams",
        aliases: &["streaming", "typing", "wait for answer", "no streaming"],
        section: CHAT,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "readingtheme",
        key: THEME_KEY,
        label: "Reading theme",
        description: "The colours and font the chat is drawn in",
        aliases: &["theme", "colour scheme", "color scheme", "dark mode", "light mode", "appearance", "skin"],
        section: COLOURS,
        kind: Kind::Choice { default: "terminal", options: &[], indexed: false },
        scope: G,
    },
    // ---- colours: every role the window draws, per reading theme
    color!("color.text", "text", "Message text", "Colour of message text", ["text colour", "foreground", "font colour"]),
    color!("color.window", "window", "Background", "Colour of the chat's and panes' background", ["background", "bg", "backdrop"]),
    color!("color.raised", "raised", "Raised surface", "Colour of the explorer, dialogs and bars", ["panel", "sidebar background", "surface"]),
    color!("color.sunken", "sunken", "Sunken surface", "Colour behind code blocks and fields", ["code background", "inset", "well"]),
    color!("color.control", "control", "Controls", "Colour of buttons, fields and the selected chat", ["button", "field", "selected row"]),
    color!("color.hover", "hover", "Hover highlight", "Colour of a row under the pointer", ["hover background", "mouse over", "highlight"]),
    color!("color.border", "border", "Borders", "Colour of outlines around panes and controls", ["outline", "frame", "stroke"]),
    color!("color.rule", "rule", "Rules and separators", "Colour of separators and table lines", ["separator", "divider", "lines"]),
    color!("color.chrometext", "chromeText", "Interface text", "Colour of names, titles and labels around the chat", ["chrome text", "labels", "titles"]),
    color!("color.muted", "mutedText", "Secondary text", "Colour of secondary text: previews, details, quotes", ["muted text", "grey text", "dim text"]),
    color!("color.faint", "faintText", "Faint text", "Colour of the quietest text: stamps, hints", ["faint text", "hint text", "timestamp colour"]),
    color!("color.accent", "accent", "Accent", "Colour of highlights, the cursor, switches and section titles", ["highlight", "primary", "brand"]),
    color!("color.accenttext", "accentText", "Text on accent", "Colour of text drawn on the accent", ["on accent", "badge text", "contrast text"]),
    color!("color.focus", "focus", "Focused pane outline", "Colour of the outline of the pane that has the keyboard", ["focus ring", "active pane", "keyboard outline"]),
    color!("color.bubble", "bubble", "Your bubble", "Colour behind your own messages", ["user bubble", "my messages", "balloon"]),
    color!("color.link", "link", "Links", "Colour of links in messages", ["hyperlink", "url", "anchor"]),
    color!("color.success", "success", "Success", "Colour of working agents and things that went well", ["green", "ok", "busy"]),
    color!("color.warning", "warning", "Warning", "Colour of warnings and stopped answers", ["yellow", "caution", "orange"]),
    color!("color.danger", "danger", "Danger", "Colour of errors and failed messages", ["red", "error", "failure"]),
    color!("color.dangersurface", "dangerSurface", "Error background", "Colour behind error banners", ["error background", "alert surface", "danger surface"]),
    // ---- behaviour
    Spec {
        name: "follow",
        key: "conversation/followNewMessages",
        label: "Follow new messages",
        description: "Keep the chat scrolled to the newest message as answers arrive, while you are at the bottom",
        aliases: &["autoscroll", "auto scroll", "stick to bottom", "tail", "jump to new", "scroll lock"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "scrollspeed",
        key: "behaviour/scrollSpeed",
        label: "Scroll speed",
        description: "How far the arrow keys, J/K and Page Up/Down move the chat, relative to normal",
        aliases: &["scroll step", "scrolling speed", "page speed", "line step", "behavior"],
        section: BEHAVIOUR,
        kind: number(100.0, 25.0, 400.0, 25.0, "%"),
        scope: G,
    },
    Spec {
        name: "doublepress",
        key: "keymap/doublePressMs",
        label: "Double-press window",
        description: "How quickly a key must be pressed twice to count as a double press",
        aliases: &["double tap", "double click", "key repeat window", "chord timeout"],
        section: BEHAVIOUR,
        kind: number(300.0, 150.0, 1000.0, 50.0, "ms"),
        scope: G,
    },
    Spec {
        name: "previewonmove",
        key: "explorer/livePreview",
        label: "Preview on move",
        description: "Show a chat as the explorer's cursor moves onto it, before Enter",
        aliases: &["live preview", "preview while browsing", "peek", "follow cursor"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "notifyreplies",
        key: "notifications/replies",
        label: "Reply notifications",
        description: "A desktop notification when an agent answers in a chat that is not open",
        aliases: &["notifications", "alerts", "notify", "popups", "toasts", "desktop notifications"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "notifysound",
        key: "notifications/sound",
        label: "Notification sound",
        description: "Ask the desktop to play its message sound with each notification",
        aliases: &["sounds", "chime", "ding", "audio alert", "beep"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "pausemobile",
        key: "notifications/pauseMobileWhileDesktopActive",
        label: "Pause phone alerts",
        description: "Hold back phone notifications while you are active at this desktop",
        aliases: &["mobile push", "phone notifications", "quiet phone", "push alerts"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "spokenreplies",
        key: "audio/muted",
        label: "Spoken replies",
        description: "Read agents' answers aloud",
        aliases: &["voice", "speech", "mute", "unmute", "tts", "read aloud", "sound"],
        section: BEHAVIOUR,
        kind: Kind::Toggle { default: true, inverted: true },
        scope: G,
    },
    Spec {
        name: "confirmstop",
        key: "behaviour/confirmStop",
        label: "Confirm stop and release",
        description: "Ask before Stop agent (Ctrl+.) or Release agent interrupt or let go of an agent",
        aliases: &["confirm dialogs", "are you sure", "confirmation", "safety", "prompt before stop"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "linkopen",
        key: "behaviour/linkOpen",
        label: "Opening links",
        description: "What a click on a link (or a link hint) does",
        aliases: &["links", "browser", "open urls", "copy links", "hyperlinks"],
        section: BEHAVIOUR,
        kind: Kind::Choice { default: "browser", options: LINKS, indexed: false },
        scope: G,
    },
    Spec {
        name: "startupnewagent",
        key: "launch/newAgentOnStartup",
        label: "New agent on startup",
        description: "Start a new agent when Clarp opens, instead of reopening the last chat",
        aliases: &["startup chat", "on launch", "open with new chat", "startup"],
        section: BEHAVIOUR,
        kind: toggle(false),
        scope: G,
    },
    Spec {
        name: "anonymousagents",
        key: "launch/anonymousAgents",
        label: "Anonymous agents",
        description: "Start new agents without a persona by default",
        aliases: &["no persona", "nameless", "plain agents", "agent identity"],
        section: BEHAVIOUR,
        kind: toggle(true),
        scope: G,
    },
    Spec {
        name: "reducedmotion",
        key: "appearance/reducedMotion",
        label: "Reduce motion",
        description: "Stop the shimmer, breathing rings and other animations",
        aliases: &["animations", "motion", "no animation", "accessibility", "still"],
        section: BEHAVIOUR,
        kind: toggle(false),
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
        Kind::Toggle { .. } => value.as_bool().map(Value::Bool).ok_or_else(|| format!("{} must be true or false, not {value}", spec.name)),
        Kind::Number { min, max, unit, zero, .. } => {
            let n = value.as_f64().ok_or_else(|| format!("{} must be a number, not {value}", spec.name))?;
            if zero.is_some() && n == 0.0 {
                return Ok(json!(0.0));
            }
            if !(min..=max).contains(&n) {
                let off = zero.map(|z| format!(" (or 0: {z})")).unwrap_or_default();
                return Err(format!("{} must be between {} and {}{unit}{off}, not {}", spec.name, plain(min), plain(max), plain(n)));
            }
            Ok(json!(n))
        }
        Kind::Choice { .. } => {
            let options = options_of(spec);
            let text = match value {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            };
            options
                .iter()
                .find(|(id, label)| id.eq_ignore_ascii_case(&text) || label.eq_ignore_ascii_case(&text))
                .map(|(id, _)| Value::from(id.clone()))
                .ok_or_else(|| format!("{} must be one of {}, not {value}", spec.name, options.iter().map(|(id, _)| id.as_str()).collect::<Vec<_>>().join(", ")))
        }
        Kind::Color { .. } => value
            .as_str()
            .and_then(normalized_color)
            .map(Value::from)
            .ok_or_else(|| format!("{} must be a colour like #1a2b3c, not {value}", spec.name)),
        Kind::Text { .. } => match value.as_str().map(str::trim) {
            Some(text) if !text.is_empty() => Ok(Value::from(text)),
            _ => Err(format!("{} must be a non-empty text, not {value}", spec.name)),
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
                .ok_or_else(|| format!("{} must be 0 to {}, not {raw}", spec.name, options.len().saturating_sub(1)))
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

/// The current value as people read it.
pub fn shown(settings: &Settings, spec: &Spec) -> String {
    let theme = theme_of(settings);
    display(spec, &current(settings, spec, &theme))
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
            other => Err(format!("{}: say on or off, not {other}", spec.name)),
        },
        Kind::Number { step, .. } => {
            let n = current_value.as_f64().unwrap_or(0.0);
            let trimmed = text.trim_end_matches(|c: char| c.is_alphabetic() || c == '%' || c == '×' || c.is_whitespace());
            let value = match trimmed {
                "+" => n + step,
                "-" => n - step,
                _ => match trimmed.strip_prefix("+=").or_else(|| trimmed.strip_prefix("-=")) {
                    Some(delta) => {
                        let d: f64 = delta.trim().parse().map_err(|_| format!("{}: {text} is not a number", spec.name))?;
                        if trimmed.starts_with('+') { n + d } else { n - d }
                    }
                    None => trimmed.parse().map_err(|_| format!("{}: {text} is not a number", spec.name))?,
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
    let spec = find(name).ok_or_else(|| format!("no setting called {name}"))?;
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
                    out.push(format!("{error}; using {}", display(spec, &default_value(spec, &theme))));
                }
            }
            Scope::Theme { .. } => {}
        }
    }
    for key in [FONT_OVERRIDES_KEY, COLOR_OVERRIDES_KEY] {
        let Some(all) = settings.get(key) else { continue };
        let Some(all) = all.as_object() else {
            out.push(format!("{key} must be an object of themes, not {all}"));
            continue;
        };
        for (theme_id, entry) in all {
            if reading_theme::normalized_theme_id(theme_id) != *theme_id {
                out.push(format!("{key}: there is no reading theme called {theme_id}"));
            }
            let Some(entry) = entry.as_object() else {
                out.push(format!("{key}.{theme_id} must be an object, not {entry}"));
                continue;
            };
            for (field, raw) in entry {
                let spec = SPECS.iter().find(|s| s.key == key && s.scope == Scope::Theme { field: leak_field(field) });
                match spec {
                    Some(spec) => {
                        if let Err(error) = validate(spec, raw) {
                            out.push(format!("{error} (theme {theme_id}); using the theme's"));
                        }
                    }
                    // The font picker keeps the family here too.
                    None if key == FONT_OVERRIDES_KEY && field == "family" => {
                        if !raw.is_string() {
                            out.push(format!("{key}.{theme_id}.family must be a font name, not {raw}"));
                        }
                    }
                    None => out.push(format!("{key}.{theme_id}: unknown entry {field}")),
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
        return (0, vec!["the file is not a JSON object of settings".into()]);
    };
    let mut problems = Vec::new();
    let mut applied = 0;
    for (key, value) in object {
        if key == "clarpSettingsVersion" || key == "$schema" {
            continue;
        }
        if !keys().contains(&key.as_str()) {
            problems.push(format!("{key} is not a setting; skipped"));
            continue;
        }
        if key == FONT_OVERRIDES_KEY || key == COLOR_OVERRIDES_KEY {
            let Some(themes) = value.as_object() else {
                problems.push(format!("{key} must be an object of themes; skipped"));
                continue;
            };
            let mut merged = settings.get(key).and_then(Value::as_object).cloned().unwrap_or_default();
            for (theme_id, entry) in themes {
                if reading_theme::normalized_theme_id(theme_id) != *theme_id {
                    problems.push(format!("{key}: there is no reading theme called {theme_id}; skipped"));
                    continue;
                }
                let Some(entry) = entry.as_object() else {
                    problems.push(format!("{key}.{theme_id} must be an object; skipped"));
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
                        None => Err(format!("{key}.{theme_id}: unknown entry {field}")),
                    };
                    match checked {
                        Ok(valid) => {
                            if let Some(target) = target.as_object_mut() {
                                target.insert(field.clone(), valid);
                                applied += 1;
                            }
                        }
                        Err(error) => problems.push(format!("{error}; skipped")),
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
            Some(Err(error)) => problems.push(format!("{error}; skipped")),
            None => problems.push(format!("{key} is not a setting; skipped")),
        }
    }
    (applied, problems)
}

/// A JSON Schema of the settings file, so an editor that reads
/// `"$schema"` explains, completes and checks every setting.
pub fn schema() -> Value {
    let mut properties = Map::new();
    for spec in SPECS {
        let doc = format!("{} — {} (:set {}; aliases: {})", spec.label, spec.description, spec.name, spec.aliases.join(", "));
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
                json!({"type": "string", "pattern": "^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", "description": format!("{} — {}", s.label, s.description)}),
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
                   "size": {"type": "number", "minimum": 9, "maximum": 40, "description": "Font size — Size of message text in the chat (:set fontsize)"}}}}),
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
            out.push(format!(
                "{} on {} is Lc {:.0} / {:.1}:1; {} text needs Lc {lc} / {ratio}:1",
                m.foreground,
                m.surface,
                m.lc.abs(),
                m.ratio,
                m.tier.name()
            ));
        }
    }
    for (vision, (a, b), de) in signal_separation(&object) {
        if (a == role || b == role) && de < MIN_STATUS_DELTA_E {
            out.push(format!("{a} and {b} are hard to tell apart with {} (ΔE {de:.0}, needs {MIN_STATUS_DELTA_E})", vision.name()));
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
