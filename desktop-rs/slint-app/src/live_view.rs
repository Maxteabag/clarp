//! Live items in a pane (docs/live-items.md §7): the open turn's entries
//! (`clarp_core::live_present`) as transcript rows, the status line, and the
//! clock that ticks running items' elapsed times from the Host's clock.

use std::collections::HashSet;
use std::time::Duration;

use clarp_core::live_present::{Entry, Kind, Options, Presented};
use clarp_core::protocol::Message;
use clarp_engine::Engine;
use slint::{ModelRc, SharedString, VecModel};

use crate::{LiveRow, MessageRow};

pub(crate) fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}

/// Whether the Host explains tool items (its setting, as `GET /live` and
/// the snapshot say; on by default).
pub(crate) fn explanations(engine: &Engine, session: &str) -> bool {
    engine.tool_explanations_enabled(session)
}

/// The chat's live entries over its `/log` rows, or none without live items.
pub(crate) fn present(engine: &Engine, session: &str, rows: &[Message], expanded: &HashSet<String>) -> Option<Presented> {
    let view = engine.live_view(session)?;
    let options = Options { explanations: explanations(engine, session), expanded: expanded.clone(), host_now_ms: view.host_now_ms(now_ms()) };
    Some(clarp_core::live_present::present(view, rows, &options))
}

/// The status line: its text, whether the agent works, the interrupt key.
pub(crate) fn status(engine: &Engine, session: &str) -> (String, bool, String) {
    let Some(view) = engine.live_view(session) else { return Default::default() };
    match clarp_core::live_present::status_line(view, view.host_now_ms(now_ms())) {
        Some(line) => (line.text, line.busy, if line.busy { stop_key() } else { String::new() }),
        None => Default::default(),
    }
}

/// The key that interrupts the agent, as the keymap has it (Ctrl+. today).
pub(crate) fn stop_key() -> String {
    crate::keymap::resolve("composer", &Default::default(), None)
        .into_iter()
        .find(|b| b.action == "stop-agent")
        .and_then(|b| b.keys.first().cloned())
        .unwrap_or_default()
}

/// Whether anything shown ticks (a running item, a busy status line).
pub(crate) fn ticking(engine: &Engine, session: &str) -> bool {
    let Some(view) = engine.live_view(session) else { return false };
    let busy = clarp_core::live_present::status_line(view, 0).is_some_and(|l| l.busy);
    busy || view.items().iter().any(|i| i.get("status").and_then(|s| s.as_str()) == Some("running"))
}

/// What a live row is built from: an equal signature builds an equal row.
pub(crate) fn signature(entry: &Entry) -> String {
    format!("{}|{}|{}|{}|{}|{}|{}|{}|{}|{}", entry.rev, entry.status, entry.title, entry.meta, entry.secondary, entry.expanded, entry.more, entry.lines.len(), entry.text.len(), entry.items.len())
}

pub(crate) fn row(entry: &Entry, blocks: Vec<crate::MessageBlock>) -> MessageRow {
    let message = entry.kind == Kind::Message;
    MessageRow {
        id: entry.key.clone().into(),
        author: if message { "assistant" } else { "live" }.into(),
        blocks: ModelRc::new(VecModel::from(blocks)),
        sender: SharedString::new(),
        stamp: SharedString::new(),
        meta: SharedString::new(),
        pending: false,
        failed: false,
        activity_label: SharedString::new(),
        group_id: SharedString::new(),
        expanded: false,
        tools: ModelRc::new(VecModel::<crate::ToolRow>::default()),
        cells: ModelRc::new(VecModel::<crate::DisplayCell>::default()),
        artifacts: ModelRc::new(VecModel::<crate::ArtifactItem>::default()),
        live: LiveRow {
            kind: entry.kind.name().into(),
            key: entry.key.clone().into(),
            status: entry.status.clone().into(),
            title: entry.title.clone().into(),
            meta: entry.meta.clone().into(),
            secondary: entry.secondary.clone().into(),
            reserve: entry.reserve_secondary,
            lines: ModelRc::new(VecModel::from(entry.lines.iter().map(|l| SharedString::from(l.as_str())).collect::<Vec<_>>())),
            more: entry.more.clone().into(),
            text: entry.text.clone().into(),
            expandable: entry.expandable,
            expanded: entry.expanded,
        },
    }
}

/// A message entry's blocks (Markdown).
pub(crate) fn message_blocks(text: &str) -> Vec<crate::MessageBlock> {
    clarp_engine::blocks::blocks(text).iter().map(|b| crate::view::message_block(b, false)).collect()
}

thread_local! {
    static TICKER: std::cell::RefCell<Option<slint::Timer>> = const { std::cell::RefCell::new(None) };
}

/// Runs the half-second clock while a pane shows something running, so
/// elapsed times move; stops it when nothing does.
pub(crate) fn keep_ticking(running: bool) {
    TICKER.with(|t| {
        let mut ticker = t.borrow_mut();
        match (running, ticker.is_some()) {
            (true, false) => {
                let timer = slint::Timer::default();
                timer.start(slint::TimerMode::Repeated, Duration::from_millis(500), || {
                    if let Some(app) = crate::app() {
                        app.tick_live();
                    }
                });
                *ticker = Some(timer);
            }
            (false, true) => *ticker = None,
            _ => {}
        }
    });
}
