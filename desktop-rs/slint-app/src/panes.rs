//! The panes as the window shows them: one `PaneView` per pane of the
//! engine's layout, each with its own transcript model, and the reports the
//! panes send back (where the keyboard is, whether the reader follows).

use std::rc::Rc;

use clarp_engine::{Change, Engine};
use slint::{Model, ModelRc, SharedString, VecModel};

use crate::view::{RowCache, Shown, attachment, sync_rows};
use crate::{App, AppWindow, Attachment, MessageRow, PaneView, SplitView, WorkspaceTab};

/// What a pane last told the window.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Report {
    pub follows: bool,
    pub at_end: bool,
    pub offset: f32,
    pub transcript_focused: bool,
    pub composer_focused: bool,
    /// The row across the top of the chat's view.
    pub top: i32,
}

impl Report {
    /// The reader is at the end of the chat and follows it: they scrolled,
    /// jumped or keyed their way there (each of which sets `follows` from
    /// where the chat then is), or never left. The chat being at its end
    /// alone is not enough: rows handed over or measured again below a
    /// reader scrolled up can bring the end into view for a frame without
    /// the reader going anywhere.
    pub fn reading_the_end(&self) -> bool {
        self.at_end && self.follows
    }
}

/// How many of a chat's latest rows get their tool calls explained.
const NARRATED_ROWS: usize = 40;

pub struct PaneState {
    pub id: String,
    pub session: String,
    pub messages: Rc<VecModel<MessageRow>>,
    shown: Vec<Shown>,
    /// Built rows reused while their source is unchanged.
    rows: RowCache,
    /// The rows' measured heights (the scrollbar's).
    book: crate::scroll_book::HeightBook,
    /// The presentation settings the rows were last presented with; a
    /// preference change re-presents only when these moved.
    presented_with: Option<(i32, bool, bool, bool, (bool, bool, bool))>,
    /// Conversation view (#702): older pages read this visit to fill a
    /// short screen, by the chat and view they were read for.
    fill: (String, u32),
    pub report: Report,
    pub draft: String,
    draft_set: i32,
    focus_composer: i32,
    focus_transcript: i32,
    to_latest: i32,
    scroll_request: i32,
    scroll_amount: f32,
    seek_request: i32,
    seek_index: i32,
    seek_down: bool,
    /// Bumped when the reader stops the chat's turn (`stop_session`).
    stopped: i32,
    /// Message search's mark (chat, row id) and its reveal request.
    marked: (String, String),
    reveal_request: i32,
    reveal_index: i32,
    /// The `@` being typed and the agents it may name.
    pub mention: Option<crate::mention_view::Open>,
    /// A mention closed with Escape stays closed while its `@` is typed on.
    pub mention_dismissed: Option<usize>,
    /// One more than where the cursor goes with the next `draft_set`.
    draft_cursor: i32,
    /// What `scroll_amount` counts: px (""), "pages" or a "row" to go to.
    scroll_kind: &'static str,
    view: PaneView,
    /// Each reply's artifact cards, updated in place: a card is never
    /// rebuilt under a click or a field that has the keyboard.
    cards: std::collections::HashMap<String, Rc<VecModel<crate::ArtifactItem>>>,
    /// Durable rows that took over live rows: row id → the place (key)
    /// they took, kept while the chat is shown so the row never moves.
    slots: std::collections::HashMap<String, String>,
    /// Unsent messages of one's own: when each was first shown (its status
    /// shows once `SEND_STATUS_GRACE` has passed).
    unsent: std::collections::HashMap<String, std::time::Instant>,
    /// The chat's latest row as last presented (its key and signature):
    /// a change while the reader is scrolled up is news (`view.news`).
    tail: Option<(String, String)>,
}

impl PaneState {
    fn new(id: &str) -> Self {
        let messages = Rc::new(VecModel::default());
        let book = crate::scroll_book::HeightBook::new();
        Self {
            id: id.to_owned(),
            session: String::new(),
            view: PaneView {
                id: id.into(),
                messages: ModelRc::from(messages.clone()),
                measure: ModelRc::from(book.measure.clone()),
                before: ModelRc::from(book.before.clone()),
                ..PaneView::default()
            },
            messages,
            shown: Vec::new(),
            rows: RowCache::default(),
            book,
            presented_with: None,
            fill: (String::new(), 0),
            report: Report { follows: true, at_end: true, ..Report::default() },
            draft: String::new(),
            draft_set: 0,
            focus_composer: 0,
            focus_transcript: 0,
            to_latest: 0,
            scroll_request: 0,
            scroll_kind: "",
            scroll_amount: 0.0,
            seek_request: 0,
            seek_index: 0,
            seek_down: false,
            stopped: 0,
            marked: (String::new(), String::new()),
            reveal_request: 0,
            reveal_index: 0,
            mention: None,
            mention_dismissed: None,
            draft_cursor: 0,
            cards: std::collections::HashMap::new(),
            slots: std::collections::HashMap::new(),
            unsent: std::collections::HashMap::new(),
            tail: None,
        }
    }
}

fn number(row: &serde_json::Map<String, serde_json::Value>, key: &str) -> f32 {
    row.get(key).and_then(serde_json::Value::as_f64).unwrap_or(0.0) as f32
}

fn text_of(value: &serde_json::Value, key: &str) -> String {
    value.get(key).and_then(serde_json::Value::as_str).unwrap_or_default().to_owned()
}

fn text(row: &serde_json::Map<String, serde_json::Value>, key: &str) -> String {
    row.get(key).and_then(serde_json::Value::as_str).unwrap_or_default().to_owned()
}

/// What a live row stands for among the transcript's rows: its key.
fn live_source(entry: &clarp_core::live_present::Entry) -> Shown {
    let source = clarp_core::presentation::PresentedRow {
        source_row: usize::MAX,
        message: clarp_core::protocol::Message { id: entry.key.clone(), ..Default::default() },
        body: entry.text.clone(),
        activity: false,
        tools: Vec::new(),
        display_cells: Vec::new(),
        activity_count: 0,
        group_ids: Vec::new(),
        group_label: String::new(),
        group_expanded: false,
        activity_inline: false,
        activity_label: String::new(),
        explanation_repeat: 0,
    };
    (source, entry.expanded, crate::live_view::signature(entry), entry.key.clone())
}

/// Conversation view (#727): an opened turn's work is one bordered box,
/// as on iOS. A finished turn's opened rows (its session rows and its fold's
/// steps) move under its reply; an opened Running box holds its steps. Each
/// row of a box is drawn as one band of it (`MessageRow.work-box`).
fn box_work(
    projection: &clarp_core::chat_view::Projection,
    history: &clarp_core::history_fold::Folded,
    live: Option<&clarp_core::live_present::Presented>,
    fresh: &mut Vec<Shown>,
    rows: &mut Vec<MessageRow>,
) {
    use clarp_core::chat_view::{self, WorkBox};
    use clarp_core::history_fold::Piece;
    use clarp_core::live_present::Kind;
    let mut boxes: Vec<WorkBox> = Vec::new();
    for (anchor, work) in projection.anchors.iter().filter(|(_, w)| w.open) {
        let trace = work.key.strip_prefix(chat_view::WORK_PREFIX).unwrap_or_default();
        if trace.is_empty() {
            continue;
        }
        let mut members: std::collections::HashSet<String> = fresh
            .iter()
            .filter(|(row, ..)| row.source_row != usize::MAX && row.message.trace_id == trace && !chat_view::in_conversation(&row.message))
            .map(|(row, ..)| row.message.id.clone())
            .collect();
        // The fold's own rows: its work row and steps, never its reply.
        for fold in history.folds.iter().filter(|f| chat_view::fold_turn(&f.key) == trace) {
            for piece in &fold.pieces {
                if let Piece::Entry(entry) = piece
                    && !(entry.kind == Kind::Message && entry.phase != "commentary")
                {
                    members.insert(entry.key.clone());
                }
            }
        }
        if !members.is_empty() {
            boxes.push(WorkBox { anchor: anchor.clone(), inside: false, members });
        }
    }
    // The Running box opened: its steps list inside it.
    if let Some(first) = live.and_then(|l| l.entries.first()).filter(|e| e.kind == Kind::Fold && e.expanded && e.key.starts_with("live:fold:")) {
        let members = live.map(|l| l.entries[1..].iter().map(|e| if e.row.is_empty() { e.key.clone() } else { e.row.clone() }).collect()).unwrap_or_default();
        boxes.push(WorkBox { anchor: first.key.clone(), inside: true, members });
    }
    if boxes.is_empty() {
        return;
    }
    let (order, edges) = {
        let ids: Vec<&str> = fresh.iter().map(|(row, ..)| row.message.id.as_str()).collect();
        chat_view::gather(&ids, &boxes)
    };
    let mut taken: Vec<Option<(Shown, MessageRow)>> = std::mem::take(fresh).into_iter().zip(std::mem::take(rows)).map(Some).collect();
    for (index, edge) in order.into_iter().zip(edges) {
        let (mut shown, mut row) = taken[index].take().expect("each row is drawn once");
        if edge != chat_view::Edge::None {
            row.work_box = edge.name().into();
            shown.2 = boxed_signature(&shown.2, edge.name());
        }
        fresh.push(shown);
        rows.push(row);
    }
}

