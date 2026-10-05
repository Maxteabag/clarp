//! The keyboard map (KeyboardMap.qml): bindings per focus state, where a
//! child state inherits its ancestors' bindings and overrides them by
//! action. The shortcut dispatcher and the shortcut bar's hints read the
//! same resolved list. The user's rebinding of a few actions is kept in
//! settings (`keymap/bindings`) as `{action: "Ctrl+…"}`.

use std::collections::{BTreeMap, HashSet};

use serde_json::{Map, Value, json};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Guard {
    None,
    /// Something needs attention.
    Attention,
    /// An agent's chat (not a pair room) is open.
    Agent,
    /// The list has rows.
    Rows,
    /// The composer can send.
    Send,
    /// The open agent is working (thinking, a tool, compacting).
    Busy,
    /// A voice reply is playing.
    Playing,
    /// The reader has scrolled up from the latest message.
    Behind,
    /// Some chat in the explorer has sub-agents to fold or unfold.
    Folds,
    /// The open chat shows artifact cards.
    Artifacts,
    /// The keyboard is on an artifact card.
    Artifact,
    /// Another window saved a newer layout (the conflict bar shows).
    LayoutWarning,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub action: &'static str,
    pub keys: Vec<String>,
    pub label: &'static str,
    /// Shown in the shortcut bar.
    pub hint: bool,
    pub guard: Guard,
    /// Handled by the focused control itself (the composer's Return); the
    /// dispatcher lets it through.
    pub native: bool,
}

fn binding(action: &'static str, keys: &[&str], label: &'static str, hint: bool, guard: Guard, native: bool) -> Binding {
    Binding { action, keys: keys.iter().map(|k| (*k).to_owned()).collect(), label, hint, guard, native }
}

/// Actions a user may rebind, and only to one Ctrl chord each.
pub const EDITABLE: &[&str] = &["switcher", "sidebar", "split-right", "split-down", "zoom", "balance", "next-workspace"];
const RESERVED: &[&str] = &["Ctrl+A", "Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+Y"];

fn parent(state: &str) -> Option<&'static str> {
    Some(match state {
        "main" => "root",
        "workspace" | "settings" | "updates" | "teams" => "main",
        "navigation" | "composer" | "search" => "workspace",
        "pane" | "sidebar" => "navigation",
        "modal" | "launch" | "blocked" | "hints" => "root",
        _ => return None,
    })
}

