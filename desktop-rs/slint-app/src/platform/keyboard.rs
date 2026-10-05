//! The modifier keys the window system says are held (winit's
//! `ModifiersChanged`, from the compositor's own keyboard state), set
//! against the ones Slint read on a key: a release the window never saw
//! (an input method or a window switch took it) leaves Slint reading every
//! later key with that modifier, so J arrives as Alt+J and nothing runs.

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

thread_local! {
    /// The window system's last word, while no modifier key went by since
    /// (its report on that key is still to come).
    static REPORTED: Cell<Option<Modifiers>> = const { Cell::new(None) };
}

/// The window system said which modifiers are held.
pub fn reported(modifiers: Modifiers) {
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