/// A row's signature with its band of a work box: a row that enters or
/// leaves a box, or changes band, is drawn again.
fn boxed_signature(signature: &str, edge: &str) -> String {
    if edge.is_empty() { signature.to_owned() } else { format!("{signature}|box:{edge}") }
}

/// The live failed turn's card while no `/log` row says it yet
/// (docs/turn-errors.md): under its prompt, before any newer prompt and
/// before unsent messages of one's own.
fn splice_live_error(
    key: &str,
    error: &clarp_core::turn_error::TurnError,
    session: &str,
    plan: &clarp_core::turn_error::Plan,
    engine: &clarp_engine::Engine,
    fresh: &mut Vec<Shown>,
    rows: &mut Vec<MessageRow>,
) {
    let sent = fresh.iter().rposition(|(row, ..)| !(row.message.pending || row.message.delivery_failed)).map_or(0, |i| i + 1);
    let newer_prompt = fresh.iter().position(|(row, ..)| !error.message_id.is_empty() && row.message.id == error.message_id).and_then(|at| {
        fresh[at + 1..].iter().position(|(row, ..)| row.message.role == "user").map(|next| at + 1 + next)
    });
    let at = newer_prompt.unwrap_or(sent).min(sent);
    let source = clarp_core::presentation::PresentedRow {
        source_row: usize::MAX,
        message: clarp_core::protocol::Message {
            id: key.to_owned(),
            role: "assistant".into(),
            origin: "system".into(),
            trace_id: error.trace_id.clone(),
            turn_error: Some(error.clone()),
            ..Default::default()
        },
        body: String::new(),
        activity: false,
        tools: Vec::new(),
        display_cells: Vec::new(),
        activity_count: 0,
        group_ids: Vec::new(),
        group_label: String::new(),
        group_expanded: false,
        activity_inline: false,
        activity_label: String::new(),
        explanation_repeat: 0,
    };
    let mut row = crate::view::message_row(&source, false, &std::collections::HashSet::new(), session, None);
    row.error = crate::turn_error_view::finish(error, key, session, plan, engine);
    let signature = format!("turn-error|{}", crate::turn_error_view::signature(&row.error));
    rows.insert(at, row);
    fresh.insert(at, (source, false, signature, key.to_owned()));
}

/// One quiet line under each turn that did not complete, in the Host's
/// words (`turn_end_reason`, #731): drawn as the Host's own system lines are.
fn splice_turn_reasons(lines: &[clarp_core::turn_reason::Line], fresh: &mut Vec<Shown>, rows: &mut Vec<MessageRow>) {
    if lines.is_empty() {
        return;
    }
    let placed = {
        let shown: Vec<clarp_core::turn_reason::Shown<'_>> = fresh.iter().map(|(row, ..)| clarp_core::turn_reason::Shown::of(&row.message, &row.body)).collect();
        clarp_core::turn_reason::place(lines, &shown)
    };
    // Last first, so the earlier places still hold.
    for (at, line) in placed.into_iter().rev() {
        let source = clarp_core::presentation::PresentedRow {
            source_row: usize::MAX,
            message: clarp_core::protocol::Message { id: line.key.clone(), role: "system".into(), text: line.text.clone(), display_text: line.text.clone(), ..Default::default() },
            body: line.text.clone(),
            activity: false,
            tools: Vec::new(),
            display_cells: Vec::new(),
            activity_count: 0,
            group_ids: Vec::new(),
            group_label: String::new(),
            group_expanded: false,
            activity_inline: false,
            activity_label: String::new(),
            explanation_repeat: 0,
        };
        let row = crate::view::message_row(&source, false, &std::collections::HashSet::new(), "", None);
        rows.insert(at, row);
        fresh.insert(at, (source, false, format!("turn-reason|{}", line.text), line.key));
    }
}

impl App {
    /// Brings every pane's scroll book up to its rows; true while rows
    /// remain to be measured. A pane busy elsewhere is left for next time.
    pub fn measure_rows(&self) -> bool {
        let Ok(mut panes) = self.pane_state.try_borrow_mut() else { return true };
        let mut busy = false;
        for pane in panes.iter_mut() {
            busy |= pane.book.update(&pane.messages);
        }
        busy
    }

    /// A row the transcript measured off screen.
    pub fn row_measured(&self, pane: &str, batch: i32, id: &str, height: f32, width: f32) {
        let Ok(mut panes) = self.pane_state.try_borrow_mut() else { return };
        if let Some(pane) = panes.iter_mut().find(|p| p.id == pane) {
            pane.book.measured(batch, id, height, width);
            crate::scroll_book::measure_soon();
        }
    }

    /// A row the transcript drew.
    pub fn row_shown(&self, pane: &str, id: &str, height: f32, width: f32) {
        let Ok(mut panes) = self.pane_state.try_borrow_mut() else { return };
        crate::scroll_journal::event(pane, "height");
        if let Some(pane) = panes.iter_mut().find(|p| p.id == pane)
            && pane.book.shown(&pane.messages, id, height, width)
        {
            crate::scroll_book::measure_soon();
        }
    }

    /// A transcript's width or text size changed: its rows are measured again.
    pub fn relayout(&self, pane: &str) {
        let Ok(mut panes) = self.pane_state.try_borrow_mut() else { return };
        if let Some(pane) = panes.iter_mut().find(|p| p.id == pane) {
            pane.book.relayout();
            crate::scroll_book::measure_soon();
        }
    }

    /// The row `y` px down a pane's chat falls on.
    pub fn row_at(&self, pane: &str, y: f32) -> i32 {
        let Ok(panes) = self.pane_state.try_borrow() else { return 0 };
        panes.iter().find(|p| p.id == pane).map_or(0, |p| p.book.row_at(y) as i32)
    }

    /// The active chat's measured height of row `id`, and its rows measured
    /// and waiting (for the checks).
    pub fn measured_height(&self, id: &str) -> (Option<f32>, (usize, usize)) {
        let Some(index) = self.active_index() else { return (None, (0, 0)) };
        let panes = self.pane_state.borrow();
        (panes[index].book.height(id), panes[index].book.progress())
    }

    /// How long the active chat's last measuring pass took (for the checks).
    pub fn measuring_took(&self) -> Option<std::time::Duration> {
        let index = self.active_index()?;
        self.pane_state.borrow()[index].book.last_pass()
    }

    /// Index of the active pane in `pane_state`.
    pub fn active_index(&self) -> Option<usize> {
        let engine = self.engine.borrow();
        let active = engine.panes().active_pane_id();
        self.pane_state.borrow().iter().position(|p| p.id == active)
    }

    pub fn active_report(&self) -> Report {
        self.active_index().map(|i| self.pane_state.borrow()[i].report).unwrap_or_default()
    }

    /// Whether row `id` of pane `pane` trails a chat's messages: the Running
    /// box or another live-only row (author "live"), or a send still
    /// pending, failed or withdrawn (the layout-shift rule's trailing rows).
    pub fn row_trailing(&self, pane: &str, id: &str) -> bool {
        if id.is_empty() {
            return false;
        }
        let Ok(panes) = self.pane_state.try_borrow() else { return false };
        panes.iter().find(|p| p.id == pane).is_some_and(|p| {
            p.messages.iter().find(|row| row.id == id).is_some_and(|row| row.author == "live" || row.pending || row.failed || row.stopped)
        })
    }

    pub fn active_messages(&self) -> Option<Rc<VecModel<MessageRow>>> {
        self.active_index().map(|i| self.pane_state.borrow()[i].messages.clone())
    }

