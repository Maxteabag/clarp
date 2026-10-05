//! The modifier keys the window system says are held (winit's
//! `ModifiersChanged`, from the compositor's own keyboard state), set
//! against the ones Slint read on a key: a release the window never saw
//! leaves Slint reading every later key with that modifier.

use std::cell::Cell;

/// Which modifiers are down.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Modifiers {
    pub control: bool,
    pub alt: bool,
    pub shift: bool,
    pub meta: bool,
}

thread_local! {
    /// The window system's last word, while no modifier key went by since.
    static REPORTED: Cell<Option<Modifiers>> = const { Cell::new(None) };
}

/// The window system said which modifiers are held.
pub fn reported(modifiers: Modifiers) {
    REPORTED.with(|r| r.set(Some(modifiers)));
}
