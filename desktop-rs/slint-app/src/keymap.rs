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
        "modal" | "launch" | "blocked" => "root",
        _ => return None,
    })
}

fn state(name: &str) -> Vec<Binding> {
    use Guard::{Agent, Attention, None as Always, Rows, Send};
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
            b("refresh", &["Ctrl+R"], "Refresh", false, Always, false),
            b("mute", &["Ctrl+M"], "Mute", false, Always, false),
            b("overview", &["Ctrl+Shift+O"], "Overview", false, Always, false),
            b("tools", &["Ctrl+Shift+T"], "Tools", false, Always, false),
            b("ui-larger", &["Ctrl+="], "Larger", false, Always, false),
            b("ui-smaller", &["Ctrl+-"], "Smaller", false, Always, false),
            b("ui-reset", &["Ctrl+0"], "Reset scale", false, Always, false),
        ],
        "workspace" => vec![
            b("retry-message", &["Ctrl+Alt+R"], "Retry failed message", false, Agent, false),
            b("jump-latest", &["Ctrl+End"], "Latest", true, Agent, false),
            b("assign-agent", &["Ctrl+A"], "Assign contact", false, Agent, false),
            b("auto-assign-agent", &["Ctrl+Shift+A"], "Auto assign", false, Agent, false),
            b("escape", &["Escape"], "Navigate", true, Always, false),
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
        ],
        "navigation" => vec![
            b("next-attention", &["N", "Ctrl+J"], "Next attention", true, Attention, false),
            b("focus-sidebar", &["E"], "Agents", true, Always, false),
            b("focus-pane", &["C"], "Conversation", true, Always, false),
            b("focus-composer", &["I"], "Type", true, Agent, false),
            b("toggle-focus", &["Tab", "Shift+Tab"], "Switch focus", true, Always, false),
            b("switcher", &["Space", "Ctrl+K"], "Commands", true, Always, false),
            b("escape", &["Escape"], "Conversation", false, Always, false),
        ],
        "pane" => vec![
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
            b("agent-next", &["J", "Down"], "Next", true, Rows, false),
            b("agent-previous", &["K", "Up"], "Previous", true, Rows, false),
            b("agent-open", &["Return", "Enter"], "Open", true, Rows, false),
            b("agent-search", &["/"], "Search", true, Always, false),
        ],
        "composer" => vec![
            b("jump-latest", &["Ctrl+End"], "Latest", true, Agent, true),
            b("send", &["Return"], "Send", true, Send, true),
            b("newline", &["Shift+Return"], "New line", true, Always, true),
            b("queue", &["Ctrl+Return"], "Queue", true, Send, true),
        ],
        "search" => vec![
            b("search-next", &["Down"], "Results", true, Rows, true),
            b("escape", &["Escape"], "Agents", true, Always, false),
        ],
        "settings" => vec![
            b("settings-move", &["Up", "Down"], "Move", true, Always, true),
            b("settings-open", &["Return"], "Change", true, Always, true),
            b("escape", &["Escape"], "Back", true, Always, false),
        ],
        "updates" => vec![b("refresh", &["Ctrl+R"], "Refresh", true, Always, false), b("escape", &["Escape"], "Chats", true, Always, false)],
        "teams" => vec![b("escape", &["Escape"], "Chats", true, Always, false)],
        "launch" => vec![
            b("switcher", &["Ctrl+K"], "Commands", true, Always, false),
            b("change-directory", &["Ctrl+Alt+D"], "Change directory", true, Always, false),
            b("escape", &["Escape"], "Back", true, Always, false),
        ],
        "modal" => vec![b("escape", &["Escape"], "Close", true, Always, false)],
        _ => Vec::new(),
    }
}

pub const STATES: &[&str] = &["root", "main", "workspace", "navigation", "pane", "sidebar", "composer", "search", "settings", "updates", "teams", "launch", "modal", "blocked"];

/// What the guards test.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Facts {
    pub attention: bool,
    pub agent: bool,
    pub rows: bool,
    pub can_send: bool,
}

impl Facts {
    fn allows(self, guard: Guard) -> bool {
        match guard {
            Guard::None => true,
            Guard::Attention => self.attention,
            Guard::Agent => self.agent,
            Guard::Rows => self.rows,
            Guard::Send => self.can_send,
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
        let mut seen: BTreeMap<String, &'static str> = BTreeMap::new();
        for entry in resolve(name, &overrides, None) {
            for key in &entry.keys {
                if let Some(other) = seen.insert(key.clone(), entry.action)
                    && other != entry.action
                {
                    return Err(format!("Conflict in {name}: {key}"));
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
        Facts { attention: true, agent: true, rows: true, can_send: true }
    }

    #[test]
    fn child_states_inherit_and_override_by_action() {
        let none = Overrides::new();
        // Escape in a pane is navigation's "Conversation", not workspace's "Navigate".
        let pane = resolve("pane", &none, Some(all()));
        assert_eq!(pane.iter().find(|b| b.action == "escape").unwrap().label, "Conversation");
        assert_eq!(pane.iter().find(|b| b.action == "split-right").unwrap().keys, ["Alt+V", "Ctrl+Alt+V", "Ctrl+Shift+V"]);
        assert!(pane.iter().any(|b| b.action == "settings"), "main's bindings reach a pane");
        assert_eq!(action_for("pane", "E", &none, all()), Some("focus-sidebar"));
        assert_eq!(action_for("composer", "E", &none, all()), None, "letters type in the composer");
        assert_eq!(action_for("composer", "Return", &none, all()), None, "the composer sends by itself");
        assert_eq!(action_for("composer", "Ctrl+Alt+V", &none, all()), Some("split-right"));
        assert!(resolve("blocked", &none, Some(all())).is_empty());
    }

    #[test]
    fn guards_hide_bindings_that_cannot_run() {
        let none = Overrides::new();
        let nothing = Facts::default();
        assert_eq!(action_for("pane", "N", &none, nothing), None);
        assert_eq!(action_for("pane", "N", &none, Facts { attention: true, ..nothing }), Some("next-attention"));
        assert_eq!(action_for("pane", "Ctrl+End", &none, nothing), None);
        assert!(!hints("composer", &none, nothing).iter().any(|b| b.action == "send"));
        assert!(hints("composer", &none, all()).iter().any(|b| b.action == "send"));
    }

    #[test]
    fn rebinding_is_validated_and_round_trips() {
        let overrides = set_binding(&Overrides::new(), "switcher", "Ctrl+P").unwrap();
        assert_eq!(action_for("pane", "Ctrl+P", &overrides, all()), Some("switcher"));
        assert_eq!(action_for("pane", "Ctrl+K", &overrides, all()), None, "the default key is replaced");
        assert_eq!(import(&export(&overrides)).unwrap(), overrides);
        assert!(set_binding(&overrides, "mute", "Ctrl+Q").is_err(), "not editable");
        assert!(set_binding(&overrides, "zoom", "Ctrl+V").unwrap_err().contains("Reserved"));
        assert!(set_binding(&overrides, "zoom", "Alt+Z").is_err(), "one Ctrl chord");
        assert!(set_binding(&overrides, "zoom", "Ctrl+B").unwrap_err().contains("Conflict"), "Ctrl+B is the sidebar");
        assert!(set_binding(&overrides, "switcher", "").unwrap().is_empty(), "an empty key resets");
        assert!(import(r#"{"version": 2, "bindings": {}}"#).is_err());
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