    /// Rebuilds every transcript from scratch: a view setting (timestamps)
    /// changes rows their source rows do not.
    pub fn rebuild_transcripts(&self) {
        for pane in self.pane_state.borrow_mut().iter_mut() {
            crate::scroll_journal::reset(&pane.id, "rebuild", pane.messages.row_count());
            pane.shown.clear();
            pane.rows.clear();
            pane.presented_with = None;
            pane.cards.clear();
            pane.slots.clear();
            pane.messages.set_vec(Vec::new());
            pane.book.clear();
            pane.to_latest += 1;
        }
        crate::scroll_book::measure_soon();
    }

    /// Panes and transcript rows the window holds, for the memory line.
    pub fn memory_counters(&self) -> (usize, usize) {
        let panes = self.pane_state.borrow();
        (panes.len(), panes.iter().map(|p| p.messages.row_count()).sum())
    }

    /// Each pane's chat and composer text, in layout order.
    pub fn pane_drafts(&self) -> Vec<(String, String, String)> {
        self.pane_state.borrow().iter().map(|p| (p.id.clone(), p.session.clone(), p.draft.clone())).collect()
    }

    /// What the active pane's composer holds.
    pub fn active_draft(&self) -> String {
        self.active_index().map(|i| self.pane_state.borrow()[i].draft.clone()).unwrap_or_default()
    }

    /// The active pane's chat.
    /// The chat whose pane draws the live row `key` (a Running box), the
    /// open chat first.
    pub fn session_showing(&self, key: &str) -> Option<String> {
        let panes = self.pane_state.borrow();
        let has = |i: usize| panes[i].messages.iter().any(|r| r.live.key == key);
        let active = self.active_index().filter(|i| has(*i));
        active.or_else(|| (0..panes.len()).find(|i| has(*i))).map(|i| panes[i].session.clone())
    }

    /// The chat pane `id` shows; None when there is no such pane.
    pub fn pane_session(&self, id: &str) -> Option<String> {
        self.pane_state.borrow().iter().find(|p| p.id == id).map(|p| p.session.clone())
    }

    pub fn active_session(&self) -> String {
        self.active_index().map(|i| self.pane_state.borrow()[i].session.clone()).unwrap_or_default()
    }

    pub fn active_id(&self) -> SharedString {
        self.engine.borrow().panes().active_pane_id().into()
    }

    pub fn active_view(&self) -> Option<PaneView> {
        self.active_index().map(|i| self.pane_state.borrow()[i].view.clone())
    }

    fn bump(&self, apply: impl Fn(&mut PaneState)) {
        let Some(index) = self.active_index() else { return };
        let mut panes = self.pane_state.borrow_mut();
        apply(&mut panes[index]);
        let view = self.pane_view(&panes[index]);
        panes[index].view = view.clone();
        self.panes.set_row_data(index, view);
    }

    /// An empty pane has no composer to type into; its transcript keeps
    /// the keyboard so shortcuts still reach the map.
    pub fn focus_composer(&self) {
        self.bump(|p| if p.session.is_empty() { p.focus_transcript += 1 } else { p.focus_composer += 1 });
    }
    pub fn focus_transcript(&self) {
        self.bump(|p| p.focus_transcript += 1);
    }
    pub fn to_latest(&self) {
        self.bump(|p| p.to_latest += 1);
    }

    /// The reader stops `session`'s turn (Stop, Ctrl+., a stalled turn's
    /// Stop). Every chat showing it hears so: what the stop settles (the
    /// partial answer, the line saying it was stopped) never moves the
    /// reader, who stays at the end if they were there (#846).
    pub fn stop_session(&self, session: &str) {
        self.engine.borrow_mut().stop_session(session);
        if session.is_empty() {
            return;
        }
        let mut panes = self.pane_state.borrow_mut();
        for (index, pane) in panes.iter_mut().enumerate().filter(|(_, p)| p.session == session) {
            pane.stopped += 1;
            let view = self.pane_view(pane);
            pane.view = view.clone();
            self.panes.set_row_data(index, view);
        }
    }
    /// Scrolls the active transcript by `delta` px (down for positive).
    pub fn scroll_by(&self, delta: f32) {
        self.bump(|p| {
            p.scroll_request += 1;
            p.scroll_amount = delta;
            p.scroll_kind = "";
        });
    }
    /// Scrolls the active transcript by `pages` of its height.
    pub fn scroll_pages(&self, pages: f32) {
        self.bump(|p| {
            p.scroll_request += 1;
            p.scroll_amount = pages;
            p.scroll_kind = "pages";
        });
    }
    /// Brings row `index` of the active transcript to the top of its view.
    pub fn scroll_to_row(&self, index: usize) {
        self.bump(|p| {
            p.scroll_request += 1;
            p.scroll_amount = index as f32;
            p.scroll_kind = "row";
        });
    }

    /// Brings row `index` of the active transcript into view by the rows'
    /// measured heights (`down`: its end to the bottom, when it fits).
    pub fn seek_row(&self, index: usize, down: bool) {
        self.bump(|p| {
            p.seek_request += 1;
            p.seek_index = index as i32;
            p.seek_down = down;
        });
    }

    /// Marks message `message_id` of `session` in the active pane and
    /// brings it into view; false while the pane does not show it (yet).
    pub fn reveal_message(&self, session: &str, message_id: &str) -> bool {
        let Some(index) = self.active_index() else { return false };
        let mut panes = self.pane_state.borrow_mut();
        let pane = &mut panes[index];
        if pane.session != session {
            return false;
        }
        let Some(at) = pane.shown.iter().position(|s| s.0.message.id == message_id || s.3 == message_id) else { return false };
        let Some(row) = pane.messages.row_data(at) else { return false };
        pane.marked = (session.to_owned(), row.id.to_string());
        pane.reveal_request += 1;
        pane.reveal_index = at as i32;
        let view = self.pane_view(pane);
        pane.view = view.clone();
        self.panes.set_row_data(index, view);
        true
    }

    /// Pane `id` no longer marks a row (the find bar closed).
    pub fn clear_marked(&self, id: &str) {
        self.change_pane(id, |pane| {
            let had = !pane.marked.1.is_empty();
            pane.marked = (String::new(), String::new());
            had
        });
    }

    /// The row message search marked in the active pane (for the checks).
    pub fn marked_row(&self) -> String {
        self.active_index().map(|i| self.pane_state.borrow()[i].view.marked.to_string()).unwrap_or_default()
    }

    /// Changes pane `id` and shows it again.
    pub fn with_pane<R>(&self, id: &str, change: impl FnOnce(&mut PaneState) -> R) -> Option<R> {
        let mut panes = self.pane_state.borrow_mut();
        let index = panes.iter().position(|p| p.id == id)?;
        let result = change(&mut panes[index]);
        let view = self.pane_view(&panes[index]);
        panes[index].view = view.clone();
        self.panes.set_row_data(index, view);
        Some(result)
    }

    /// Changes pane `id`, and shows it again when `change` says it changed.
    pub fn change_pane(&self, id: &str, change: impl FnOnce(&mut PaneState) -> bool) {
        let mut panes = self.pane_state.borrow_mut();
        let Some(index) = panes.iter().position(|p| p.id == id) else { return };
        if change(&mut panes[index]) {
            let view = self.pane_view(&panes[index]);
            panes[index].view = view.clone();
            self.panes.set_row_data(index, view);
        }
    }

    /// Puts `text` in pane `id`'s composer with the cursor at `cursor`
    /// (bytes), and in the chat's draft (and other panes on it).
    pub fn set_draft_at(&self, id: &str, text: &str, cursor: usize) {
        let Some(session) = self.with_pane(id, |pane| {
            pane.draft = text.to_owned();
            pane.draft_set += 1;
            pane.draft_cursor = cursor as i32 + 1;
            pane.session.clone()
        }) else {
            return;
        };
        self.engine.borrow_mut().set_draft(&session, text);
        self.replace_draft(&session, text, Some(id));
    }

    /// Pane `id` stopped following a new reply with its first line at the
    /// top: the rest of it is below, which the jump-to-latest dot says.
    pub fn news_below(&self, id: &str) {
        let mut panes = self.pane_state.borrow_mut();
        let Some(index) = panes.iter().position(|p| p.id == id) else {
            return;
        };
        if panes[index].view.news {
            return;
        }
        panes[index].view.news = true;
        let view = self.pane_view(&panes[index]);
        panes[index].view = view.clone();
        if index < self.panes.row_count() {
            self.panes.set_row_data(index, view);
        }
    }

