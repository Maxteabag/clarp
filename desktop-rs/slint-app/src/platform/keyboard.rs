//! The modifier keys the window system says are held (winit's
//! `ModifiersChanged`, from the compositor's own keyboard state), set
//! against the ones Slint read on a key: a release the window never saw
//! (an input method or a window switch took it) leaves Slint reading every
//! later key with that modifier, so J arrives as Alt+J and nothing runs.
//!
//! On macOS Slint reads Cmd as `control` and the Control key as `meta`
//! (as Qt does): the window system's report is read the same way, and the
//! Control key also counts as Ctrl for shortcuts, so Cmd+K and Ctrl+K both
//! run Ctrl+K. `CLARP_KEY_TRACE=1` traces every key to stderr.

use std::cell::Cell;

use slint::platform::{Key, WindowEvent};

/// Which modifiers are down.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Modifiers {
    pub control: bool,
    pub alt: bool,
    pub shift: bool,
    pub meta: bool,
}

impl Modifiers {
    /// The window system's modifiers (winit's Control and Super keys) as
    /// Slint reads them on this platform: swapped on macOS.
    pub fn from_window_system(control_key: bool, alt: bool, shift: bool, super_key: bool) -> Self {
        if cfg!(target_os = "macos") {
            Self { control: super_key, alt, shift, meta: control_key }
        } else {
            Self { control: control_key, alt, shift, meta: super_key }
        }
    }

    /// Whether a shortcut reads Ctrl: on macOS Cmd (`control`) or the
    /// Control key (`meta`); elsewhere Ctrl alone (Super is not a shortcut
    /// modifier there).
    pub fn shortcut_control(self) -> bool {
        self.control || (cfg!(target_os = "macos") && self.meta)
    }

    fn any(self) -> bool {
        self.control || self.alt || self.shift || self.meta
    }

    /// The keys that put these down, left and right.
    fn keys(self) -> Vec<Key> {
        let mut keys = Vec::new();
        if self.control {
            keys.extend([Key::Control, Key::ControlR]);
        }
        if self.alt {
            keys.push(Key::Alt);
        }
        if self.shift {
            keys.extend([Key::Shift, Key::ShiftR]);
        }
        if self.meta {
            keys.extend([Key::Meta, Key::MetaR]);
        }
        keys
    }
}

/// The modifiers `read` on a key that `reported` says are up.
pub fn stale_against(read: Modifiers, reported: Modifiers) -> Modifiers {
    Modifiers {
        control: read.control && !reported.control,
        alt: read.alt && !reported.alt,
        shift: read.shift && !reported.shift,
        meta: read.meta && !reported.meta,
    }
}

/// True for a modifier key itself.
pub fn is_modifier(text: &str) -> bool {
    [Key::Control, Key::ControlR, Key::Alt, Key::AltGr, Key::Shift, Key::ShiftR, Key::Meta, Key::MetaR]
        .iter()
        .any(|m| slint::SharedString::from(*m) == text)
}

/// `CLARP_KEY_TRACE=1`: every key, modifier report and decision to stderr.
pub fn tracing() -> bool {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *ON.get_or_init(|| std::env::var("CLARP_KEY_TRACE").is_ok_and(|v| v == "1"))
}

/// A key's text as code points, for the trace ("U+006B 'k'").
pub fn code_points(text: &str) -> String {
    text.chars()
        .map(|c| if c.is_control() || (c as u32) >= 0xF700 { format!("U+{:04X}", c as u32) } else { format!("U+{:04X} '{c}'", c as u32) })
        .collect::<Vec<_>>()
        .join(" ")
}

thread_local! {
    /// The window system's last word, while no modifier key went by since
    /// (its report on that key is still to come).
    static REPORTED: Cell<Option<Modifiers>> = const { Cell::new(None) };
}

/// The window system said which modifiers are held.
pub fn reported(modifiers: Modifiers) {
    if tracing() {
        eprintln!("key-trace: window system modifiers {modifiers:?}");
    }
    REPORTED.with(|r| r.set(Some(modifiers)));
}

/// A modifier key went down or up; the window system reports on it next.
pub fn modifier_key_seen() {
    REPORTED.with(|r| r.set(None));
}

