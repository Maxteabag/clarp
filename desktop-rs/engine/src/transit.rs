//! Moving an agent to another backend from its profile (Rust Host feature
//! `agent_backend_transit`, contract 158): the options the Host offers for
//! one agent (`GET /agent-model-options?session=S`), the start (`POST
//! /agent-transit`, idempotent by its request id), its progress (the
//! `agent-transit` event; after a reconnect `GET /agent-transit`) and the
//! running move a snapshot row names. The Host writes the chat's result
//! row; nothing here adds one, and the chat is never cleared: its
//! `conversation_id` stays. Without the feature nothing here runs.

use std::collections::HashMap;

use clarp_core::json::Object;
use clarp_core::transit::{self, Backend, Event, Flow};
use serde_json::Value;

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct Transits {
    /// `agent_backend_transit` was in the last `/server-info`.
    pub feature: bool,
    /// `agent_backend_transit_tools` too: a move carries the tools.
    pub tools: bool,
    /// Each agent's flow, by session (idle when absent).
    flows: HashMap<String, Flow>,
    /// The options read for an agent's picker, by session.
    options: HashMap<String, Options>,
}

#[derive(Default, Clone)]
pub struct Options {
    pub loading: bool,
    pub backends: Vec<Backend>,
    pub error: String,
}

impl Options {
    /// Legacy rows can still label a current agent or an in-flight move.
    pub fn choices(&self) -> impl Iterator<Item = &Backend> {
        self.backends.iter().filter(|b| b.id != clarp_core::catalog::LEGACY_ANTIGRAVITY)
    }
}

const OPTIONS: &str = "transit-options:";
const START: &str = "transit-start:";
const STATUS: &str = "transit-status:";