    /// A pane's report, from its transcript and composer.
    /// A pane's report; true when more than where it is scrolled to (the
    /// offset, the top row) changed.
    pub fn reported(&self, id: &str, report: Report) -> bool {
        let mut panes = self.pane_state.borrow_mut();
        let Some(index) = panes.iter().position(|p| p.id == id) else {
            return true;
        };
        let old = std::mem::replace(&mut panes[index].report, report);
        // Back at the end, the news is read.
        if report.reading_the_end() && panes[index].view.news {
            panes[index].view.news = false;
            let view = self.pane_view(&panes[index]);
            panes[index].view = view.clone();
            if index < self.panes.row_count() {
                self.panes.set_row_data(index, view);
            }
        }
        Report { offset: report.offset, top: report.top, ..old } != report
    }

    /// Typing in one pane updates the chat's draft; other panes on the same
    /// chat show it too.
    pub fn draft_edited(&self, id: &str, text: &str) {
        let session = {
            let mut panes = self.pane_state.borrow_mut();
            let Some(pane) = panes.iter_mut().find(|p| p.id == id) else { return };
            pane.draft = text.to_owned();
            pane.session.clone()
        };
        self.engine.borrow_mut().set_draft(&session, text);
        self.replace_draft(&session, text, Some(id));
    }

    /// Puts `text` in every composer on `session` (but `except`). Typing
    /// (`except`, the pane typed in) changes only the other composers'
    /// text, through DraftMirror; otherwise their rows are replaced.
    pub fn replace_draft(&self, session: &str, text: &str, except: Option<&str>) {
        let mut panes = self.pane_state.borrow_mut();
        let mut mirrored = false;
        for index in 0..panes.len() {
            let pane = &mut panes[index];
            if pane.session != session || Some(pane.id.as_str()) == except || pane.draft == text {
                continue;
            }
            pane.draft = text.to_owned();
            if let Some(except) = except {
                if !mirrored && let Some(window) = crate::window() {
                    use slint::ComponentHandle;
                    let mirror = window.global::<crate::DraftMirror>();
                    mirror.set_session(session.into());
                    mirror.set_except(except.into());
                    mirror.set_text(text.into());
                    mirror.set_set(mirror.get_set().wrapping_add(1));
                }
                mirrored = true;
                continue;
            }
            pane.draft_set += 1;
            let view = self.pane_view(pane);
            pane.view = view.clone();
            self.panes.set_row_data(index, view);
        }
    }

    pub fn session_of(&self, id: &str) -> String {
        self.pane_state.borrow().iter().find(|p| p.id == id).map(|p| p.session.clone()).unwrap_or_default()
    }

    fn pane_view(&self, pane: &PaneState) -> PaneView {
        let mut view = pane.view.clone();
        view.draft = pane.draft.clone().into();
        view.draft_set = pane.draft_set;
        view.focus_composer = pane.focus_composer;
        view.focus_transcript = pane.focus_transcript;
        view.to_latest = pane.to_latest;
        view.scroll_request = pane.scroll_request;
        view.scroll_amount = pane.scroll_amount;
        view.seek_request = pane.seek_request;
        view.seek_index = pane.seek_index;
        view.seek_down = pane.seek_down;
        view.stopped = pane.stopped;
        view.marked = if pane.marked.0 == pane.session { pane.marked.1.clone() } else { String::new() }.into();
        view.reveal_request = pane.reveal_request;
        view.reveal_index = pane.reveal_index;
        view.draft_cursor = pane.draft_cursor;
        crate::mention_view::apply(self, pane, &mut view);
        view.scroll_kind = pane.scroll_kind.into();
        view
    }

    fn header(&self, engine: &Engine, view: &mut PaneView, session: &str) {
        let agent = engine.roster().find(session);
        view.name = if session.is_empty() { SharedString::new() } else { engine.chat_name(session).into() };
        // The header's avatar without its portrait (`header_portrait`).
        view.initial = crate::view::initial(&view.name);
        view.symbol = agent.map(|a| a.avatar_symbol.clone()).unwrap_or_default().into();
        // The status line starts with the Host badge, then a moved agent's
        // "Moved to …" (#651, Host contract 125).
        view.host = engine.host_badge(session).into();
        view.moved = agent.map(clarp_core::all_hosts::moved_line).unwrap_or_default().into();
        view.moving = crate::profile_view::moving_line(engine, session).into();
        view.pair = session.starts_with("pair:");
        view.model = agent.map(|a| a.model.clone()).unwrap_or_default().into();
        view.effort = agent.map(|a| a.effort.clone()).unwrap_or_default().into();
        view.status = agent.map(|a| a.status_text.clone()).unwrap_or_default().into();
        view.default_effort = agent.map(|a| engine.default_effort_for_model(&a.backend, &a.model)).unwrap_or_default().into();
        view.busy = agent.is_some_and(|a| a.busy);
        view.working = crate::cells_view::working(engine, session);
        let (status, busy, key) = crate::live_view::status(engine, session);
        if engine.live_active(session) {
            // The status line, or the working row's shimmer, stands in for
            // the typing dots.
            view.working = false;
        }
        view.live_status = status.into();
        view.live_busy = busy;
        view.live_stop_key = key.into();
        view.stall = Self::stall_indicator(engine, session).into();
        view.availability = Self::availability_line(engine, session).into();
        view.stall_stop = clarp_core::stall::STOP_LABEL.into();
        // The stalled indicator's Stop names its key, as the status line's does.
        if !view.stall.is_empty() && view.live_stop_key.is_empty() {
            view.live_stop_key = crate::live_view::stop_key(engine).into();
        }
        let path = agent.map(|a| a.working_directory.clone()).unwrap_or_default();
        if path.is_empty() {
            view.workspace_kind = SharedString::new();
            view.workspace_label = SharedString::new();
        } else {
            let described = self.workspaces.borrow_mut().describe(&path, engine.shared_filesystem());
            view.workspace_kind = clarp_core::json::string(&described, "kind").into();
            view.workspace_label = clarp_core::json::string(&described, "label").into();
        }
    }

    /// Recording, transcribing and playback, for every pane.
    pub fn voice_state(&self) {
        let (recording_session, playing) =
            crate::platform::audio::with(|audio| (audio.recording().then(|| audio.recording_target()), audio.playing() || audio.paused()))
                .unwrap_or((None, false));
        let mut panes = self.pane_state.borrow_mut();
        for index in 0..panes.len() {
            let pane = &mut panes[index];
            let session = pane.session.clone();
            let transcribing = crate::platform::audio::with(|audio| audio.transcriptions_for_session(&session)).unwrap_or(0);
            let recording = recording_session.as_deref() == Some(session.as_str());
            if pane.view.transcribing == transcribing && pane.view.recording == recording && pane.view.playing == playing {
                continue;
            }
            pane.view.transcribing = transcribing;
            pane.view.recording = recording;
            pane.view.playing = playing;
            let view = self.pane_view(pane);
            pane.view = view.clone();
            self.panes.set_row_data(index, view);
        }
    }

    fn composer(engine: &Engine, view: &mut PaneView, session: &str) {
        let attachments: Vec<Attachment> = engine.attachments(session).iter().map(attachment).collect();
        view.attachments = ModelRc::new(VecModel::from(attachments));
        view.can_send = engine.can_send(session);
        view.queued = crate::view::queued_shown(session, engine.queue_count(session));
        view.quota_notice = engine.quota_notice(session).into();
        view.queue_paused = engine.queue_paused(session);
        view.stop_notice = engine.composer_stop_notice(session).into();
        // The queue strip: the loaded queue's waiting messages, while the
        // Host still counts some (a stale list never outlives its count).
        // Hidden by the reader's setting (Show the queue off): no line at all.
        let listed = (view.queued > 0 || engine.queue_count(session) > 0) && crate::queue_strip::shown(engine);
        let waiting = if listed { crate::queue_strip::waiting(engine, session) } else { Vec::new() };
        // The line also says what the Host did with a send instead while
        // nothing waits, so it is asked whenever the line may show.
        let chip = if crate::queue_strip::shown(engine) { crate::queue_strip::chip(engine, session) } else { Default::default() };
        view.chip_kind = chip.kind.into();
        view.chip_target = chip.target.into();
        view.chip_label = chip.label.into();
        view.chip_count = chip.count;
        view.chip_reason = chip.reason.into();
        view.chip_acting = chip.acting;
        view.waiting = ModelRc::new(VecModel::from(waiting));
        view.waiting_refused = if chip.count > 0 {
            clarp_core::queue_line::refusal_line(&engine.turn_queue_refusal(session)).into()
        } else {
            Default::default()
        };
        let (meter, warn) = crate::queue_strip::meter(engine, session);
        view.meter = meter.into();
        view.meter_warn = warn;
    }

