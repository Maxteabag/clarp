//! `@agent` in the composer: typing `@` lists the agents that match what
//! follows it (fuzzy, best first); Up/Down choose, Tab or Enter complete the
//! name, Escape closes the list. A draft naming another agent goes to that
//! agent's chat: the Host's way to put a message in an agent's chat is a
//! `/send` to its session (the agent-to-agent path speaks as an agent, not
//! as the user). The route shows before the send (a chip, "→ Mike") and on
//! the sent row in that chat.

use clarp_core::mention::{self, Active, Candidate};
use clarp_engine::Engine;
use slint::{ModelRc, SharedString, VecModel};

use crate::panes::PaneState;
use crate::{App, MentionRow, PaneView};

/// How many agents the list shows.
const SHOWN: usize = 8;

/// An open mention list: the `@` token, the agents that match it, the
/// chosen row.
#[derive(Debug, Clone)]
pub struct Open {
    pub mention: Active,
    pub candidates: Vec<Candidate>,
    pub current: i32,
}

/// The agents a draft may name: the roster's, by name.
pub fn candidates(engine: &Engine) -> Vec<Candidate> {
    engine
        .roster()
        .agents()
        .iter()
        .filter(|a| !a.janitor)
        .map(|a| Candidate { session: a.session.clone(), name: clarp_core::protocol::display_name(a).to_owned(), activity: a.last_activity })
        .filter(|c| !c.name.is_empty())
        .collect()
}

/// The agent `text` sends to from `session`'s composer, when it names
/// another one.
pub fn route(candidates: &[Candidate], session: &str, text: &str) -> Option<Candidate> {
    mention::target(text, candidates).filter(|c| c.session != session).cloned()
}

/// The sent row's note in `session`'s chat: "→ name" for a message whose
/// first mention names this chat's own agent.
pub fn sent_to(candidates: &[Candidate], session: &str, text: &str) -> String {
    mention::target(text, candidates).filter(|c| c.session == session).map(|c| format!("→ {}", c.name)).unwrap_or_default()
}

/// What a pane's composer shows of its mention: the list and the route.
pub fn apply(app: &App, pane: &PaneState, view: &mut PaneView) {
    let rows: Vec<MentionRow> = pane
        .mention
        .iter()
        .flat_map(|open| open.candidates.iter())
        .map(|c| MentionRow { session: c.session.clone().into(), name: c.name.clone().into(), detail: c.session.clone().into() })
        .collect();
    view.mentions = ModelRc::new(VecModel::from(rows));
    view.mention_current = pane.mention.as_ref().map_or(0, |o| o.current);
    // While the engine is busy elsewhere the route is the one shown.
    if let Ok(engine) = app.engine.try_borrow() {
        view.route = route(&candidates(&engine), &pane.session, &pane.draft).map(|c| SharedString::from(c.name)).unwrap_or_default();
    }
}

/// The draft or its cursor moved: opens, narrows or closes the list (the
/// pane is drawn again only when the list or the route changed).
pub fn scan(app: &App, pane: &str, text: &str, cursor: i32) {
    let all = candidates(&app.engine.borrow());
    let active = usize::try_from(cursor).ok().and_then(|at| mention::active(text, at));
    app.change_pane(pane, |pane| {
        let shown = |open: &Option<Open>| open.as_ref().map(|o| (o.candidates.iter().map(|c| c.session.clone()).collect::<Vec<_>>(), o.current));
        let before = (shown(&pane.mention), route(&all, &pane.session, &pane.draft));
        pane.draft = text.to_owned();
        match active {
            None => {
                pane.mention = None;
                pane.mention_dismissed = None;
            }
            Some(active) if pane.mention_dismissed == Some(active.start) => pane.mention = None,
            Some(active) => {
                pane.mention_dismissed = None;
                let ranked = mention::rank(&all, &active.query, SHOWN);
                // The chosen agent stays chosen while the list narrows.
                let chosen = pane.mention.as_ref().and_then(|o| o.candidates.get(o.current as usize)).map(|c| c.session.clone());
                let current = chosen.and_then(|s| ranked.iter().position(|c| c.session == s)).unwrap_or(0) as i32;
                pane.mention = (!ranked.is_empty()).then_some(Open { mention: active, candidates: ranked, current });
            }
        }
        before != (shown(&pane.mention), route(&all, &pane.session, &pane.draft))
    });
}

pub fn moved(app: &App, pane: &str, index: i32) {
    app.change_pane(pane, |pane| {
        let Some(open) = pane.mention.as_mut() else { return false };
        open.current = index.clamp(0, open.candidates.len() as i32 - 1);
        true
    });
}

/// Completes the mention with row `index`'s name.
pub fn accept(app: &App, pane: &str, index: i32) {
    let Some((open, draft)) = app.with_pane(pane, |pane| (pane.mention.take(), pane.draft.clone())).and_then(|(o, d)| Some((o?, d))) else { return };
    let Some(chosen) = usize::try_from(index).ok().and_then(|i| open.candidates.get(i)) else { return };
    let (text, cursor) = mention::complete(&draft, &open.mention, &chosen.name);
    app.set_draft_at(pane, &text, cursor);
}

pub fn dismiss(app: &App, pane: &str) {
    app.change_pane(pane, |pane| {
        pane.mention_dismissed = pane.mention.take().map(|o| o.mention.start);
        true
    });
}

/// The active pane's mention list shows (Escape closes it first).
pub fn is_open(app: &App) -> bool {
    app.active_view().is_some_and(|v| slint::Model::row_count(&v.mentions) > 0)
}

pub fn dismiss_active(app: &App) {
    let id = app.active_id();
    dismiss(app, &id);
}