fn state(name: &str) -> Vec<Binding> {
    use Guard::{Agent, Artifact, Artifacts, Attention, Behind, Busy, Folds, LayoutWarning, None as Always, Playing, Rows, Send};
    let b = binding;
    match name {
        "main" => vec![
            b("edit-keymap", &["Ctrl+Alt+,"], "Key bindings", false, Always, false),
            b("next-workspace", &["Ctrl+Alt+W"], "Next workspace", false, Always, false),
            b("update-preview", &["Ctrl+Alt+U"], "Update", false, Always, false),
            b("next-attention", &["Ctrl+J"], "Next attention", true, Attention, false),
            b("change-directory", &["Ctrl+Alt+D"], "Change directory", false, Always, false),
            b("switcher", &["Ctrl+K"], "Commands", true, Always, false),
            b("sidebar", &["Ctrl+B"], "Show/hide sidebar", false, Always, false),
            b("shortcut-bar", &["Ctrl+Shift+K"], "Show/hide keybindings", false, Always, false),
            b("quick-new-agent", &["Ctrl+Shift+N"], "New contact & chat", false, Always, false),
            b("rename-agent", &["F2"], "Rename contact", false, Always, false),
            b("new", &["Ctrl+N"], "New agent", false, Always, false),
            b("new-contact", &["Ctrl+Alt+N"], "Start contact", false, Always, false),
            b("settings", &["Ctrl+,", "Ctrl+4"], "Settings", false, Always, false),
            b("chats", &["Ctrl+1"], "Chats", false, Always, false),
            b("updates", &["Ctrl+2"], "Updates", false, Always, false),
            b("teams", &["Ctrl+3"], "Teams", false, Always, false),
            b("recent-agents", &["Ctrl+R"], "Recent", true, Always, false),
            b("refresh", &["F5"], "Refresh", false, Always, false),
            b("mute", &["Ctrl+M"], "Mute", false, Always, false),
            b("overview", &["Ctrl+Shift+O"], "Overview", false, Always, false),
            b("tools", &["Ctrl+Shift+T"], "Tools", false, Always, false),
            b("toggle-explanations", &["Ctrl+Shift+X"], "Tool explanations", false, Always, false),
            b("ui-larger", &["Ctrl+="], "Larger", false, Always, false),
            b("ui-smaller", &["Ctrl+-"], "Smaller", false, Always, false),
            b("ui-reset", &["Ctrl+0"], "Reset scale", false, Always, false),
        ],
        "workspace" => vec![
            // Ctrl+E / Ctrl+H reach the explorer and the chat from anywhere,
            // typing included (no Escape first).
            b("focus-sidebar", &["Ctrl+E"], "Explorer", false, Always, false),
            b("focus-pane", &["Ctrl+H"], "Chat", false, Always, false),
            b("retry-message", &["Ctrl+Alt+R"], "Retry failed message", false, Agent, false),
            b("jump-latest", &["Ctrl+End"], "Latest", true, Behind, false),
            b("auto-assign-agent", &["Ctrl+Shift+A"], "Auto assign", false, Agent, false),
            b("escape", &["Escape"], "Navigate", false, Always, false),
            b("keep-layout", &["Ctrl+Shift+S"], "Keep this layout", true, LayoutWarning, false),
            b("move-left", &["Ctrl+Alt+Left"], "Left pane", false, Always, false),
            b("move-right", &["Ctrl+Alt+Right"], "Right pane", false, Always, false),
            b("move-up", &["Ctrl+Alt+Up"], "Upper pane", false, Always, false),
            b("move-down", &["Ctrl+Alt+Down"], "Lower pane", false, Always, false),
            b("split-right", &["Ctrl+Alt+V"], "Split right", false, Always, false),
            b("split-down", &["Ctrl+Alt+S"], "Split down", false, Always, false),
            b("close-pane", &["Ctrl+Alt+X"], "Close pane", false, Always, false),
            b("zoom", &["Ctrl+Alt+Z"], "Zoom pane", false, Always, false),
            b("balance", &["Ctrl+Alt+="], "Balance panes", false, Always, false),
            b("agent-terminal", &["Ctrl+Alt+T"], "Terminal", false, Agent, false),
            b("release-agent", &["Ctrl+Shift+R"], "Release", false, Agent, false),
            b("stop-agent", &["Ctrl+.", "Ctrl+C"], "Stop", false, Agent, false),
            b("talk", &["Ctrl+Shift+Space"], "Talk", false, Agent, false),
            // Link hints, from the composer too.
            b("link-hints", &["Ctrl+L"], "Open a link", false, Always, false),
        ],
        "navigation" => vec![
            b("next-attention", &["N", "Ctrl+J"], "Next attention", true, Attention, false),
            b("focus-sidebar", &["E", "Ctrl+E"], "Explorer", false, Always, false),
            b("focus-pane", &["H", "Ctrl+H"], "Chat", false, Always, false),
            b("focus-composer", &["I"], "Insert", false, Agent, false),
            b("toggle-focus", &["Tab", "Shift+Tab"], "Switch focus", false, Always, false),
            b("switcher", &["Space", "Ctrl+K"], "Commands", true, Always, false),
            b("link-hints", &["F", "Ctrl+L"], "Open a link", false, Always, false),
            b("escape", &["Escape"], "Chat", false, Always, false),
        ],
        "pane" => vec![
            // The chat's artifact cards: K from the chat is the lowest card on screen.
            b("artifact-previous", &["K"], "Cards", true, Artifacts, false),
            b("artifact-next", &["J"], "Next card", false, Artifacts, false),
            // O, not Enter: Enter belongs to the composer, where a card's
            // keys never reach.
            b("artifact-open", &["O"], "Open", true, Artifact, false),
            b("artifact-choose", &["1", "2", "3", "4", "5", "6", "7", "8", "9"], "Choose", false, Artifact, false),
            b("artifact-discard", &["Delete"], "Discard", false, Artifact, false),
            // A gallery's tiles, an audio clip's position; S stops a clip.
            b("artifact-back", &["Left"], "Back", false, Artifact, false),
            b("artifact-forward", &["Right"], "Forward", false, Artifact, false),
            b("artifact-stop", &["S"], "Stop", false, Artifact, false),
            b("move-left", &["Alt+Left", "Ctrl+Alt+Left"], "Left pane", false, Always, false),
            b("move-right", &["Alt+Right", "Ctrl+Alt+Right"], "Right pane", false, Always, false),
            b("move-up", &["Alt+Up", "Ctrl+Alt+Up"], "Upper pane", false, Always, false),
            b("move-down", &["Alt+Down", "Ctrl+Alt+Down"], "Lower pane", false, Always, false),
            b("split-right", &["Alt+V", "Ctrl+Alt+V", "Ctrl+Shift+V"], "Split right", false, Always, false),
            b("split-down", &["Alt+S", "Ctrl+Alt+S", "Ctrl+Shift+H"], "Split down", false, Always, false),
            b("close-pane", &["Alt+X", "Ctrl+Alt+X", "Ctrl+Shift+W"], "Close pane", false, Always, false),
            b("zoom", &["Alt+Z", "Ctrl+Alt+Z", "Ctrl+Shift+Z"], "Zoom pane", false, Always, false),
            b("balance", &["Alt+=", "Ctrl+Alt+=", "Ctrl+Shift+="], "Balance panes", false, Always, false),
        ],
        "sidebar" => vec![
            b("agent-next", &["J", "Down"], "Next", false, Rows, false),
            b("agent-previous", &["K", "Up"], "Previous", false, Rows, false),
            b("agent-open", &["Return", "Enter"], "Open", false, Rows, false),
            b("agent-search", &["/"], "Search", true, Always, false),
            b("toggle-preview", &["P"], "Preview", true, Always, false),
            b("toggle-compact", &["V"], "Compact view", true, Always, false),
            b("fold", &["Left"], "Fold", true, Folds, false),
            b("unfold", &["L", "Right"], "Unfold", true, Folds, false),
            // The explorer's header buttons, shown while it has the keyboard.
            b("new", &["Ctrl+N"], "New agent", true, Always, false),
            b("sidebar", &["Ctrl+B"], "Hide sidebar", true, Always, false),
        ],
        "composer" => vec![
            b("jump-latest", &["Ctrl+End"], "Latest", true, Behind, true),
            b("send", &["Return"], "Send", false, Send, true),
            b("newline", &["Shift+Return"], "New line", false, Always, true),
            b("queue", &["Ctrl+Return"], "Queue", true, Busy, true),
            b("focus-pane", &["Ctrl+H"], "Chat", false, Always, false),
            b("focus-sidebar", &["Ctrl+E"], "Explorer", false, Always, false),
            // The composer has no buttons: its actions are keys, shown here.
            b("attach", &["Ctrl+Shift+O"], "Attach", true, Agent, true),
            b("stop-agent", &["Ctrl+."], "Stop", true, Busy, false),
            b("silence", &["Ctrl+Shift+M"], "Stop voice", true, Playing, false),
        ],
        "search" => vec![
            b("search-next", &["Down"], "Results", false, Rows, true),
            b("escape", &["Escape"], "Explorer", false, Always, false),
        ],
        "settings" => vec![
            b("settings-move", &["Up", "Down"], "Move", true, Always, true),
            b("settings-open", &["Return"], "Change", true, Always, true),
            b("escape", &["Escape"], "Back", true, Always, false),
        ],
        "updates" => vec![b("refresh", &["F5"], "Refresh", true, Always, false), b("escape", &["Escape"], "Chats", true, Always, false)],
        "teams" => vec![b("escape", &["Escape"], "Chats", true, Always, false)],
        "launch" => vec![
            b("switcher", &["Ctrl+K"], "Commands", true, Always, false),
            // The hub's own keys (it handles them; listed here for the bar).
            b("launch-provider", &["Ctrl+Left"], "Provider", true, Always, true),
            b("launch-model", &["Ctrl+M"], "Model", true, Always, true),
            b("launch-effort", &["Ctrl+E"], "Effort", true, Always, true),
            b("launch-directory", &["Ctrl+D"], "Directory", true, Always, true),
            b("launch-new-contact", &["Ctrl+N"], "New contact", true, Always, true),
            b("launch-show-all", &["Ctrl+T"], "Show all", true, Always, true),
            b("change-directory", &["Ctrl+Alt+D"], "Change directory", false, Always, false),
            b("escape", &["Escape"], "Back", true, Always, false),
        ],
        "modal" => vec![b("escape", &["Escape"], "Close", true, Always, false)],
        // Link hints show: digits pick a link (the bar says which), Enter
        // opens a number that also starts a longer one; any other key
        // cancels.
        "hints" => vec![
            b("hint-digit", &["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], "Open", true, Always, false),
            b("hint-open", &["Return"], "Open", false, Always, false),
            b("hint-back", &["Backspace"], "Undo digit", false, Always, false),
            b("hint-cancel", &["Escape"], "Cancel", true, Always, false),
        ],
        _ => Vec::new(),
    }
}

pub const STATES: &[&str] = &["root", "main", "workspace", "navigation", "pane", "sidebar", "composer", "search", "settings", "updates", "teams", "launch", "modal", "blocked", "hints"];

/// What the guards test.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Facts {
    pub attention: bool,
    pub agent: bool,
    pub rows: bool,
    pub can_send: bool,
    pub busy: bool,
    pub playing: bool,
    pub behind: bool,
    pub folds: bool,
    pub artifacts: bool,
    pub artifact: bool,
    pub layout_warning: bool,
}

impl Facts {
    fn allows(self, guard: Guard) -> bool {
        match guard {
            Guard::None => true,
            Guard::Attention => self.attention,
            Guard::Agent => self.agent,
            Guard::Rows => self.rows,
            Guard::Send => self.can_send,
            Guard::Busy => self.busy,
            Guard::Playing => self.playing,
            Guard::Behind => self.behind,
            Guard::Folds => self.folds,
            Guard::Artifacts => self.artifacts,
            Guard::Artifact => self.artifact,
            Guard::LayoutWarning => self.layout_warning,
        }
    }
}

pub type Overrides = BTreeMap<String, String>;

/// Bindings in effect in `state`, nearest state first; `facts` None ignores
/// the guards (for conflict checks).
pub fn resolve(state_name: &str, overrides: &Overrides, facts: Option<Facts>) -> Vec<Binding> {
    let (mut result, mut seen) = (Vec::new(), HashSet::new());
    let mut current = Some(state_name);
    while let Some(name) = current {
        for mut entry in state(name) {
            if !seen.insert(entry.action) {
                continue;
            }
            if facts.is_none_or(|facts| facts.allows(entry.guard)) {
                if let Some(key) = overrides.get(entry.action) {
                    entry.keys = vec![key.clone()];
                }
                result.push(entry);
            }
        }
        current = parent(name);
    }
    result
}

/// The action `chord` runs in `state`, if any. `native` bindings are left to
/// the focused control.
pub fn action_for(state_name: &str, chord: &str, overrides: &Overrides, facts: Facts) -> Option<&'static str> {
    resolve(state_name, overrides, Some(facts))
        .into_iter()
        .find(|entry| entry.keys.iter().any(|key| key == chord))
        .and_then(|entry| (!entry.native).then_some(entry.action))
}