    /// The open chat's working indicator for a stalled turn (#249), "" when
    /// the Host offers no Stop for a stall.
    /// In Conversation view the Running box carries it, Stop inside (#702):
    /// the pane then draws no line of its own.
    fn stall_indicator(engine: &Engine, session: &str) -> String {
        if crate::live_view::running_box_shown(engine, session) {
            return String::new();
        }
        let Some(agent) = engine.roster().find(session) else { return String::new() };
        let Some(since) = clarp_core::stall::stop_offered(agent) else { return String::new() };
        clarp_core::stall::indicator(since, chrono::Utc::now().timestamp_millis(), agent.queued_turn_count > 0)
    }

    /// The chat header's portrait: the one the chat list draws (its size,
    /// already made), or none for an empty pane or a pair room.
    fn header_portrait(&self, session: &str) -> slint::Image {
        if session.is_empty() || session.starts_with("pair:") {
            return slint::Image::default();
        }
        let sizes = crate::avatar_view::sizes(self);
        let compact = crate::window().is_some_and(|w| w.get_explorer_compact());
        crate::avatar_view::portrait(self, session, if compact { sizes.compact } else { sizes.row })
    }

    /// Why the open chat's agent cannot answer the user now (#918), the
    /// header's short line, counted on its Host's clock; "" while it can,
    /// and while it works on the user's own turn (`header_line`). The chat
    /// list keeps its own line (`view::row_activity`).
    fn availability_line(engine: &Engine, session: &str) -> String {
        let clock = clarp_core::availability::Clock::now(engine.host_clock_offset_ms(session));
        let roster = engine.roster();
        let audience = roster.find(session).map(|a| a.working_audience.as_str()).unwrap_or_default();
        roster.availability(session).and_then(|a| clarp_core::availability::header_line(a, audience, clock)).unwrap_or_default()
    }

    /// Once a second with the turn clock: a stalled chat's minutes move on,
    /// so only the panes whose indicator changed are rewritten.
    pub fn tick_stall_notices(&self) {
        let Ok(engine) = self.engine.try_borrow() else { return };
        let Ok(mut panes) = self.pane_state.try_borrow_mut() else { return };
        for index in 0..panes.len() {
            let pane = &mut panes[index];
            if pane.view.stall.is_empty() {
                continue;
            }
            let stall = Self::stall_indicator(&engine, &pane.session);
            if pane.view.stall == stall.as_str() {
                continue;
            }
            pane.view.stall = stall.into();
            if let Some(mut row) = self.panes.row_data(index).filter(|row| row.id == pane.view.id) {
                row.stall = pane.view.stall.clone();
                self.panes.set_row_data(index, row);
            }
        }
    }

    /// Every ten seconds (`App::tick_availability`): the header lines of
    /// agents that cannot answer count their wait; only the panes whose
    /// line changed are rewritten.
    pub fn tick_availability_lines(&self) {
        let Ok(engine) = self.engine.try_borrow() else { return };
        let Ok(mut panes) = self.pane_state.try_borrow_mut() else { return };
        for index in 0..panes.len() {
            let pane = &mut panes[index];
            if pane.view.availability.is_empty() {
                continue;
            }
            let line = Self::availability_line(&engine, &pane.session);
            if pane.view.availability == line.as_str() {
                continue;
            }
            pane.view.availability = line.into();
            if let Some(mut row) = self.panes.row_data(index).filter(|row| row.id == pane.view.id) {
                row.availability = pane.view.availability.clone();
                self.panes.set_row_data(index, row);
            }
        }
    }

