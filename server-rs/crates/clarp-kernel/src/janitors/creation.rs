//! A janitor that is made as a new agent, with one request.
//!
//! An app that makes a janitor which is no agent yet says a name, a tool, where it works,
//! its job, and an id of the request. The request is one request however often it is
//! sent: a session is kept for it first ([`Book::begin_creation`]), then the Host makes the
//! agent as a janitor and says so ([`Book::creation_made`]), gives it the job, and what was
//! answered is kept ([`Book::complete_creation`]). Sent again at any point, it goes on
//! where it was and makes nothing twice; under the same id with other settings it is
//! refused.
//!
//! The rules are the Python Host's (`server/lib/janitors.py`: `begin_creation`,
//! `create_reserved_agent`, `complete_creation`), held by
//! `tests/fixtures/janitor-creation-vectors.json`
//! (`server-rs/tools/janitor-creation-vectors.py`).

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::book::{Book, World, default_trigger, model_fits, refused};
use super::{Refused, default_model, execution, option_patch, template};
use crate::python;

/// How long the id of a request and a janitor's name may be, how much of the name goes
/// into the session's, and how often a session is drawn before the Host gives up.
const ID_MOST: usize = 160;
const NAME_MOST: usize = 100;
const SLUG_MOST: usize = 40;
const DRAWN_MOST: usize = 10;
/// What a request for a new janitor may say, the tool it gets when it names none, and the
/// job when it names none.
const SAID: [&str; 10] = [
    "name",
    "backend",
    "cwd",
    "model",
    "effort",
    "template_id",
    "scope",
    "attachments",
    "options",
    "execution",
];
const DEFAULT_TOOL: &str = "codex";
const DEFAULT_JOB: &str = "task-labels";

/// Who the new agent is to be.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Identity {
    pub persona: String,
    pub backend: String,
    pub cwd: String,
    pub model: String,
    pub effort: String,
}

/// One request for a new janitor, as it is kept from its first asking on.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Creation {
    /// What it said, as one text: the same request is the same text.
    pub asked: String,
    /// The session kept for its agent.
    pub session: String,
    pub identity: Identity,
    /// The agent that was made for it, once it was.
    pub agent_id: Option<String>,
    /// What was answered when it was finished.
    pub response: Option<Value>,
    pub created_at: i64,
    pub completed_at: Option<i64>,
}

/// Where a request for a new janitor stands.
#[derive(Debug, Clone, PartialEq)]
pub struct Begun {
    pub request_id: String,
    pub session: String,
    pub agent_id: Option<String>,
    pub completed: bool,
    pub response: Option<Value>,
    pub identity: Identity,
}

impl Begun {
    /// As the Python Host tells it.
    pub fn shown(&self) -> Value {
        let identity = &self.identity;
        json!({
            "request_id": self.request_id, "session": self.session, "agent_id": self.agent_id,
            "completed": self.completed, "response": self.response,
            "identity": {
                "name": identity.persona, "backend": identity.backend, "cwd": identity.cwd,
                "model": identity.model, "effort": identity.effort,
            },
        })
    }
}

fn conflict(why: &str) -> Refused {
    refused(409, "creation_conflict", why)
}

