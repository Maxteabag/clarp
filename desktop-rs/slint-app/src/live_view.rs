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
    if !engine.live_active(session) {
        return None;
    }
    let view = engine.live_view(session)?;
    let options = Options { explanations: explanations(engine, session), expanded: expanded.clone(), host_now_ms: view.host_now_ms(now_ms()) };
    let mut presented = clarp_core::live_present::present(view, rows, &options);
    pace(&mut presented);
    Some(presented)
}

/// The chat's finished turns folded (`clarp_core::history_fold`): every
/// settled turn but the one live items show, from its retired live items
/// when the view kept them, else from `/log`.
pub(crate) fn history(engine: &Engine, session: &str, rows: &[Message], expanded: &HashSet<String>, live: Option<&Presented>) -> clarp_core::history_fold::Folded {
    let view = engine.live_view(session).filter(|_| engine.live_active(session));
    let running = view.and_then(|v| v.turn()).is_some_and(|t| matches!(t.get("status").and_then(|s| s.as_str()), Some("running") | None));
    let busy = running || engine.roster().find(session).is_some_and(|a| a.busy);
    let narrating = engine.narrator_enabled() && !engine.narrator_unavailable();
    let explain = |tool: &clarp_core::json::Object| engine.explanation_for(session, tool);
    let options = clarp_core::history_fold::Options {
        summaries: engine.log_turn_summary(),
        explanations: explanations(engine, session),
        expanded: expanded.clone(),
        open_turn: live.map(|l| l.turn_id.clone()).unwrap_or_default(),
        held_rows: live.map(|l| l.own_rows.clone()).unwrap_or_default(),
        busy,
        explain: narrating.then_some(&explain as &dyn Fn(&clarp_core::json::Object) -> String),
    };
    clarp_core::history_fold::present(rows, view, &options)
}

/// The status line: its text, whether the agent works, the interrupt key.
pub(crate) fn status(engine: &Engine, session: &str) -> (String, bool, String) {
    let Some(view) = engine.live_view(session).filter(|_| engine.live_active(session)) else { return Default::default() };
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
    let Some(view) = engine.live_view(session).filter(|_| engine.live_active(session)) else { return false };
    let busy = clarp_core::live_present::status_line(view, 0).is_some_and(|l| l.busy);
    busy || view.items().iter().any(|i| i.get("status").and_then(|s| s.as_str()) == Some("running"))
}

/// What a live row is built from: an equal signature builds an equal row.
pub(crate) fn signature(entry: &Entry) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
        entry.rev,
        entry.status,
        entry.title,
        entry.meta,
        entry.secondary,
        entry.expanded,
        entry.more,
        entry.lines.len(),
        entry.text.len(),
        entry.items.len(),
        entry.explaining,
        entry.row
    )
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
            explaining: entry.explaining,
            lines: ModelRc::new(VecModel::from(entry.lines.iter().map(|l| SharedString::from(l.as_str())).collect::<Vec<_>>())),
            more: entry.more.clone().into(),
            text: entry.text.clone().into(),
            expandable: entry.expandable,
            expanded: entry.expanded,
        },
    }
}

/// Markdown as message blocks, parsed whole.
pub(crate) fn message_blocks(text: &str) -> Vec<crate::MessageBlock> {
    clarp_engine::blocks::blocks(text).iter().map(|b| crate::view::message_block(b, false)).collect()
}

/// A streaming message's blocks, cached per item: the blocks up to the last
/// blank line outside a code fence never change as the text grows, so they
/// are parsed (and styled) once and only the tail after them again.
#[derive(Default)]
pub(crate) struct BlockCache {
    /// Item key → the committed text and its blocks.
    entries: std::collections::HashMap<String, (String, Vec<crate::MessageBlock>)>,
    /// Bytes parsed so far (what the cache saves shows here).
    pub(crate) parsed: usize,
}