    fn messages(&self, pane: &mut PaneState) {
        crate::artifacts_view::settle_downloads(self, &pane.session);
        // Live items (§7): the open turn's rows, over the /log rows. A live
        // /log row an item shows is not shown twice, nor a durable row (or
        // its tools) the item rows show instead.
        // Finished turns fold the same way (§7.6): their rows show as the
        // fold, their tools as its rows.
        let (live, history, projection, reasons, errors) = {
            let engine = self.engine.borrow();
            let rows = engine.conversation(&pane.session).map(|c| c.rows().to_vec()).unwrap_or_default();
            // Failed turns the Host explains (`turn_error_explained`): one
            // card each, repeats as one, the live failure until its notice lands.
            let live_error = engine.live_view(&pane.session).and_then(|v| v.turn()).and_then(|turn| {
                Some((clarp_core::json::string(turn, "turn_id"), clarp_core::turn_error::of_live_turn(turn)?))
            });
            let errors = clarp_core::turn_error::plan(&rows, live_error.as_ref().map(|(id, error)| (id.as_str(), error.clone())));
            // Why each turn that did not complete ended (`turn_end_reason`, #731);
            // a turn its card explains says it once, on the card.
            let mut reasons = if engine.turn_end_reason(&pane.session) { clarp_core::turn_reason::ended(&rows) } else { Vec::new() };
            reasons.retain(|line| !errors.traces.contains(&line.turn_id));
            let expanded = self.expanded.borrow();
            let live = crate::live_view::present(&engine, &pane.session, &rows, &expanded);
            let history = crate::live_view::history(&engine, &pane.session, &rows, &expanded, live.as_ref());
            // Conversation view (#702): the rows it leaves out, and each
            // finished turn's Show work on its reply.
            let projection = crate::live_view::conversation_view(&engine, &pane.session, &rows).then(|| clarp_core::chat_view::project(&rows, &expanded));
            (live, history, projection, reasons, errors)
        };
        let mut hide = history.hidden_rows.clone();
        hide.extend(errors.hidden.iter().cloned());
        if let Some(projection) = &projection {
            hide.extend(projection.hidden_rows.iter().cloned());
        }
        let mut strip = history.stripped_calls.clone();
        if let Some(live) = &live {
            hide.extend(live.absorbed_rows.iter().chain(&live.hidden_rows).cloned());
            strip.extend(live.stripped_calls.iter().cloned());
        }
        let mut presented = self.engine.borrow_mut().presented_except(&pane.session, &hide, &strip);
        if let Some(live) = &live {
            presented.retain(|row| !live.hidden_rows.contains(&row.message.id));
        }
        let always = self.engine.borrow().chat_activity_mode(&pane.session) == clarp_core::presentation::ALWAYS_VISIBLE;
        let expanded = self.expanded.borrow();
        let stamps = self.prefs.borrow().timestamps;
        let session_artifacts = self.engine.borrow().artifacts_for_session(&pane.session);
        let artifacts = crate::cells_view::artifacts_by_row(&presented, &session_artifacts);
        // A native form's card reads its spec when it is drawn.
        crate::form_view::want(self, &session_artifacts);
        let agent_name = self.engine.borrow().roster().find(&pane.session).map(|a| clarp_core::protocol::display_name(a).to_owned()).unwrap_or_default();
        let mentionable = crate::mention_view::candidates(&self.engine.borrow());
        let cursor = self.artifact_cursor.borrow().clone();
        // Image blocks show the keyboard's place from the bridge.
        if let Some(window) = crate::window() {
            use slint::ComponentHandle;
            let bridge = window.global::<crate::ArtifactBridge>();
            bridge.set_cursor(cursor.clone().into());
            bridge.set_tile(crate::artifacts_view::tile());
        }
        let choices = self.artifact_choices.borrow().clone();
        let drafts = self.artifact_drafts.borrow().clone();
        let editing = self.artifact_editing.borrow().clone();
        // Decisions shown pending with a why line keep it once answered.
        for artifact in self.engine.borrow().artifacts_for_session(&pane.session) {
            let shown = crate::artifacts_view::artifact_item(&artifact);
            if shown.pending && !shown.meta.is_empty() {
                self.artifact_seen_pending.borrow_mut().insert(shown.id.to_string());
            }
        }
        let seen_pending = self.artifact_seen_pending.borrow().clone();
        let state = crate::artifacts_view::CardState { cursor: &cursor, choices: &choices, drafts: &drafts, editing: &editing, seen_pending: &seen_pending };
        pane.presented_with = Some(self.presentation_key(&pane.session));
        let image_host = self.engine.borrow().transcript_image_host(&pane.session).map(str::to_owned);
        // The find bar's words, marked in the rows holding them all.
        let highlight = crate::find_view::highlight(&pane.id, &pane.session);
        let lit: std::collections::HashSet<String> = highlight
            .as_ref()
            .map(|h| {
                presented
                    .iter()
                    .filter(|row| crate::find_view::row_matches(&row.message, &h.terms))
                    .map(|row| row.message.id.clone())
                    .collect()
            })
            .unwrap_or_default();
        let built = pane.rows.rows(&presented, always, &expanded, &pane.session, image_host.as_deref(), highlight.as_ref().map(|h| (h, &lit)));
        let lit_key = highlight.as_ref().map(|h| format!("{}{}", h.terms.join(" "), h.open)).unwrap_or_default();
        let now = std::time::Instant::now();
        let mut unsent = std::collections::HashMap::new();
        let mut kept = std::collections::HashMap::new();
        let rows: Vec<MessageRow> = presented
            .iter()
            .zip(&artifacts)
            .zip(built)
            .map(|((row, artifacts), mut shown)| {
                if !stamps {
                    shown.stamp = SharedString::new();
                }
                // An unsent message says so only once the grace period has
                // passed; the pane is refreshed then, once.
                if shown.pending {
                    let since = pane.unsent.get(&row.message.id).copied();
                    let first = since.unwrap_or(now);
                    unsent.insert(row.message.id.clone(), first);
                    match crate::view::send_status_due(now.duration_since(first)) {
                        Ok(()) => shown.status_due = true,
                        Err(left) if since.is_none() => {
                            let session = pane.session.clone();
                            slint::Timer::single_shot(left, move || {
                                if let Some(app) = crate::app() {
                                    app.refresh(&[Change::Conversation(session)]);
                                }
                            });
                        }
                        Err(_) => {}
                    }
                }
                // Show work on a finished turn's reply (Conversation view).
                if let Some(work) = projection.as_ref().and_then(|p| p.anchors.get(&row.message.id)) {
                    shown.work_key = work.key.clone().into();
                    shown.work_open = work.open;
                }
                // A failed turn's card: its count, buttons and Details.
                if let Some(error) = row.message.turn_error.as_ref().filter(|_| shown.author == "error" && !row.message.automated) {
                    shown.error = crate::turn_error_view::finish(error, &row.message.id, &pane.session, &errors, &self.engine.borrow());
                }
                // A message addressed to this chat's agent by an @-mention.
                if shown.author == "user" {
                    shown.meta = crate::mention_view::sent_to(&mentionable, &pane.session, &row.message.text).into();
                }
                // A receipt names whose question it was and goes to its card
                // when the card is in the chat.
                if !shown.receipt.key.is_empty() {
                    let card = session_artifacts.iter().find(|a| text_of(a, "artifact_id") == shown.receipt.artifact_id.as_str());
                    shown.receipt.agent = agent_name.clone().into();
                    shown.receipt.linked = card.is_some();
                    if let Some(card) = card {
                        shown.receipt.kind = if text_of(card, "type") == "question" { "QUESTION" } else { "DECISION" }.into();
                    }
                }
                let engine = self.engine.borrow();
                let cards: Vec<crate::ArtifactItem> = artifacts.iter().map(|a| crate::artifacts_view::card(a, &engine, &state)).collect();
                let model = pane.cards.remove(&row.message.id).unwrap_or_default();
                crate::artifacts_view::update_cards(&model, cards);
                shown.artifacts = ModelRc::from(model.clone());
                kept.insert(row.message.id.clone(), model);
                shown
            })
            .collect();
        pane.cards = kept;
        pane.unsent = unsent;
        // Opened activity the Host sent without its tool calls: fetch them.
        let mut engine = self.engine.borrow_mut();
        for (row, shown) in presented.iter().zip(&rows) {
            if !shown.expanded {
                continue;
            }
            if row.group_label.is_empty() {
                if row.message.tool_details_available {
                    engine.load_tool_details(&pane.session, &row.message.id);
                }
            } else {
                for id in &row.group_ids {
                    engine.load_tool_details(&pane.session, id);
                }
            }
        }
        for id in &history.details_wanted {
            engine.load_tool_details(&pane.session, id);
        }
        // Plain-English tools: the latest open rows' calls, explained.
        let narrating = engine.narrator_enabled() && !engine.narrator_unavailable();
        if narrating {
            for tool in &history.unexplained {
                if !engine.explanation_failed(&pane.session, tool) {
                    engine.request_explanation(&pane.session, tool);
                }
            }
        }
        let mut rows = rows;
        let recent = rows.len().saturating_sub(NARRATED_ROWS);
        for (index, (row, shown)) in presented.iter().zip(rows.iter_mut()).enumerate() {
            if !narrating || index < recent || !shown.expanded {
                continue;
            }
            let calls: Vec<crate::ToolRow> = row
                .tools
                .iter()
                .zip(shown.tools.iter())
                .map(|(tool, mut card)| {
                    let Some(activity) = tool.as_object() else { return card };
                    let failed = engine.explanation_failed(&pane.session, activity);
                    card.narrated = !failed;
                    card.explanation = engine.explanation_for(&pane.session, activity).into();
                    if !failed && card.explanation.is_empty() {
                        engine.request_explanation(&pane.session, activity);
                    }
                    card
                })
                .collect();
            shown.tools = ModelRc::new(VecModel::from(calls));
        }
        drop(engine);
        let signature = |artifacts: &Vec<serde_json::Value>, row: &MessageRow| {
            let explained: Vec<String> = row.tools.iter().map(|t| format!("{}:{}", t.narrated, t.explanation)).collect();
            let cards = artifacts
                .iter()
                .map(|a| format!("{}@{}", text_of(a, "artifact_id"), a.get("updated_at").cloned().unwrap_or_default()))
                .collect::<Vec<_>>()
                .join(",");
            // A row with images is drawn again when one lands.
            let pictures = if row.blocks.iter().any(|b| b.kind == "images") {
                format!("{}@{}", image_host.as_deref().unwrap_or_default(), crate::artifacts_view::pictures_landed())
            } else {
                String::new()
            };
            let receipt = format!("{}:{}:{}", row.receipt.agent, row.receipt.kind, row.receipt.linked);
            // Another agent's prompt is drawn again when it opens or folds.
            // An unsent message is drawn again when its status becomes due.
            // A failure card is drawn again when its count or buttons change.
            let error = if row.error.id.is_empty() { String::new() } else { crate::turn_error_view::signature(&row.error) };
            // A row the find bar marks is drawn again when its words change.
            let found = if lit.contains(row.id.as_str()) { lit_key.as_str() } else { "" };
            format!("{cards}|{}|{pictures}|{receipt}|{}|{}|{}:{}|{error}|{found}", explained.join(","), row.prompt.expanded, row.status_due, row.work_key, row.work_open)
        };
        let signatures: Vec<String> = artifacts.iter().zip(&rows).map(|(a, row)| signature(a, row)).collect();
        let mut fresh: Vec<Shown> = presented
            .into_iter()
            .zip(rows.iter().map(|r| r.expanded))
            .zip(signatures)
            .map(|((row, open), s)| {
                let key = row.message.id.clone();
                (row, open, s, key)
            })
            .collect();
        let mut rows = rows;
        let mut kept: std::collections::HashSet<String> = std::collections::HashSet::new();
        self.splice_history(pane, &history, &mut fresh, &mut rows, &mut kept);
        if let Some(live) = &live {
            self.splice_live(pane, live, &mut fresh, &mut rows);
            kept.extend(live.entries.iter().filter(|e| e.row.is_empty()).map(|e| e.key.clone()));
        }
        if let Some((key, error)) = &errors.live {
            splice_live_error(key, error, &pane.session, &errors, &self.engine.borrow(), &mut fresh, &mut rows);
        }
        splice_turn_reasons(&reasons, &mut fresh, &mut rows);
        self.splice_coordination(pane, &mut fresh, &mut rows, &mut kept);
        if let Some(projection) = &projection {
            box_work(projection, &history, live.as_ref(), &mut fresh, &mut rows);
        }
        pane.rows.keep_live(&kept.iter().map(String::as_str).collect());
        let (removed, inserted, updated) = sync_rows(&pane.messages, &mut pane.shown, fresh, rows);
        // The latest row changed while the reader does not follow the end
        // (scrolled up, whether or not the end shows for a frame): news.
        // Older rows read in at the top leave the latest row as it was.
        let tail = pane.shown.last().map(|s| (s.3.clone(), s.2.clone()));
        if pane.tail.is_some() && tail != pane.tail && !pane.report.follows {
            pane.view.news = true;
        }
        pane.tail = tail;
        if removed + inserted + updated > 0 {
            crate::scroll_journal::event(&pane.id, &format!("rows -{removed} +{inserted} ~{updated}"));
        }
        crate::scroll_book::measure_soon();
        self.fill_conversation(pane, projection.is_some());
    }

