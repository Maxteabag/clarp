//! A code block or a table over the window (`block_view.slint`): a click on
//! a code block, or a wide table's expand icon, opens it; its copy icons
//! (the chat's and the viewer's) put the code, or the table's Markdown, on
//! the clipboard.

use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::{App, AppWindow, BlockBridge, MessageBlock, TableRow};

pub const OVERLAY: &str = "block";

pub fn bind(window: &AppWindow) {
    let bridge = window.global::<BlockBridge>();
    bridge.on_copy(|text| {
        if let Err(error) = crate::platform::clipboard::copy(&text) {
            if let Some(app) = crate::app() {
                app.engine.borrow_mut().report_error(&format!("Could not copy: {error}"));
            }
        }
    });
    bridge.on_open(|block| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open(&app, &window, block);
        }
    });
}

/// Shows `block` over the window, a table's header row apart from the rows
/// it stays above.
pub fn open(app: &App, window: &AppWindow, block: MessageBlock) {
    let rows: Vec<TableRow> = block.rows.iter().collect();
    let (head, body) = match rows.split_first() {
        Some((head, body)) => (head.clone(), body.to_vec()),
        None => (TableRow::default(), Vec::new()),
    };
    window.set_block_view_head(head);
    window.set_block_view_body(ModelRc::new(VecModel::from(body)));
    window.set_block_view(block);
    crate::commands::open_overlay(app, window, OVERLAY);
    window.invoke_focus_block_view();
}