impl BlockCache {
    pub(crate) fn blocks(&mut self, key: &str, text: &str) -> Vec<crate::MessageBlock> {
        let point = clarp_core::stream_text::commit_point(text);
        // Only the open turns' messages are kept: an old one is parsed again
        // if it is ever needed.
        if self.entries.len() > 64 && !self.entries.contains_key(key) {
            self.entries.clear();
        }
        let entry = self.entries.entry(key.to_owned()).or_default();
        // Text that no longer extends what was committed starts afresh.
        if !text.starts_with(entry.0.as_str()) || entry.0.len() > point {
            *entry = Default::default();
        }
        if point > entry.0.len() {
            let fresh = &text[entry.0.len()..point];
            self.parsed += fresh.len();
            entry.1.extend(message_blocks(fresh));
            entry.0 = text[..point].to_owned();
        }
        let tail = &text[point..];
        self.parsed += tail.len();
        let mut blocks = entry.1.clone();
        blocks.extend(message_blocks(tail));
        blocks
    }
}

/// The paced reveal of streaming messages: how much of each item's text
/// shows. New text flows out in word steps within a few frames instead of
/// landing in bursts as the Host's appends arrive.
#[derive(Default)]
pub(crate) struct Pacer {
    shown: std::collections::HashMap<String, usize>,
    /// Frames each message took to show (for the checks).
    frames: std::collections::HashMap<String, usize>,
}

impl Pacer {
    /// The part of `text` to show now. A message first seen settled (or long,
    /// opening a chat mid-answer) shows whole.
    pub(crate) fn visible<'a>(&mut self, key: &str, text: &'a str, running: bool) -> &'a str {
        let shown = *self.shown.entry(key.to_owned()).or_insert(if running && text.len() < 400 { 0 } else { text.len() });
        let mut end = shown.min(text.len());
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        &text[..end]
    }

    /// One frame: every message behind its text moves on. True while any
    /// still is.
    pub(crate) fn advance(&mut self, texts: &[(String, usize, String)]) -> bool {
        let mut behind = false;
        for (key, _, text) in texts {
            let shown = self.shown.entry(key.clone()).or_insert(0);
            if *shown < text.len() {
                *shown = clarp_core::stream_text::next_reveal(*shown, text);
                *self.frames.entry(key.clone()).or_default() += 1;
                behind |= *shown < text.len();
            }
        }
        behind
    }

    pub(crate) fn frames(&self, key: &str) -> usize {
        self.frames.get(key).copied().unwrap_or(0)
    }

    pub(crate) fn behind(&self, key: &str, text: &str) -> bool {
        self.shown.get(key).is_some_and(|s| *s < text.len())
    }
}