    /// A Conversation page shorter than the screen must not look empty:
    /// older pages are read, as scrolling up reads them, until there are
    /// enough rows, history ends or five pages were read this round. A new
    /// round begins when the chat opens or switches to Conversation.
    fn fill_conversation(&self, pane: &mut PaneState, conversation: bool) {
        const ROWS: usize = 12;
        const PAGES: u32 = 5;
        let round = if conversation { pane.session.clone() } else { String::new() };
        if pane.fill.0 != round {
            pane.fill = (round, 0);
        }
        if !conversation || pane.shown.len() >= ROWS || pane.fill.1 >= PAGES {
            return;
        }
        let mut engine = self.engine.borrow_mut();
        let more = engine.conversation(&pane.session).is_some_and(|c| c.has_more() && !c.loading());
        if more {
            pane.fill.1 += 1;
            engine.load_older(&pane.session);
        }
    }

    /// What a transcript's rows depend on among the preferences.
    fn presentation_key(&self, session: &str) -> (i32, bool, bool, bool, (bool, bool, bool)) {
        let engine = self.engine.borrow();
        // The quiet line where agent messages were (#653) is drawn or not.
        let line = engine.chat_hidden_coordination(session) && engine.settings().boolean(crate::catalogue::COORDINATION_LINE, true);
        // The chat's own Tool activity (#653), else the global one.
        // The chat's view (#702): its own choice, Clean chat or the Host's.
        // Its Running box stands for a working agent's turn when no live
        // turn does, so a snapshot that starts or ends one draws it again.
        let conversation = engine.chat_view_conversation(session);
        let busy = conversation && engine.roster().find(session).is_some_and(|a| a.busy);
        // A stall puts the box in even where no turn would (#249).
        let stalled = busy && engine.roster().find(session).and_then(clarp_core::stall::stop_offered).is_some();
        (engine.chat_activity_mode(session), engine.show_when_ready(), self.prefs.borrow().timestamps, line, (conversation, busy, stalled))
    }

    /// Puts the live rows where the turn happened (`Presented::anchors`):
    /// after its prompt, before any row written after it began, below every
    /// row written before an entry began, and before unsent messages of
    /// one's own. A durable row that took over a message shows in that
    /// entry's place under the entry's key, so it is updated there, not
    /// inserted, and keeps that key while the chat is shown.
    fn splice_live(&self, pane: &mut PaneState, live: &clarp_core::live_present::Presented, fresh: &mut Vec<Shown>, rows: &mut Vec<MessageRow>) {
        let mut placed: std::collections::HashMap<String, (Shown, MessageRow)> = std::collections::HashMap::new();
        for entry in live.entries.iter().filter(|e| !e.row.is_empty()) {
            if let Some(index) = fresh.iter().position(|(row, ..)| row.message.id == entry.row) {
                placed.insert(entry.row.clone(), (fresh.remove(index), rows.remove(index)));
            }
        }
        // Where the turn happened: after its prompt and what came before
        // it, before anything written once it began; unsent messages last.
        let messages: Vec<&clarp_core::protocol::Message> = fresh.iter().map(|(row, ..)| &row.message).collect();
        let sent = fresh.iter().rposition(|(row, ..)| !(row.message.pending || row.message.delivery_failed)).map_or(0, |i| i + 1);
        // Each entry at its own place: a turn the Host never settled splits
        // at the newer prompts its later items started after.
        let anchors: Vec<usize> = live.anchors(&messages).into_iter().map(|at| at.min(sent)).collect();
        let built: Vec<(Shown, MessageRow)> = live
            .entries
            .iter()
            .map(|entry| {
                if let Some((mut shown, row)) = placed.remove(&entry.row) {
                    pane.slots.insert(entry.row.clone(), entry.key.clone());
                    shown.3 = entry.key.clone();
                    return (shown, row);
                }
                (live_source(entry), pane.rows.live_row(entry))
            })
            .collect();
        // A durable row that held an entry's place keeps it once the turn
        // is gone from the live state, so it never moves.
        for shown in fresh.iter_mut() {
            if let Some(slot) = pane.slots.get(&shown.0.message.id) {
                shown.3 = slot.clone();
            }
        }
        // Last first, so the earlier places still hold; equal places keep
        // the entries' order.
        for ((source, row), at) in built.into_iter().zip(anchors).rev() {
            let at = at.min(fresh.len());
            fresh.insert(at, source);
            rows.insert(at, row);
        }
    }

    /// The quiet line where the Host left coordination between agents out
    /// (`hidden_coordination`, #653): "3 messages between agents" above the
    /// row that follows a run (`hidden_before`), and after the newest row
    /// for a run at the end (`hidden_after`). Opened, the chat is read again
    /// with every run shown for the visit. None when this device turned
    /// the line off, nor while the visit shows them.
    fn splice_coordination(&self, pane: &mut PaneState, fresh: &mut Vec<Shown>, rows: &mut Vec<MessageRow>, kept: &mut std::collections::HashSet<String>) {
        let after = {
            let engine = self.engine.borrow();
            if !engine.chat_hidden_coordination(&pane.session) || engine.coordination_visit(&pane.session) || !engine.settings().boolean(crate::catalogue::COORDINATION_LINE, true) {
                return;
            }
            engine.conversation(&pane.session).and_then(|c| c.hidden_after().cloned())
        };
        let line = |anchor: &str, count: &clarp_core::json::Object| clarp_core::live_present::Entry {
            key: format!("{}{anchor}", crate::artifacts_view::COORDINATION_PREFIX),
            kind: clarp_core::live_present::Kind::Fold,
            status: "completed".into(),
            title: clarp_core::coordination::line(&clarp_core::coordination::Count::from_json(count)),
            expandable: true,
            ..Default::default()
        };
        let mut at = 0;
        while at < fresh.len() {
            if let Some(count) = fresh[at].0.message.hidden_before.clone() {
                let entry = line(&fresh[at].0.message.id, &count);
                kept.insert(entry.key.clone());
                rows.insert(at, pane.rows.live_row(&entry));
                fresh.insert(at, live_source(&entry));
                at += 1;
            }
            at += 1;
        }
        if let Some(count) = after {
            // After the newest row the Host sent, before unsent ones.
            let end = fresh.iter().rposition(|(row, ..)| !(row.message.pending || row.message.delivery_failed)).map_or(0, |i| i + 1);
            let entry = line("end", &count);
            kept.insert(entry.key.clone());
            rows.insert(end, pane.rows.live_row(&entry));
            fresh.insert(end, live_source(&entry));
        }
    }

    /// Puts each finished turn's fold where the turn is: its entries after
    /// its prompt, among the rows it keeps (every message, commentary
    /// too), in the turn's order. `kept` gathers the entries' keys.
    fn splice_history(
        &self,
        pane: &mut PaneState,
        history: &clarp_core::history_fold::Folded,
        fresh: &mut Vec<Shown>,
        rows: &mut Vec<MessageRow>,
        kept: &mut std::collections::HashSet<String>,
    ) {
        use clarp_core::history_fold::Piece;
        for fold in &history.folds {
            let position = |fresh: &Vec<Shown>, id: &str| fresh.iter().position(|(row, ..)| row.message.id == id);
            // The turn's rows in its order, in the places they hold.
            if let Some((places, sources)) = clarp_core::history_fold::turn_order(fold, |id| position(&*fresh, id)) {
                let mut taken: Vec<(usize, (Shown, MessageRow))> = Vec::new();
                let mut by_index: Vec<usize> = sources.clone();
                by_index.sort_unstable_by(|a, b| b.cmp(a));
                for index in by_index {
                    taken.push((index, (fresh.remove(index), rows.remove(index))));
                }
                for (place, source) in places.into_iter().zip(sources) {
                    let at = taken.iter().position(|(index, _)| *index == source).expect("a taken row");
                    let (_, (shown, row)) = taken.swap_remove(at);
                    fresh.insert(place, shown);
                    rows.insert(place, row);
                }
            }
            let first_row = fold.pieces.iter().find_map(|p| if let Piece::Row(id) = p { position(fresh, id) } else { None });
            let mut at = fold
                .prompt
                .as_deref()
                .and_then(|id| position(fresh, id))
                .map(|i| i + 1)
                .or(first_row)
                .or_else(|| fold.next.as_deref().and_then(|id| position(fresh, id)))
                .unwrap_or(fresh.len());
            for piece in &fold.pieces {
                match piece {
                    Piece::Row(id) => {
                        if let Some(index) = position(fresh, id) {
                            at = index + 1;
                        }
                    }
                    Piece::Entry(entry) => {
                        kept.insert(entry.key.clone());
                        fresh.insert(at, live_source(entry));
                        rows.insert(at, pane.rows.live_row(entry));
                        at += 1;
                    }
                }
            }
        }
    }

