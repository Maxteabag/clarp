//! The `?` overlay in the window: built for where the keyboard is, and the
//! keyboard given back there when it closes.

use std::cell::Cell;

use slint::{ModelRc, VecModel};

use crate::{App, AppWindow, HelpGroup, Hint, commands};

pub const OVERLAY: &str = "help";

thread_local! {
    /// The map's state the help was opened from.
    static ORIGIN: Cell<&'static str> = const { Cell::new("pane") };
}

pub fn open(app: &App, window: &AppWindow) {
    let state = commands::context(app, window);
    let on_card = state == "pane" && crate::artifacts_view::selected(app).is_some();
    let help = crate::help::help(state, on_card, commands::vim_on(app), &commands::overrides(app));
    ORIGIN.with(|o| o.set(state));
    let groups: Vec<HelpGroup> = help
        .groups
        .into_iter()
        .map(|g| HelpGroup {
            title: g.title.into(),
            items: ModelRc::new(VecModel::from(g.items.into_iter().map(|i| Hint { keys: i.keys.into(), label: i.label.into() }).collect::<Vec<_>>())),
        })
        .collect();
    window.set_help_title(help.title.into());
    window.set_help_groups(ModelRc::new(VecModel::from(groups)));
    window.set_help_footer(help.footer.into());
    commands::open_overlay(app, window, OVERLAY);
}

/// The help closed on the chats: the chat or the explorer it was opened
/// from gets the keyboard back. False when another place takes it.
pub fn give_back(app: &App, window: &AppWindow) -> bool {
    if window.get_surface() != "chats" {
        return false;
    }
    match ORIGIN.with(Cell::get) {
        "sidebar" => window.invoke_focus_sidebar(),
        _ => app.focus_transcript(),
    }
    true
}