impl Book {
    /// The first asking of a request for a new janitor, or any later one: what it says is
    /// checked whole before anything is made, a session is kept for its agent, and where
    /// the request stands is answered.
    pub fn begin_creation(
        &mut self,
        request_id: &Value,
        payload: &Value,
        world: &mut dyn World,
        now_ms: i64,
    ) -> Result<Begun, Refused> {
        let invalid = |why: &str| Refused::new(400, "invalid_janitor", why);
        // An id is a text without blanks or signs below them.
        let id = request_id.as_str().filter(|id| {
            let fits = (1..=ID_MOST).contains(&id.chars().count());
            fits && id.chars().all(|sign| u32::from(sign) >= 33)
        });
        let id = id.ok_or_else(|| invalid("A stable creation request_id is required"))?;
        let payload = payload.as_object();
        let mut values = payload
            .cloned()
            .ok_or_else(|| invalid("Creation payload must be an object"))?;
        values.remove("request_id");
        if values.keys().any(|key| !SAID.contains(&key.as_str())) {
            return Err(invalid("Unsupported new Janitor configuration field"));
        }
        let asked = python::sorted_ascii(&Value::Object(values.clone()));
        if let Some(known) = self.creations.get(id) {
            if known.asked != asked {
                return Err(conflict(
                    "This creation request refers to different settings",
                ));
            }
            return self.begun(id, world);
        }
        let said = |key: &str| values.get(key).filter(|value| !value.is_null());
        let name = said("name").and_then(Value::as_str).map(python::strip);
        let name = name.filter(|name| !name.is_empty() && name.chars().count() <= NAME_MOST);
        let name = name.ok_or_else(|| invalid("A short Janitor name is required"))?;
        let backend = match said("backend") {
            None => DEFAULT_TOOL,
            Some(named) => {
                let known = named.as_str().filter(|name| world.backend(name));
                known.ok_or_else(|| invalid("Choose an available agent backend"))?
            }
        };
        if said("cwd").is_some_and(|cwd| !cwd.is_string()) {
            return Err(invalid("Workspace must be a path"));
        }
        if ["model", "effort"]
            .iter()
            .any(|key| said(key).is_some_and(|value| !value.is_string()))
        {
            return Err(invalid("Model and effort must be text"));
        }
        // A tool's own model for janitors, where the request names none, and with it the
        // least effort; a model that is named comes without one.
        let text = |key: &str| said(key).and_then(Value::as_str).unwrap_or_default();
        let own = default_model(backend);
        let model = match values.contains_key("model") {
            true => text("model"),
            false => own,
        };
        let effort = match values.contains_key("effort") {
            true => text("effort"),
            false if !own.is_empty() && model == own => "low",
            false => "",
        };
        let (model, effort) = (python::strip(model), python::strip(effort).to_lowercase());
        let job = match values.get("template_id") {
            None => DEFAULT_JOB,
            Some(named) => named.as_str().unwrap_or_default(),
        };
        let run = execution(job, said("execution"), backend)?;
        let provider = run.get("provider").and_then(Value::as_str);
        model_fits(
            world,
            backend,
            Some(&json!(model)),
            Some(&json!(effort)),
            provider,
        )?;
        if backend == "agy" && !effort.is_empty() {
            return Err(invalid(
                "AGY model-specific effort compatibility is unknown",
            ));
        }
        // Its job, whom it watches, what starts it and its options: as for a janitor that
        // is made of an agent, before there is an agent.
        template(job)?;
        let everyone = json!({});
        self.scope_of(said("scope").unwrap_or(&everyone), world)?;
        let first = default_trigger(job)?;
        self.attachments_of(said("attachments").unwrap_or(&first), "", job, world)?;
        if let Some(options) = values.get("options") {
            option_patch(job, options, None)?;
        }
        let identity = Identity {
            persona: name.to_string(),
            backend: backend.to_string(),
            cwd: world.directory(text("cwd")),
            model: model.to_string(),
            effort,
        };
        // The session: the name's letters and digits, and something nobody has drawn.
        let letters = name.to_lowercase();
        let letters = letters
            .chars()
            .filter(|sign| sign.is_alphanumeric() || "._-".contains(*sign));
        let slug: String = letters.take(SLUG_MOST).collect();
        let slug = if slug.is_empty() {
            "janitor"
        } else {
            slug.as_str()
        };
        let mut session = None;
        for _ in 0..DRAWN_MOST {
            let drawn = format!("{slug}-{}", world.drawn());
            let kept = self.creations.values().any(|kept| kept.session == drawn);
            if !kept && world.agent(&drawn).is_none() {
                session = Some(drawn);
                break;
            }
        }
        let session = session.ok_or_else(|| conflict("Could not reserve a fresh session"))?;
        let kept = Creation {
            asked,
            session,
            identity,
            created_at: now_ms,
            ..Creation::default()
        };
        self.creations.insert(id.to_string(), kept);
        self.begun(id, world)
    }

    /// Where a request stands. Only the agent that was made for it is its agent: a session
    /// somebody else has, or an agent that is gone, ends it.
    fn begun(&self, request_id: &str, world: &dyn World) -> Result<Begun, Refused> {
        let kept = self.creations.get(request_id);
        let kept = kept.ok_or_else(|| conflict("Creation request does not own this session"))?;
        let there = world.agent(&kept.session).map(|agent| agent.agent_id);
        match (&there, &kept.agent_id) {
            (Some(there), made) if made.as_ref() != Some(there) => {
                Err(conflict("The reserved session belongs to another creation"))
            }
            (None, Some(_)) => Err(conflict("This creation's agent was removed")),
            _ => Ok(Begun {
                request_id: request_id.to_string(),
                session: kept.session.clone(),
                agent_id: kept.agent_id.clone(),
                completed: kept.completed_at.is_some(),
                response: kept.response.clone(),
                identity: kept.identity.clone(),
            }),
        }
    }

    /// The agent of a request was made.
    pub fn creation_made(&mut self, request_id: &str, agent_id: &str) {
        if let Some(kept) = self.creations.get_mut(request_id) {
            kept.agent_id = Some(agent_id.to_string());
        }
    }

    /// The janitor of a request has its job: what is answered is kept, and is the answer
    /// to the request from now on.
    pub fn complete_creation(
        &mut self,
        request_id: &str,
        session: &str,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<Value, Refused> {
        let kept = self.creations.get(request_id);
        if !kept.is_some_and(|kept| kept.session == session) {
            return Err(conflict("Creation request does not own this session"));
        }
        let begun = self.begun(request_id, world)?;
        if begun.completed {
            return Ok(begun.response.unwrap_or(Value::Null));
        }
        let Some(agent_id) = begun.agent_id else {
            let why = "The registered identity has not been created";
            return Err(refused(409, "creation_incomplete", why));
        };
        let configured = self.shown(session, world, now_ms)?;
        if configured["agent_id"] != json!(agent_id) {
            return Err(conflict("Creation identity changed"));
        }
        if let Some(kept) = self.creations.get_mut(request_id) {
            kept.response = Some(configured.clone());
            kept.completed_at = Some(now_ms);
        }
        Ok(configured)
    }
}