    /// The half-second clock: live rows whose text changed (elapsed times)
    /// and the status lines, without presenting the chats again.
    pub fn tick_live(&self) {
        let mut running = false;
        let mut panes = self.pane_state.borrow_mut();
        for index in 0..panes.len() {
            let pane = &mut panes[index];
            let session = pane.session.clone();
            let engine = self.engine.borrow();
            running |= crate::live_view::ticking(&engine, &session);
            let (status, busy, key) = crate::live_view::status(&engine, &session);
            let rows = engine.conversation(&session).map(|c| c.rows().to_vec()).unwrap_or_default();
            if let Some(live) = crate::live_view::present(&engine, &session, &rows, &self.expanded.borrow()) {
                // A durable row in an entry's place is the chat's, not the clock's.
                for entry in live.entries.iter().filter(|e| e.row.is_empty()) {
                    let Some(at) = pane.shown.iter().position(|s| s.3 == entry.key) else { continue };
                    // A row keeps its band of a work box (#727) as it ticks.
                    let edge = pane.messages.row_data(at).map(|r| r.work_box).unwrap_or_default();
                    let signature = boxed_signature(&crate::live_view::signature(entry), &edge);
                    if pane.shown[at].2 == signature {
                        continue;
                    }
                    pane.shown[at].2 = signature;
                    // A running time ticking is not news.
                    if at + 1 == pane.shown.len() {
                        pane.tail = Some((entry.key.clone(), pane.shown[at].2.clone()));
                    }
                    let mut row = pane.rows.live_row(entry);
                    row.work_box = edge;
                    pane.messages.set_row_data(at, row);
                    crate::scroll_journal::event(&pane.id, "live");
                    crate::scroll_book::measure_soon();
                }
            }
            drop(engine);
            if pane.view.live_status != status.as_str() || pane.view.live_busy != busy {
                pane.view.live_status = status.into();
                pane.view.live_busy = busy;
                pane.view.live_stop_key = key.into();
                let view = self.pane_view(pane);
                pane.view = view.clone();
                self.panes.set_row_data(index, view);
            }
        }
        drop(panes);
        crate::live_view::keep_ticking(running);
    }

    /// Brings the panes up to date with the engine after `changes`.
    pub fn refresh_panes(&self, window: &AppWindow, changes: &[Change]) {
        let layout_changed = changes.iter().any(|c| matches!(c, Change::Panes | Change::Selection));
        let mut rebound = Vec::new();
        if layout_changed || self.pane_state.borrow().is_empty() {
            let (layout, active) = {
                let engine = self.engine.borrow();
                (engine.panes().view_layout(), engine.panes().active_pane_id().to_owned())
            };
            let mut old = std::mem::take(&mut *self.pane_state.borrow_mut());
            let mut next = Vec::new();
            for row in &layout {
                let id = text(row, "id");
                let mut pane = match old.iter().position(|p| p.id == id) {
                    Some(index) => old.remove(index),
                    None => PaneState::new(&id),
                };
                let session = text(row, "session");
                if pane.session != session {
                    // Another chat: its rows, its draft, from its latest message.
                    pane.session = session.clone();
                    crate::scroll_journal::reset(&pane.id, "chat", pane.messages.row_count());
                    pane.shown.clear();
                    pane.tail = None;
                    pane.view.news = false;
                    pane.rows.clear();
                    pane.presented_with = None;
                    pane.cards.clear();
                            pane.slots.clear();
                    pane.messages.set_vec(Vec::new());
                    pane.book.clear();
                    pane.draft = self.engine.borrow().draft(&session);
                    pane.draft_set += 1;
                    pane.draft_cursor = 0;
                    pane.mention = None;
                    pane.mention_dismissed = None;
                    pane.to_latest += 1;
                    rebound.push(id.clone());
                }
                let view = &mut pane.view;
                view.session = session.into();
                view.x = number(row, "x");
                view.y = number(row, "y");
                view.width = number(row, "width");
                view.height = number(row, "height");
                view.shown = row.get("shown").and_then(serde_json::Value::as_bool).unwrap_or(false);
                view.active = id == active;
                next.push(pane);
            }
            *self.pane_state.borrow_mut() = next;
            let engine = self.engine.borrow();
            let splits: Vec<SplitView> = engine
                .panes()
                .split_layout()
                .iter()
                .map(|s| SplitView {
                    id: text(s, "id").into(),
                    vertical: text(s, "direction") == "vertical",
                    x: number(s, "x"),
                    y: number(s, "y"),
                    width: number(s, "width"),
                    height: number(s, "height"),
                    ratio: number(s, "ratio"),
                })
                .collect();
            window.set_splits(ModelRc::new(VecModel::from(splits)));
            let active_workspace = engine.panes().active_workspace().to_owned();
            let tabs: Vec<WorkspaceTab> = engine
                .panes()
                .workspaces()
                .iter()
                .map(|w| WorkspaceTab { id: text(w, "id").into(), name: text(w, "name").into(), active: text(w, "id") == active_workspace })
                .collect();
            window.set_workspace_bar(self.prefs.borrow().workspace_bar);
            window.set_workspaces(ModelRc::new(VecModel::from(tabs)));
            window.set_save_warning(engine.panes().workspace_save_warning().into());
        }
        let everything = changes.iter().any(|c| matches!(c, Change::Preferences | Change::Roster | Change::Selection | Change::Panes | Change::Profile));
        let mut panes = std::mem::take(&mut *self.pane_state.borrow_mut());
        for pane in &mut panes {
            let session = pane.session.clone();
            let presentation = self.presentation_key(&session);
            let fresh = rebound.contains(&pane.id);
            // A preference re-presents a transcript only when one its rows
            // depend on changed (the theme, voice and the rest do not).
            // The roster brings the Host's chat view (#702).
            let preference = changes.iter().any(|c| matches!(c, Change::Preferences | Change::Roster)) && pane.presented_with != Some(presentation);
            let conversation = fresh
                || preference
                || changes.iter().any(|c| matches!(c, Change::Narrator | Change::Updates) || matches!(c, Change::Conversation(s) | Change::Live(s) if *s == session));
            if conversation {
                self.messages(pane);
            }
            // A Stop's line also follows the rows: the stopped turn's own
            // end line, arriving after the Stop's answer, takes its place.
            let composer = fresh
                || everything
                || changes.iter().any(|c| matches!(c, Change::Composer(s) | Change::Live(s) | Change::Stopped(s) | Change::Queue(s) if *s == session))
                || (!pane.view.stop_notice.is_empty() && changes.iter().any(|c| matches!(c, Change::Conversation(s) if *s == session)));
            // A portrait made since: the header's avatar takes it.
            if !(composer || everything) && changes.iter().any(|c| matches!(c, Change::Avatars)) {
                pane.view.portrait = self.header_portrait(&session);
            }
            if composer || everything {
                let engine = self.engine.borrow();
                let mut view = pane.view.clone();
                self.header(&engine, &mut view, &session);
                Self::composer(&engine, &mut view, &session);
                drop(engine);
                view.portrait = self.header_portrait(&session);
                pane.view = view;
            }
            if composer || everything || changes.contains(&Change::HostDown) { crate::host_down_view::pane(&self.engine.borrow(), &mut pane.view, &session); }
            pane.view = self.pane_view(pane);
        }
        let running = panes.iter().any(|p| crate::live_view::ticking(&self.engine.borrow(), &p.session));
        crate::live_view::keep_ticking(running);
        let views: Vec<PaneView> = panes.iter().map(|p| p.view.clone()).collect();
        *self.pane_state.borrow_mut() = panes;
        // Panes that stay keep their elements (and the keyboard): drop the
        // closed ones, insert new ones where the layout puts them.
        let mut index = 0;
        while index < self.panes.row_count() {
            let id = self.panes.row_data(index).map(|v| v.id.clone()).unwrap_or_default();
            if views.iter().any(|v| v.id == id) {
                index += 1;
            } else {
                self.panes.remove(index);
            }
        }
        for (index, view) in views.into_iter().enumerate() {
            match self.panes.row_data(index) {
                Some(current) if current.id == view.id => {
                    if current != view {
                        self.panes.set_row_data(index, view);
                    }
                }
                _ => {
                    // Kept panes' order never changes, so the new pane goes here.
                    self.panes.insert(index, view);
                }
            }
        }
    }
}