/// A key Slint read with `read` held: when the window system says some of
/// them are up, they are released and the key is sent again without them
/// (true: the key is taken). Modifier keys themselves pass.
pub fn correct(text: &str, read: Modifiers) -> bool {
    if is_modifier(text) {
        modifier_key_seen();
        return false;
    }
    let Some(reported) = REPORTED.with(Cell::get) else { return false };
    let stale = stale_against(read, reported);
    if !stale.any() {
        return false;
    }
    eprintln!("clarp-slint: keyboard: {stale:?} still down in the window but up in the window system; releasing");
    let text = slint::SharedString::from(text);
    // After this event: Slint is still delivering it.
    let replay = move || {
        use slint::ComponentHandle;
        let Some(window) = crate::window() else { return };
        for key in stale.keys() {
            window.window().dispatch_event(WindowEvent::KeyReleased { text: key.into() });
        }
        window.window().dispatch_event(WindowEvent::KeyPressed { text });
    };
    if let Err(error) = slint::invoke_from_event_loop(replay) {
        eprintln!("clarp-slint: keyboard: cannot send the key again: {error}");
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn held(control: bool, alt: bool, shift: bool, meta: bool) -> Modifiers {
        Modifiers { control, alt, shift, meta }
    }

    #[test]
    fn a_modifier_the_window_system_calls_up_is_stale() {
        let none = Modifiers::default();
        assert_eq!(stale_against(held(true, false, false, false), none), held(true, false, false, false));
        assert_eq!(stale_against(held(true, true, true, true), none), held(true, true, true, true));
        assert_eq!(stale_against(held(true, true, false, false), held(true, false, false, false)), held(false, true, false, false), "Ctrl really is held");
        assert!(!stale_against(none, held(true, true, true, true)).any(), "held in the window system only is not Slint's to release");
        assert!(!stale_against(held(false, false, true, false), held(false, false, true, false)).any());
    }

    #[test]
    fn stale_modifiers_release_both_sides() {
        assert_eq!(held(true, false, false, false).keys(), [Key::Control, Key::ControlR]);
        assert_eq!(held(false, true, false, true).keys(), [Key::Alt, Key::Meta, Key::MetaR]);
        assert!(Modifiers::default().keys().is_empty());
    }

    #[test]
    fn cmd_and_the_control_key_read_as_slint_reads_them() {
        // winit's Control and Super keys: Cmd is Super on a Mac.
        let cmd = Modifiers::from_window_system(false, false, false, true);
        let control_key = Modifiers::from_window_system(true, false, false, false);
        if cfg!(target_os = "macos") {
            assert_eq!(cmd, held(true, false, false, false), "Cmd is Slint's control");
            assert_eq!(control_key, held(false, false, false, true), "the Control key is Slint's meta");
            assert!(cmd.shortcut_control() && control_key.shortcut_control(), "both run Ctrl shortcuts");
        } else {
            assert_eq!(control_key, held(true, false, false, false));
            assert_eq!(cmd, held(false, false, false, true));
            assert!(control_key.shortcut_control() && !cmd.shortcut_control(), "Super is no Ctrl here");
        }
    }

    #[test]
    fn cmd_k_is_not_mistaken_for_a_stale_modifier() {
        // Cmd+K on a Mac: Slint reads control; the window system's report,
        // read the same way, agrees, so the key is not sent again bare.
        let slint_read = if cfg!(target_os = "macos") { held(true, false, false, false) } else { held(false, false, false, true) };
        reported(Modifiers::from_window_system(false, false, false, true));
        assert!(!correct("k", slint_read), "Cmd+K stays Cmd+K");
        // And the real Control key.
        let slint_read = if cfg!(target_os = "macos") { held(false, false, false, true) } else { held(true, false, false, false) };
        reported(Modifiers::from_window_system(true, false, false, false));
        assert!(!correct("k", slint_read), "Ctrl+K stays Ctrl+K");
    }

    #[test]
    fn code_points_name_each_character() {
        assert_eq!(code_points("k"), "U+006B 'k'");
        assert_eq!(code_points("\u{1b}"), "U+001B");
        assert_eq!(code_points("\u{F700}"), "U+F700");
    }

    #[test]
    fn a_modifier_key_waits_for_the_window_systems_report() {
        reported(Modifiers::default());
        // Ctrl goes down: until the window system reports it, nothing is stale.
        assert!(!correct(&slint::SharedString::from(Key::Control), held(true, false, false, false)));
        assert_eq!(REPORTED.with(Cell::get), None);
        assert!(!correct("k", held(true, false, false, false)), "Ctrl+K before the report is Ctrl+K");
        reported(held(true, false, false, false));
        assert!(!correct("k", held(true, false, false, false)), "and after a report that Ctrl is held");
    }
}