/// The shortcut bar's hints for `state`.
pub fn hints(state_name: &str, overrides: &Overrides, facts: Facts) -> Vec<Binding> {
    resolve(state_name, overrides, Some(facts)).into_iter().filter(|entry| entry.hint).collect()
}

fn is_ctrl_chord(key: &str) -> bool {
    let Some(rest) = key.strip_prefix("Ctrl+") else { return false };
    let rest = rest.strip_prefix("Alt+").or_else(|| rest.strip_prefix("Shift+")).unwrap_or(rest);
    rest.chars().count() == 1 && rest.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == ',')
}

/// Validates an exported keymap (`{"version": 1, "bindings": {...}}`) and
/// returns its bindings: only editable actions, one Ctrl chord each, no
/// reserved editing key, and no two actions on one key in any state.
pub fn import(text: &str) -> Result<Overrides, String> {
    let value: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let object = value.as_object().ok_or("Unsupported keymap")?;
    let supported = object.keys().all(|k| k == "version" || k == "bindings");
    let bindings = object.get("bindings").and_then(Value::as_object);
    let (true, Some(1), Some(bindings)) = (supported, object.get("version").and_then(Value::as_i64), bindings) else {
        return Err("Unsupported keymap".into());
    };
    let mut overrides = Overrides::new();
    for (action, key) in bindings {
        let key = key.as_str().filter(|k| EDITABLE.contains(&action.as_str()) && is_ctrl_chord(k));
        let Some(key) = key else { return Err("Use a supported action and one Ctrl chord".into()) };
        if RESERVED.contains(&key) {
            return Err("Reserved text editing key".into());
        }
        overrides.insert(action.clone(), key.to_owned());
    }
    for name in STATES {
        // A nearer native binding (the focused control's own key, such as
        // the composer's Ctrl+Shift+O) shadows an outer one on purpose.
        let mut seen: BTreeMap<String, (&'static str, bool)> = BTreeMap::new();
        for entry in resolve(name, &overrides, None) {
            for key in &entry.keys {
                match seen.get(key) {
                    Some((_, true)) => continue,
                    Some((other, false)) if *other != entry.action => return Err(format!("Conflict in {name}: {key}")),
                    _ => {
                        seen.insert(key.clone(), (entry.action, entry.native));
                    }
                }
            }
        }
    }
    Ok(overrides)
}

pub fn export(overrides: &Overrides) -> String {
    let bindings: Map<String, Value> = overrides.iter().map(|(k, v)| (k.clone(), json!(v))).collect();
    serde_json::to_string_pretty(&json!({"version": 1, "bindings": bindings})).unwrap_or_default()
}

/// Rebinds (or, with an empty key, resets) one action, validated as a whole.
pub fn set_binding(overrides: &Overrides, action: &str, key: &str) -> Result<Overrides, String> {
    let mut next = overrides.clone();
    if key.trim().is_empty() {
        next.remove(action);
    } else {
        next.insert(action.to_owned(), key.trim().to_owned());
    }
    import(&export(&next))
}

/// A key event as the map spells it ("Ctrl+Alt+V", "Shift+Tab", "Escape",
/// "E"), or None for a bare modifier.
pub fn chord(text: &str, control: bool, alt: bool, shift: bool) -> Option<String> {
    use slint::platform::Key;
    let named = [
        (Key::Escape, "Escape"),
        (Key::Tab, "Tab"),
        (Key::Backtab, "Tab"),
        (Key::Return, "Return"),
        (Key::LeftArrow, "Left"),
        (Key::RightArrow, "Right"),
        (Key::UpArrow, "Up"),
        (Key::DownArrow, "Down"),
        (Key::Home, "Home"),
        (Key::End, "End"),
        (Key::PageUp, "PageUp"),
        (Key::PageDown, "PageDown"),
        (Key::Delete, "Delete"),
        (Key::Backspace, "Backspace"),
        (Key::F2, "F2"),
        (Key::F5, "F5"),
        (Key::Space, "Space"),
    ];
    let modifiers = [Key::Control, Key::Alt, Key::AltGr, Key::Shift, Key::ShiftR, Key::Meta, Key::MetaR, Key::ControlR];
    if modifiers.iter().any(|m| slint::SharedString::from(*m) == text) {
        return None;
    }
    let backtab = slint::SharedString::from(Key::Backtab) == text;
    let key = match named.iter().find(|(k, _)| slint::SharedString::from(*k) == text) {
        Some((_, name)) => (*name).to_owned(),
        None if text == " " => "Space".to_owned(),
        None => {
            let mut chars = text.chars();
            let (Some(c), None) = (chars.next(), chars.next()) else { return None };
            if c.is_control() {
                return None;
            }
            c.to_uppercase().to_string()
        }
    };
    // A letter's case already says Shift; symbols keep what the layout made.
    let letter = key.len() == 1 && key.chars().all(|c| c.is_ascii_alphabetic());
    let shift = (shift || backtab) && (letter || key.len() > 1);
    let mut out = String::new();
    for (held, name) in [(control, "Ctrl+"), (alt, "Alt+"), (shift, "Shift+")] {
        if held {
            out += name;
        }
    }
    Some(out + &key)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all() -> Facts {
        Facts { attention: true, agent: true, rows: true, can_send: true, busy: true, playing: true, behind: true, folds: true, artifacts: true, artifact: true, layout_warning: true }
    }

    #[test]
    fn child_states_inherit_and_override_by_action() {
        let none = Overrides::new();
        // Escape in a pane is navigation's "Chat", not workspace's "Navigate".
        let pane = resolve("pane", &none, Some(all()));
        assert_eq!(pane.iter().find(|b| b.action == "escape").unwrap().label, "Chat");
        assert_eq!(pane.iter().find(|b| b.action == "split-right").unwrap().keys, ["Alt+V", "Ctrl+Alt+V", "Ctrl+Shift+V"]);
        assert!(pane.iter().any(|b| b.action == "settings"), "main's bindings reach a pane");
        assert_eq!(action_for("pane", "E", &none, all()), Some("focus-sidebar"));
        assert_eq!(action_for("sidebar", "Left", &none, all()), Some("fold"));
        assert_eq!(action_for("sidebar", "H", &none, all()), Some("focus-pane"), "H is the chat everywhere");
        assert_eq!(action_for("sidebar", "P", &none, all()), Some("toggle-preview"));
        assert_eq!(action_for("composer", "Ctrl+E", &none, all()), Some("focus-sidebar"));
        assert_eq!(action_for("composer", "Ctrl+H", &none, all()), Some("focus-pane"));
        assert_eq!(action_for("pane", "Ctrl+E", &none, all()), Some("focus-sidebar"));
        assert_eq!(action_for("sidebar", "Right", &none, all()), Some("unfold"));
        assert_eq!(action_for("composer", "E", &none, all()), None, "letters type in the composer");
        assert_eq!(action_for("composer", "Return", &none, all()), None, "the composer sends by itself");
        assert_eq!(action_for("composer", "Ctrl+Alt+V", &none, all()), Some("split-right"));
        assert!(resolve("blocked", &none, Some(all())).is_empty());
        assert_eq!(action_for("pane", "F", &none, all()), Some("link-hints"));
        assert_eq!(action_for("composer", "Ctrl+L", &none, all()), Some("link-hints"), "the chord works while typing");
        assert_eq!(action_for("composer", "F", &none, all()), None, "F types in the composer");
        assert_eq!(action_for("hints", "7", &none, all()), Some("hint-digit"));
        assert_eq!(action_for("hints", "Escape", &none, all()), Some("hint-cancel"));
        assert_eq!(action_for("hints", "Ctrl+K", &none, all()), None, "hints own the keyboard");
    }

    #[test]
    fn guards_hide_bindings_that_cannot_run() {
        let none = Overrides::new();
        let nothing = Facts::default();
        assert_eq!(action_for("pane", "N", &none, nothing), None);
        assert_eq!(action_for("pane", "N", &none, Facts { attention: true, ..nothing }), Some("next-attention"));
        assert_eq!(action_for("pane", "Ctrl+End", &none, nothing), None);
        assert!(!hints("composer", &none, nothing).iter().any(|b| b.action == "queue"));
        assert!(hints("composer", &none, all()).iter().any(|b| b.action == "queue"));
    }

    fn peter() -> Overrides {
        // Ctrl+J is next attention's: taking it over leaves N for that.
        let clash = add(&Overrides::new(), "next-agent", "main", "Ctrl+J").unwrap_err();
        let Refusal::Clash(clashes) = clash else { panic!("Ctrl+J is next attention's: {clash:?}") };
        assert!(clashes.iter().any(|c| c.actions.contains(&"next-attention") && c.state == "main"), "{clashes:?}");
        let taken = take_over(&Overrides::new(), "next-agent", "main", "Ctrl+J").unwrap();
        add(&taken, "next-agent", "main", "Right Right").unwrap()
    }

    #[test]
    fn keys_parse_and_spell_doubles() {
        assert_eq!(parse_key("ctrl+j").as_deref(), Ok("Ctrl+J"));
        assert_eq!(parse_key("Shift+Ctrl+alt+v").as_deref(), Ok("Ctrl+Alt+Shift+V"), "modifiers in the map's order");
        assert_eq!(parse_key("Right Right").as_deref(), Ok("Right Right"));
        assert_eq!(parse_key(" right   right ").as_deref(), Ok("Right Right"));
        assert_eq!(parse_key("Enter").as_deref(), Ok("Return"));
        assert_eq!(parse_key("Ctrl+=").as_deref(), Ok("Ctrl+="));
        assert_eq!(parse_key("F7").as_deref(), Ok("F7"));
        assert!(parse_key("Right Left").is_err(), "a double press is one key twice");
        assert!(parse_key("J J J").is_err());
        assert!(parse_key("Ctrl+").is_err());
        assert!(parse_key("Ctrl+Banana").is_err());
        assert!(parse_key("").is_err());
        assert_eq!(display("Right Right"), "Right ×2");
        assert_eq!(display("Ctrl+Return"), "Ctrl+Enter");
        assert_eq!(display("Escape"), "Esc");
    }

    #[test]
    fn several_bindings_per_action_save_and_load() {
        let overrides = peter();
        assert_eq!(keys_in("pane", "next-agent", &overrides), ["Ctrl+J", "Right Right"]);
        assert_eq!(from_settings(&to_settings(&overrides)), overrides, "kept in settings as is");
        assert_eq!(import(&export(&overrides)).unwrap(), overrides, "and in an exported profile");
        assert!(export(&overrides).contains("\"version\": 2"));
        // A one-chord profile from before still loads: its key replaces the default.
        let old = import(r#"{"version": 1, "bindings": {"switcher": "Ctrl+P"}}"#).unwrap();
        assert_eq!(action_for("pane", "Ctrl+P", &old, all()), Some("switcher"));
        assert_eq!(action_for("pane", "Ctrl+K", &old, all()), None, "the default key is replaced");
        assert_eq!(action_for("launch", "Ctrl+P", &old, all()), Some("switcher"));
        assert_eq!(from_settings(&json!({"switcher": "Ctrl+P"})), old, "the old setting too");
        assert!(import(r#"{"version": 3, "bindings": {}}"#).is_err());
        assert!(import(r#"{"version": 2, "bindings": {"no-such-action": {"main": {"add": ["Ctrl+Q"]}}}}"#).is_err());
        assert!(import(r#"{"version": 2, "bindings": {"mute": {"nowhere": {"add": ["Ctrl+Q"]}}}}"#).is_err());
        assert!(import(r#"{"version": 2, "bindings": {"mute": {"main": {"add": ["Right Left"]}}}}"#).is_err());
    }

    #[test]
    fn user_bindings_reach_child_contexts_and_add_to_defaults() {
        let overrides = peter();
        for state in ["main", "workspace", "navigation", "pane", "sidebar", "settings", "updates", "composer"] {
            assert_eq!(action_for(state, "Ctrl+J", &overrides, all()), Some("next-agent"), "Ctrl+J in {state}");
        }
        assert_eq!(action_for("pane", "N", &overrides, all()), Some("next-attention"), "N is still next attention");
        assert_eq!(action_for("launch", "Ctrl+J", &overrides, all()), None, "dialogs are not everywhere");
        // A binding in the explorer is the explorer's alone.
        let explorer = add(&Overrides::new(), "mute", "sidebar", "Shift+M").unwrap();
        assert_eq!(action_for("sidebar", "Shift+M", &explorer, all()), Some("mute"));
        assert_eq!(action_for("pane", "Shift+M", &explorer, all()), None);
        // Any action, not only a few; removing a default and resetting it.
        let gone = remove(&Overrides::new(), "next-attention", "navigation", "N");
        assert_eq!(action_for("sidebar", "N", &gone, all()), None);
        assert_eq!(action_for("sidebar", "Ctrl+J", &gone, all()), Some("next-attention"));
        assert!(reset(&gone, "next-attention", "navigation").is_empty());
        let both = remove(&peter(), "next-agent", "main", "Ctrl+J");
        assert_eq!(keys_in("pane", "next-agent", &both), ["Right Right"]);
        // The user's own keys come first, for the bar.
        let extra = add(&Overrides::new(), "switcher", "main", "Ctrl+P").unwrap();
        assert_eq!(keys_in("pane", "switcher", &extra), ["Ctrl+P", "Space", "Ctrl+K"]);
        assert_eq!(hints("pane", &extra, all()).iter().find(|b| b.action == "switcher").unwrap().keys[0], "Ctrl+P");
    }

    #[test]
    fn typing_keys_from_outside_never_reach_the_composer() {
        let overrides = peter();
        assert!(!keys_in("composer", "next-agent", &overrides).contains(&"Right Right".to_owned()));
        assert_eq!(double_action("composer", "Right", &overrides, all()), None, "Right Right moves the cursor twice");
        let letters = add(&Overrides::new(), "mute", "main", "M").unwrap();
        assert_eq!(action_for("pane", "M", &letters, all()), Some("mute"));
        assert_eq!(action_for("composer", "M", &letters, all()), None, "M types");
        assert_eq!(action_for("search", "M", &letters, all()), None);
        // Bound there on purpose, it works there.
        let there = add(&overrides, "next-agent", "composer", "Right Right").unwrap();
        assert_eq!(double_action("composer", "Right", &there, all()), Some("next-agent"));
    }

    #[test]
    fn clashes_are_found_refused_and_taken_over() {
        let clash = add(&Overrides::new(), "next-agent", "main", "J").unwrap_err();
        let Refusal::Clash(clashes) = clash else { panic!("{clash:?}") };
        let states: Vec<&str> = clashes.iter().map(|c| c.state).collect();
        assert!(states.contains(&"sidebar") && states.contains(&"pane"), "J is the explorer's next and the chat's next card: {states:?}");
        let taken = take_over(&Overrides::new(), "next-agent", "main", "J").unwrap();
        assert!(clashes(&taken).is_empty());
        assert_eq!(action_for("sidebar", "J", &taken, all()), Some("next-agent"));
        assert_eq!(action_for("sidebar", "Down", &taken, all()), Some("agent-next"), "the other keys stay");
        assert!(matches!(add(&Overrides::new(), "zoom", "main", "Ctrl+V"), Err(Refusal::Reserved(_))));
        assert!(matches!(add(&Overrides::new(), "zoom", "main", "Ctrl+C Ctrl+C"), Err(Refusal::Reserved(_))));
        assert!(take_over(&Overrides::new(), "zoom", "main", "Ctrl+Z").is_err(), "reserved cannot be taken over");
        assert!(matches!(add(&Overrides::new(), "zoom", "main", "Banana"), Err(Refusal::Invalid(_))));
        // A double press is not its single press.
        assert!(add(&Overrides::new(), "next-agent", "sidebar", "Right Right").is_ok());
        assert!(clashes(&Overrides::new()).is_empty(), "the defaults agree");
        let clash = add(&peter(), "mute", "pane", "Right Right").unwrap_err();
        assert!(matches!(clash, Refusal::Clash(_)), "inherited from everywhere: {clash:?}");
        assert!(import(&export(&add_unchecked(&peter(), "mute", "pane", "Right Right"))).unwrap_err().contains("Right ×2"), "a clashing profile is refused");
    }

    #[test]
    fn a_double_press_is_its_second_press_within_the_window() {
        let window = std::time::Duration::from_millis(300);
        let t0 = std::time::Instant::now();
        let ms = |n| t0 + std::time::Duration::from_millis(n);
        let mut presses = Presses::default();
        assert!(!presses.press("Right", t0, window, false));
        assert!(presses.press("Right", ms(200), window, false), "the second in time");
        assert!(!presses.press("Right", ms(300), window, false), "a third starts again");
        assert!(!presses.press("Right", ms(700), window, false), "too late");
        assert!(!presses.press("Left", ms(750), window, false));
        assert!(!presses.press("Right", ms(800), window, false), "another key between");
        assert!(!presses.press("Right", ms(820), window, true), "a held key repeating is not a double press");
        assert!(!presses.press("Right", ms(900), window, false));
        assert!(presses.press("Right", ms(1199), window, false));
    }

    #[test]
    fn a_single_press_keeps_its_meaning() {
        let overrides = peter();
        let nothing = Facts { rows: true, ..Facts::default() };
        // Nothing is on Right in the chat: the second Right is the next agent.
        assert_eq!(action_for("pane", "Right", &overrides, nothing), None);
        assert_eq!(double_action("pane", "Right", &overrides, nothing), Some("next-agent"));
        // On a gallery or a clip Right steps or seeks, each press, at once.
        let card = Facts { artifact: true, artifacts: true, ..nothing };
        assert_eq!(action_for("pane", "Right", &overrides, card), Some("artifact-forward"));
        assert_eq!(double_action("pane", "Right", &overrides, card), None, "seeking twice is not a jump");
        // The explorer's Right unfolds; with nothing to unfold it is free.
        assert_eq!(double_action("sidebar", "Right", &overrides, Facts { folds: true, ..nothing }), None);
        assert_eq!(double_action("sidebar", "Right", &overrides, nothing), Some("next-agent"));
        // Bound in the explorer itself, the first Right unfolds and the second jumps.
        let there = add(&overrides, "next-agent", "sidebar", "Right Right").unwrap();
        assert_eq!(action_for("sidebar", "Right", &there, Facts { folds: true, ..nothing }), Some("unfold"));
        assert_eq!(double_action("sidebar", "Right", &there, Facts { folds: true, ..nothing }), Some("next-agent"));
        assert_eq!(double_action("pane", "Left", &overrides, nothing), None);
    }

    #[test]
    fn every_action_can_be_bound_but_not_a_control_s_own_keys() {
        let actions: Vec<&str> = actions().iter().map(|(a, _)| *a).collect();
        for action in ["next-agent", "previous-agent", "mute", "next-attention", "agent-next", "orchestrator", "escape"] {
            assert!(actions.contains(&action), "{action}");
        }
        for action in ["send", "newline", "hint-digit", "artifact-choose", "launch-model"] {
            assert!(!actions.contains(&action), "{action} is the control's");
        }
        assert!(matches!(add(&Overrides::new(), "send", "composer", "Ctrl+Q"), Err(Refusal::Invalid(_))));
    }

    #[test]
    fn key_events_are_spelled_like_the_map() {
        use slint::platform::Key;
        let key = |k: Key| slint::SharedString::from(k).to_string();
        assert_eq!(chord("v", true, true, false).as_deref(), Some("Ctrl+Alt+V"));
        assert_eq!(chord("e", false, false, false).as_deref(), Some("E"));
        assert_eq!(chord(&key(Key::Escape), false, false, false).as_deref(), Some("Escape"));
        assert_eq!(chord(&key(Key::Backtab), false, false, true).as_deref(), Some("Shift+Tab"));
        assert_eq!(chord(&key(Key::Return), true, false, false).as_deref(), Some("Ctrl+Return"));
        assert_eq!(chord(" ", false, false, false).as_deref(), Some("Space"));
        assert_eq!(chord("=", true, false, true).as_deref(), Some("Ctrl+="), "a symbol keeps its own shift");
        assert_eq!(chord(&key(Key::Control), true, false, false), None);
    }
}