impl Engine {
    /// The chat's own Host can move its agent to another backend.
    pub fn transit_offered(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_offered(local));
        self.transits.feature
    }

    /// The chat's own Host carries the tools along in a move.
    pub fn transit_carries_tools(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_carries_tools(local));
        self.transits.tools
    }

    /// The agent's flow (idle when none).
    pub fn transit_flow(&self, session: &str) -> Flow {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_flow(local));
        self.transits.flows.get(session).cloned().unwrap_or_default()
    }

    /// The picker's options for the agent.
    pub fn transit_options(&self, session: &str) -> Options {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_options(local));
        self.transits.options.get(session).cloned().unwrap_or_default()
    }

    /// A move may not start: a turn runs or messages wait.
    pub fn transit_blocked(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_blocked(local));
        self.roster.find(session).is_some_and(|a| a.busy || a.queued_turn_count > 0)
    }

    fn flow_mut(&mut self, session: &str) -> &mut Flow {
        self.transits.flows.entry(session.to_owned()).or_default()
    }

    /// The Backend row was chosen: the picker opens and the Host's options
    /// for this agent are read again (sign-ins and limits change).
    pub fn open_transit(&mut self, session: &str) -> bool {
        on_peer!(self.peer_mut(session), |engine, local| engine.open_transit(local));
        if !self.transits.feature || session.is_empty() {
            return false;
        }
        let blocked = self.transit_blocked(session);
        if !self.flow_mut(session).open(blocked) {
            return false;
        }
        let options = self.transits.options.entry(session.to_owned()).or_default();
        options.loading = true;
        options.error.clear();
        self.api.get(&format!("{OPTIONS}{session}"), "/agent-model-options", &[("session", session)]);
        self.changes.push(Change::Profile);
        true
    }

    /// A backend in the picker (an unavailable one does nothing).
    pub fn pick_transit_backend(&mut self, session: &str, backend: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.pick_transit_backend(local, backend));
        let Some(chosen) = self.transits.options.get(session).and_then(|o| o.choices().find(|b| b.id == backend)).cloned() else { return };
        self.flow_mut(session).pick_backend(&chosen);
        self.changes.push(Change::Profile);
    }

    /// A model of the chosen backend: the confirmation follows.
    pub fn pick_transit_model(&mut self, session: &str, model: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.pick_transit_model(local, model));
        self.flow_mut(session).pick_model(model);
        self.changes.push(Change::Profile);
    }

    /// Escape in the picker: one level back.
    pub fn transit_back(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_back(local));
        self.flow_mut(session).back();
        self.changes.push(Change::Profile);
    }

    /// Closes the picker, or clears a finished result.
    pub fn reset_transit(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.reset_transit(local));
        if let Some(flow) = self.transits.flows.get_mut(session) {
            flow.reset();
            self.changes.push(Change::Profile);
        }
    }

    /// Move: `POST /agent-transit`; a retry of the same start keeps its
    /// request id, so the Host answers the same transit.
    pub fn confirm_transit(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.confirm_transit(local));
        let Some((backend, model, request_id)) = self.flow_mut(session).confirm(|| uuid::Uuid::new_v4().to_string()) else { return };
        let body = transit::start_body(session, &backend, &model, &request_id);
        self.api.post_json(&format!("{START}{session}"), "/agent-transit", body, None);
        self.changes.push(Change::Profile);
    }

    /// `/server-info` answered.
    pub(crate) fn transit_server_info(&mut self, info: &Object) {
        let has = transit::offered_by(info);
        if has != self.transits.feature {
            self.transits.feature = has;
            self.changes.push(Change::Profile);
        }
        let tools = transit::tools_offered_by(info);
        if tools != self.transits.tools {
            self.transits.tools = tools;
            self.changes.push(Change::Profile);
        }
    }

    /// SSE `agent-transit`: a phase of one of this Host's agents' moves.
    pub(crate) fn transit_event(&mut self, event: &Object) {
        let Some(progress) = Event::parse(event) else { return };
        let session = if self.roster.find(&progress.session).is_some() || progress.agent_id.is_empty() {
            progress.session.clone()
        } else {
            self.roster.find_by_agent_id(&progress.agent_id).map_or(progress.session.clone(), |a| a.session.clone())
        };
        if session.is_empty() {
            return;
        }
        self.flow_mut(&session).apply(&progress);
        self.changes.push(Change::Profile);
    }

    /// The snapshot was read: a move a row names is shown (also one another
    /// app started); a held one the row no longer names is asked for by id.
    pub(crate) fn transit_snapshot(&mut self) {
        if !self.transits.feature {
            return;
        }
        let running: Vec<(String, Option<Event>)> = self
            .roster
            .agents()
            .iter()
            .map(|a| (a.session.clone(), a.transit.as_ref().and_then(|t| {
                let mut fields = t.clone();
                fields.insert("session".into(), Value::String(a.session.clone()));
                Event::parse(&fields)
            })))
            .collect();
        let mut changed = false;
        for (session, event) in running {
            match event {
                Some(event) => {
                    let flow = self.flow_mut(&session);
                    let before = flow.clone();
                    flow.adopt(&event);
                    changed |= *flow != before;
                }
                None => {
                    let held = self.transits.flows.get(&session).and_then(|f| f.transit_id().map(str::to_owned));
                    if let Some(id) = held {
                        self.api.get(&format!("{STATUS}{session}"), "/agent-transit", &[("session", &session), ("transit_id", &id)]);
                    }
                }
            }
        }
        if changed {
            self.changes.push(Change::Profile);
        }
    }

    /// Replies for the transit tags; true when it was one.
    pub(crate) fn transit_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(session) = tag.strip_prefix(OPTIONS) {
            let mut backends = transit::options(object, &chrono::Local::now());
            if !self.antigravity_acp_offered() {
                backends.retain(|backend| backend.id != clarp_core::catalog::ANTIGRAVITY_ACP);
            }
            let options = self.transits.options.entry(session.to_owned()).or_default();
            options.loading = false;
            options.error.clear();
            options.backends = backends;
        } else if let Some(session) = tag.strip_prefix(START) {
            let id = clarp_core::json::string(object, "transit_id");
            self.flow_mut(session).started(&id);
        } else if let Some(session) = tag.strip_prefix(STATUS) {
            if let Some(event) = Event::parse(object) {
                self.flow_mut(session).apply(&event);
            }
        } else {
            return false;
        }
        self.changes.push(Change::Profile);
        true
    }

    /// A refused or failed request; true when it was one of these.
    pub(crate) fn transit_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if let Some(session) = tag.strip_prefix(OPTIONS) {
            let options = self.transits.options.entry(session.to_owned()).or_default();
            options.loading = false;
            options.error = transit::OPTIONS_FAILED.to_owned();
        } else if let Some(session) = tag.strip_prefix(START) {
            let backend = match self.transits.flows.get(session).map(|f| &f.step) {
                Some(transit::Step::Moving { backend, .. }) => backend.clone(),
                _ => String::new(),
            };
            // backend_unavailable's reason is the one the picker showed.
            let unavailable = self
                .transits
                .options
                .get(session)
                .and_then(|o| o.backends.iter().find(|b| b.id == backend))
                .map(|b| b.reason.clone())
                .unwrap_or_default();
            let reason = transit::refusal(status, &transit::refusal_code(message), &unavailable);
            self.flow_mut(session).refused(&backend, &reason);
        } else if let Some(session) = tag.strip_prefix(STATUS) {
            // The Host forgot it (a restart): no move runs any more.
            if status == 404 {
                self.flow_mut(session).forgotten();
            }
        } else {
            return false;
        }
        self.changes.push(Change::Profile);
        true
    }
}
