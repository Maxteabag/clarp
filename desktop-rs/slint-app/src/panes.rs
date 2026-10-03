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
    /// The presentation settings the rows were last presented with; a
    /// preference change re-presents only when these moved.
    presented_with: Option<(i32, bool, bool)>,
    pub report: Report,
    draft: String,
    draft_set: i32,
    focus_composer: i32,
    focus_transcript: i32,
    to_latest: i32,
    scroll_request: i32,
    scroll_amount: f32,
    view: PaneView,
    /// Each reply's artifact cards, updated in place: a card is never
    /// rebuilt under a click or a field that has the keyboard.
    cards: std::collections::HashMap<String, Rc<VecModel<crate::ArtifactItem>>>,
    /// Live rows last shown: key → the items each stood for.
    live_items: std::collections::HashMap<String, Vec<String>>,
    /// Durable rows that took over live rows: row id → the place (key)
    /// they took, kept while the chat is shown so the row never moves.
    slots: std::collections::HashMap<String, String>,
}

impl PaneState {
    fn new(id: &str) -> Self {
        let messages = Rc::new(VecModel::default());
        Self {
            id: id.to_owned(),
            session: String::new(),
            view: PaneView { id: id.into(), messages: ModelRc::from(messages.clone()), ..PaneView::default() },
            messages,
            shown: Vec::new(),
            rows: RowCache::default(),
            presented_with: None,
            report: Report { follows: true, at_end: true, ..Report::default() },
            draft: String::new(),
            draft_set: 0,
            focus_composer: 0,
            focus_transcript: 0,
            to_latest: 0,
            scroll_request: 0,
            scroll_amount: 0.0,
            cards: std::collections::HashMap::new(),
            live_items: std::collections::HashMap::new(),
            slots: std::collections::HashMap::new(),
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

impl App {
    /// Index of the active pane in `pane_state`.
    pub fn active_index(&self) -> Option<usize> {
        let engine = self.engine.borrow();
        let active = engine.panes().active_pane_id();
        self.pane_state.borrow().iter().position(|p| p.id == active)
    }

    pub fn active_report(&self) -> Report {
        self.active_index().map(|i| self.pane_state.borrow()[i].report).unwrap_or_default()
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
            pane.live_items.clear();
            pane.slots.clear();
            pane.messages.set_vec(Vec::new());
            pane.to_latest += 1;
        }
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
    /// Scrolls the active transcript by `delta` px (down for positive).
    pub fn scroll_by(&self, delta: f32) {
        self.bump(|p| {
            p.scroll_request += 1;
            p.scroll_amount = delta;
        });
    }

    /// A pane's report, from its transcript and composer.
    pub fn reported(&self, id: &str, report: Report) {
        if let Some(pane) = self.pane_state.borrow_mut().iter_mut().find(|p| p.id == id) {
            pane.report = report;
        }
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

    /// Puts `text` in every composer on `session` (but `except`).
    pub fn replace_draft(&self, session: &str, text: &str, except: Option<&str>) {
        let mut panes = self.pane_state.borrow_mut();
        for index in 0..panes.len() {
            let pane = &mut panes[index];
            if pane.session != session || Some(pane.id.as_str()) == except || pane.draft == text {
                continue;
            }
            pane.draft = text.to_owned();
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
        view
    }

    fn header(&self, engine: &Engine, view: &mut PaneView, session: &str) {
        let agent = engine.roster().find(session);
        view.name = if session.is_empty() { SharedString::new() } else { engine.chat_name(session).into() };
        view.pair = session.starts_with("pair:");
        view.model = agent.map(|a| a.model.clone()).unwrap_or_default().into();
        view.effort = agent.map(|a| a.effort.clone()).unwrap_or_default().into();
        view.status = agent.map(|a| a.status_text.clone()).unwrap_or_default().into();
        view.default_effort = agent.map(|a| engine.default_effort_for_model(&a.backend, &a.model)).unwrap_or_default().into();
        view.busy = agent.is_some_and(|a| a.busy);
        view.working = crate::cells_view::working(engine, session);
        let (status, busy, key) = crate::live_view::status(engine, session);
        if engine.live_view(session).is_some() {
            // The status line stands in for the typing dots.
            view.working = false;
        }
        view.live_status = status.into();
        view.live_busy = busy;
        view.live_stop_key = key.into();
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
        view.queued = engine.queue_count(session);
        view.quota_notice = engine.quota_notice(session).into();
    }

    fn messages(&self, pane: &mut PaneState) {
        crate::artifacts_view::settle_downloads(self, &pane.session);
        let mut presented = self.engine.borrow_mut().presented(&pane.session);
        // Live items (§7): the open turn's rows, over the /log rows. A live
        // /log row an item shows is not shown twice.
        let live = {
            let engine = self.engine.borrow();
            let rows = engine.conversation(&pane.session).map(|c| c.rows().to_vec()).unwrap_or_default();
            crate::live_view::present(&engine, &pane.session, &rows, &self.expanded.borrow())
        };
        if let Some(live) = &live {
            presented.retain(|row| !live.hidden_rows.contains(&row.message.id));
        }
        let always = self.engine.borrow().activity_mode() == clarp_core::presentation::ALWAYS_VISIBLE;
        let expanded = self.expanded.borrow();
        let stamps = self.prefs.borrow().timestamps;
        let artifacts = crate::cells_view::artifacts_by_row(&presented, &self.engine.borrow().artifacts_for_session(&pane.session));
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
        pane.presented_with = Some(self.presentation_key());
        let built = pane.rows.rows(&presented, always, &expanded);
        let mut kept = std::collections::HashMap::new();
        let rows: Vec<MessageRow> = presented
            .iter()
            .zip(&artifacts)
            .zip(built)
            .map(|((row, artifacts), mut shown)| {
                if !stamps {
                    shown.stamp = SharedString::new();
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
        // Plain-English tools: the latest open rows' calls, explained.
        let narrating = engine.narrator_enabled() && !engine.narrator_unavailable();
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
            let pictures = if row.blocks.iter().any(|b| b.kind == "images") { crate::artifacts_view::pictures_landed() } else { 0 };
            format!("{cards}|{}|{pictures}", explained.join(","))
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
        if let Some(live) = live {
            self.splice_live(pane, &live, &mut fresh, &mut rows);
        }
        sync_rows(&pane.messages, &mut pane.shown, fresh, rows);
    }

    /// What a transcript's rows depend on among the preferences.
    fn presentation_key(&self) -> (i32, bool, bool) {
        let engine = self.engine.borrow();
        (engine.activity_mode(), engine.show_when_ready(), self.prefs.borrow().timestamps)
    }

    /// Puts the live rows after the chat's rows (before unsent messages of
    /// one's own), and gives a durable row that took over live rows the
    /// place of the first of them, so it is updated there, not inserted.
    fn splice_live(&self, pane: &mut PaneState, live: &clarp_core::live_present::Presented, fresh: &mut Vec<Shown>, rows: &mut Vec<MessageRow>) {
        let at = fresh.iter().rposition(|(row, ..)| !(row.message.pending || row.message.delivery_failed)).map_or(0, |i| i + 1);
        let built: Vec<(Shown, MessageRow)> = live
            .entries
            .iter()
            .map(|entry| {
                let row = pane.rows.live_row(entry);
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
                ((source, entry.expanded, crate::live_view::signature(entry), entry.key.clone()), row)
            })
            .collect();
        // Durable rows new since the last refresh that took over live rows
        // shown then: the first such row's place is theirs.
        let shown_before: Vec<String> = pane.shown.iter().map(|s| s.3.clone()).collect();
        let still: std::collections::HashSet<&str> = live.entries.iter().map(|e| e.key.as_str()).collect();
        for shown in fresh.iter_mut() {
            let id = shown.0.message.id.clone();
            if let Some(slot) = pane.slots.get(&id) {
                shown.3 = slot.clone();
                continue;
            }
            if shown_before.contains(&id) {
                continue;
            }
            let taken: Vec<&String> = live.taken_over.iter().filter(|(_, row)| **row == id).map(|(item, _)| item).collect();
            if taken.is_empty() {
                continue;
            }
            let place = shown_before.iter().find(|key| {
                !still.contains(key.as_str()) && !pane.slots.values().any(|s| s == *key) && pane.live_items.get(*key).is_some_and(|items| items.iter().any(|i| taken.contains(&i)))
            });
            if let Some(place) = place {
                pane.slots.insert(id, place.clone());
                shown.3 = place.clone();
            }
        }
        pane.live_items = live.entries.iter().map(|e| (e.key.clone(), e.items.clone())).collect();
        pane.rows.keep_live(&still);
        let at = at.min(fresh.len());
        let (sources, live_rows): (Vec<Shown>, Vec<MessageRow>) = built.into_iter().unzip();
        fresh.splice(at..at, sources);
        rows.splice(at..at, live_rows);
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
                for entry in &live.entries {
                    let Some(at) = pane.shown.iter().position(|s| s.3 == entry.key) else { continue };
                    let signature = crate::live_view::signature(entry);
                    if pane.shown[at].2 == signature {
                        continue;
                    }
                    pane.shown[at].2 = signature;
                    let row = pane.rows.live_row(entry);
                    pane.messages.set_row_data(at, row);
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
                    pane.rows.clear();
                    pane.presented_with = None;
                    pane.cards.clear();
                    pane.live_items.clear();
                    pane.slots.clear();
                    pane.messages.set_vec(Vec::new());
                    pane.draft = self.engine.borrow().draft(&session);
                    pane.draft_set += 1;
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
        let everything = changes.iter().any(|c| matches!(c, Change::Preferences | Change::Roster | Change::Selection | Change::Panes));
        let presentation = self.presentation_key();
        let mut panes = std::mem::take(&mut *self.pane_state.borrow_mut());
        for pane in &mut panes {
            let session = pane.session.clone();
            let fresh = rebound.contains(&pane.id);
            // A preference re-presents a transcript only when one its rows
            // depend on changed (the theme, voice and the rest do not).
            let preference = changes.contains(&Change::Preferences) && pane.presented_with != Some(presentation);
            let conversation = fresh
                || preference
                || changes.iter().any(|c| matches!(c, Change::Narrator | Change::Updates) || matches!(c, Change::Conversation(s) | Change::Live(s) if *s == session));
            if conversation {
                self.messages(pane);
            }
            let composer = fresh || everything || changes.iter().any(|c| matches!(c, Change::Composer(s) | Change::Live(s) if *s == session));
            if composer || everything {
                let engine = self.engine.borrow();
                let mut view = pane.view.clone();
                self.header(&engine, &mut view, &session);
                Self::composer(&engine, &mut view, &session);
                pane.view = view;
            }
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