thread_local! {
    pub(crate) static BLOCKS: std::cell::RefCell<BlockCache> = std::cell::RefCell::default();
    pub(crate) static PACER: std::cell::RefCell<Pacer> = std::cell::RefCell::default();
    static PACING: std::cell::RefCell<Option<slint::Timer>> = const { std::cell::RefCell::new(None) };
    /// The full text of each message being revealed: key → text.
    static TARGETS: std::cell::RefCell<Vec<(String, usize, String)>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// Paces the presented messages: each entry's text becomes what shows now,
/// and the frame clock runs while any is behind.
pub(crate) fn pace(presented: &mut Presented) {
    let mut targets = Vec::new();
    PACER.with(|p| {
        let mut pacer = p.borrow_mut();
        for entry in presented.entries.iter_mut().filter(|e| e.kind == Kind::Message) {
            let running = entry.status == "running" || entry.status == "pending";
            let full = std::mem::take(&mut entry.text);
            entry.text = pacer.visible(&entry.key, &full, running).to_owned();
            if pacer.behind(&entry.key, &full) {
                targets.push((entry.key.clone(), 0, full));
            }
        }
    });
    let behind = !targets.is_empty();
    TARGETS.with(|t| *t.borrow_mut() = targets);
    PACING.with(|p| {
        let mut pacing = p.borrow_mut();
        match (behind, pacing.is_some()) {
            (true, false) => {
                let timer = slint::Timer::default();
                timer.start(slint::TimerMode::Repeated, Duration::from_millis(33), || {
                    let targets = TARGETS.with(|t| t.borrow().clone());
                    PACER.with(|p| p.borrow_mut().advance(&targets));
                    if let Some(app) = crate::app() {
                        app.tick_live();
                    }
                });
                *pacing = Some(timer);
            }
            (false, true) => *pacing = None,
            _ => {}
        }
    });
}

/// A message entry's blocks through the committed-block cache.
pub(crate) fn cached_blocks(entry: &Entry) -> Vec<crate::MessageBlock> {
    if entry.kind != Kind::Message {
        return Vec::new();
    }
    BLOCKS.with(|b| b.borrow_mut().blocks(&entry.key, &entry.text))
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

#[cfg(test)]
mod tests {
    use super::{BlockCache, Pacer, message_blocks};

    /// A message streamed in small appends parses each committed block
    /// once: the work is the tail, not the whole text every time.
    #[test]
    fn a_streaming_message_parses_only_its_tail() {
        let paragraphs: Vec<String> = (0..20).map(|i| format!("Paragraph {i} says **something** with `code` in it.")).collect();
        let full = paragraphs.join("\n\n") + "\n\n```sh\ncargo test\n```\n\nDone.";
        let mut cache = BlockCache::default();
        let (mut whole, mut end) = (0, 0);
        while end < full.len() {
            end = (end + 7).min(full.len());
            while !full.is_char_boundary(end) {
                end += 1;
            }
            cache.blocks("live:m", &full[..end]);
            whole += end;
        }
        assert!(cache.parsed * 5 < whole, "parsed {} bytes, re-parsing would be {whole}", cache.parsed);
        let cached = cache.blocks("live:m", &full);
        let fresh = message_blocks(&full);
        // Committed paragraphs stay blocks of their own (that is what is
        // cached); otherwise the same blocks as parsing it whole.
        let kinds = |blocks: &[crate::MessageBlock]| {
            let mut kinds: Vec<String> = blocks.iter().map(|b| b.kind.to_string()).collect();
            kinds.dedup_by(|a, b| a == "prose" && b == "prose");
            kinds
        };
        assert_eq!(kinds(&cached), kinds(&fresh), "the same blocks as parsing it whole");
        assert_eq!(cached.iter().filter(|b| b.kind == "prose").count(), 21, "each committed paragraph once");
        let code = |blocks: &[crate::MessageBlock]| blocks.iter().filter(|b| b.kind == "code").map(|b| b.text.to_string()).collect::<Vec<_>>();
        assert_eq!(code(&cached), code(&fresh));
        // Text that changed under the cache (not an extension) starts over.
        let other = cache.blocks("live:m", "Something else entirely.\n\nNew.");
        assert_eq!(kinds(&other), kinds(&message_blocks("Something else entirely.\n\nNew.")));
    }

    #[test]
    fn the_pacer_reveals_a_running_message_and_shows_a_settled_one_whole() {
        let mut pacer = Pacer::default();
        let text = "Let me look at the parser and its tests.";
        assert_eq!(pacer.visible("live:a", text, true), "", "a running message starts from nothing");
        let targets = vec![("live:a".to_owned(), 0, text.to_owned())];
        let mut frames = 0;
        while pacer.advance(&targets) {
            frames += 1;
            let shown = pacer.visible("live:a", text, true);
            assert!(text.starts_with(shown) && shown.len() < text.len());
        }
        assert_eq!(pacer.visible("live:a", text, true), text);
        assert!(frames >= 2, "{frames}");
        assert_eq!(pacer.visible("live:b", text, false), text, "settled at first sight: whole");
    }
}
