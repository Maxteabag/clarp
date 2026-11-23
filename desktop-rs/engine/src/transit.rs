//! Changing an agent's backend from its profile (Maxteabag/clarp#720, Host
//! feature `agent_backend_transit`): the per-agent choice the Host offers,
//! the start, and the move's phases from the event stream, the GET after a
//! reconnect and the roster row's `transit`. The Host writes the result row
//! in the chat; nothing here adds one.

use std::collections::{HashMap, HashSet};

use clarp_core::json::{self, Object};
use clarp_core::transit::{self, Backend, Event, Flow};
use serde_json::{Value, json};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct Transits {
    feature: bool,
    flows: HashMap<String, Flow>,
    /// session → the Host's offer for that agent, once read.
    options: HashMap<String, Vec<Backend>>,
    options_loading: HashSet<String>,
    /// Transit ids whose state was asked after the roster lost them.
    asked: HashSet<String>,
}

impl Engine {
    /// The Host offers changing an agent's backend.
    pub fn backend_transit_offered(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.backend_transit_offered(local));
        self.transits.feature
    }

    /// The agent's flow (idle when none).
    pub fn transit_flow(&self, session: &str) -> Flow {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_flow(local));
        self.transits.flows.get(session).cloned().unwrap_or_default()
    }

    /// The backends the Host offers this agent; empty until read.
    pub fn transit_options(&self, session: &str) -> Vec<Backend> {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_options(local));
        self.transits.options.get(session).cloned().unwrap_or_default()
    }

    pub fn transit_options_loading(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_options_loading(local));
        self.transits.options_loading.contains(session)
    }

    /// A move may not start: a turn runs or messages wait.
    pub fn transit_blocked(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_blocked(local));
        self.roster.find(session).is_none_or(|agent| agent.busy || agent.queued_turn_count > 0)
    }

    /// A backend id in words: the Host's label when it is offered.
    pub fn transit_backend_label(&self, session: &str, backend: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_backend_label(local, backend));
        self.transits
            .options
            .get(session)
            .and_then(|options| options.iter().find(|b| b.id == backend))
            .map(|b| b.label.clone())
            .or_else(|| self.backend_options().iter().find(|o| o["id"] == backend).and_then(|o| o["label"].as_str()).map(str::to_owned))
            .filter(|label| !label.is_empty())
            .unwrap_or_else(|| {
                let mut chars = backend.chars();
                chars.next().map_or_else(String::new, |c| c.to_uppercase().chain(chars).collect())
            })
    }

    /// A model id in words: the Host's label when it is offered.
    pub fn transit_model_label(&self, session: &str, backend: &str, model: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.transit_model_label(local, backend, model));
        self.transits
            .options
            .get(session)
            .and_then(|options| options.iter().find(|b| b.id == backend))
            .and_then(|b| b.models.iter().find(|m| m.id == model))
            .map(|m| m.label.clone())
            .or_else(|| self.models_for_backend(backend).iter().find(|m| m["id"] == model).and_then(|m| m["label"].as_str()).map(str::to_owned))
            .filter(|label| !label.is_empty())
            .unwrap_or_else(|| model.to_owned())
    }

    /// Opens the picker (and reads what the Host offers); false while the
    /// agent is busy, a move runs or the Host does not offer it.
    pub fn transit_open(&mut self, session: &str) -> bool {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_open(local));
        if !self.transits.feature || session.is_empty() {
            return false;
        }
        let blocked = self.transit_blocked(session);
        let opened = self.transits.flows.entry(session.to_owned()).or_default().open(blocked);
        if opened {
            self.transits.options_loading.insert(session.to_owned());
            self.api.get(&format!("transit-options:{session}"), "/agent-model-options", &[("session", session)]);
            self.changes.push(Change::Transit(session.to_owned()));
        }
        opened
    }

    pub fn transit_pick_backend(&mut self, session: &str, backend: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_pick_backend(local, backend));
        let Some(chosen) = self.transits.options.get(session).and_then(|o| o.iter().find(|b| b.id == backend)).cloned() else { return };
        self.update_flow(session, |flow| flow.pick_backend(&chosen));
    }

    pub fn transit_pick_model(&mut self, session: &str, model: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_pick_model(local, model));
        self.update_flow(session, |flow| flow.pick_model(model));
    }

    /// Escape: one level back (the picker closes from its first step).
    pub fn transit_back(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_back(local));
        self.update_flow(session, Flow::back);
    }

    /// Closes the picker or clears a finished result.
    pub fn transit_reset(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_reset(local));
        self.update_flow(session, Flow::reset);
    }

    /// Move: POST /agent-transit with a request id a retry keeps.
    pub fn transit_confirm(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.transit_confirm(local));
        let mut start = None;
        let from = self.roster.find(session).map(|a| a.backend.clone()).unwrap_or_default();
        self.update_flow(session, |flow| {
            start = flow.confirm(|| uuid::Uuid::new_v4().to_string());
            if start.is_some() {
                flow.from_backend = from;
            }
        });
        let Some((backend, model, request_id)) = start else { return };
        self.api.post_json(
            &format!("transit-start:{session}"),
            "/agent-transit",
            json!({"session": session, "backend": backend, "model": model, "request_id": request_id}),
            None,
        );
    }

    fn update_flow(&mut self, session: &str, change: impl FnOnce(&mut Flow)) {
        if session.is_empty() {
            return;
        }
        let flow = self.transits.flows.entry(session.to_owned()).or_default();
        let before = flow.clone();
        change(flow);
        if *flow != before {
            if flow.transit_id().is_none() {
                // A new move may be asked about again.
                if let Some(id) = before.transit_id() {
                    self.transits.asked.remove(id);
                }
            }
            self.changes.push(Change::Transit(session.to_owned()));
        }
    }

    // ---- the Host ------------------------------------------------------------

    /// `/server-info` answered.
    pub(crate) fn transit_server_info(&mut self, info: &Object) {
        let has = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(transit::FEATURE)));
        if has != self.transits.feature {
            self.transits.feature = has;
            if !has {
                self.transits.flows.clear();
                self.transits.options.clear();
            }
            self.changes.push(Change::Profile);
        }
    }

    /// SSE `agent-transit`.
    pub(crate) fn transit_event(&mut self, event: &Object) {
        let Some(progress) = Event::from_json(event) else { return };
        let session = if progress.session.is_empty() {
            let agent_id = json::string(event, "agent_id");
            self.roster.agents().iter().find(|a| !agent_id.is_empty() && a.agent_id == agent_id).map(|a| a.session.clone()).unwrap_or_default()
        } else {
            progress.session.clone()
        };
        self.update_flow(&session, |flow| flow.apply(&progress));
    }

    /// After a snapshot: a move the roster names is shown (also one this app
    /// did not start); one this app holds that the roster no longer names
    /// ended while the stream was away, so its state is asked once.
    pub(crate) fn transit_snapshot(&mut self) {
        if !self.transits.feature {
            return;
        }
        let rows: Vec<(String, String, Option<Event>)> = self
            .roster
            .agents()
            .iter()
            .map(|agent| {
                let mut row = agent.transit.clone();
                row.entry("session").or_insert_with(|| Value::String(agent.session.clone()));
                (agent.session.clone(), agent.backend.clone(), Event::from_json(&row))
            })
            .collect();
        for (session, backend, running) in rows {
            if let Some(running) = running {
                self.update_flow(&session, |flow| {
                    flow.adopt(&running);
                    if flow.moving() && flow.from_backend.is_empty() {
                        flow.from_backend = backend;
                    }
                });
                continue;
            }
            let Some(id) = self.transits.flows.get(&session).and_then(|f| f.transit_id()).map(str::to_owned) else { continue };
            if self.transits.asked.insert(id.clone()) {
                self.api.get(&format!("transit-status:{session}"), "/agent-transit", &[("session", &session), ("transit_id", &id)]);
            }
        }
    }

    pub(crate) fn transit_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(session) = tag.strip_prefix("transit-options:") {
            let session = session.to_owned();
            let options = transit::options(object, &chrono::Local::now());
            self.transits.options_loading.remove(&session);
            self.transits.options.insert(session.clone(), options);
            self.changes.push(Change::Transit(session));
        } else if let Some(session) = tag.strip_prefix("transit-start:") {
            let session = session.to_owned();
            let id = json::string(object, "transit_id");
            if !id.is_empty() {
                self.update_flow(&session, |flow| flow.started(&id));
            }
        } else if let Some(session) = tag.strip_prefix("transit-status:") {
            let session = session.to_owned();
            if let Some(progress) = Event::from_json(object) {
                self.update_flow(&session, |flow| flow.apply(&progress));
            }
        } else {
            return false;
        }
        true
    }

    pub(crate) fn transit_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if let Some(session) = tag.strip_prefix("transit-options:") {
            let session = session.to_owned();
            self.transits.options_loading.remove(&session);
            self.transits.options.insert(session.clone(), Vec::new());
            self.changes.push(Change::Transit(session));
        } else if let Some(session) = tag.strip_prefix("transit-start:") {
            let session = session.to_owned();
            // The Host's refusal names its code (`{"error": "turn_running"}`);
            // a backend_unavailable one is worded as the picker words it.
            let target = match &self.transit_flow(&session).step {
                transit::Step::Moving { backend, .. } => backend.clone(),
                _ => String::new(),
            };
            let unavailable =
                self.transits.options.get(&session).and_then(|o| o.iter().find(|b| b.id == target)).and_then(|b| b.reason.clone());
            let code = clarp_core::endpoint::refusal_code(message).unwrap_or(message);
            let reason = transit::refusal(status, code, unavailable.as_deref());
            self.update_flow(&session, |flow| flow.refused(&reason));
        } else if tag.starts_with("transit-status:") {
            // The next snapshot shows where it stands.
            eprintln!("Engine: {tag} failed: {message} (HTTP {status})");
        } else {
            return false;
        }
        true
    }
}
