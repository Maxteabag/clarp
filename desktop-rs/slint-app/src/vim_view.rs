//! Vim mode in the window: Normal mode's keys, the command line and the
//! chat search, the cursor row, and what the shortcut bar and the
//! which-key panel show.

/// The setting that turns vim mode on and off.
pub const SETTING: &str = "keymap/vim";

/// The id of the chat row the vim cursor is on ("" for none).
pub fn cursor() -> String {
    String::new()
}
