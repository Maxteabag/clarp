//! What a Host keeps of its janitors, and what is done to it: a janitor is made of an
//! agent, changed, turned on and off, and removed.
//!
//! Every change that could let an old run act on a new configuration moves the janitor's
//! generation and its revision, and pauses it where the Python Host pauses it: a save
//! pauses, and so does a removal. An app that changes a janitor says which revision it saw;
//! one that saw another is told to read again.
//!
//! The book knows nothing of how an agent is kept. What it has to know of one it asks the
//! [`World`] around it, and what has to become of an agent it says as an [`Effect`], for
//! the Host to do.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use super::adaptive::{self, State};
use super::creation::Creation;
use super::runs::{Progress, Run};
use super::{
    Attachment, Kept, Named, Refused, attachment_values, execution, option_patch, option_values,
    portrait, scope, supported_triggers, template,
};

/// How long a name or an id of an agent may be.
const KEY_MOST: usize = 160;
/// The efforts a provider that is no tool of the Host takes.
const PROVIDER_EFFORTS: [&str; 4] = ["minimal", "low", "medium", "high"];

/// What the book has to know of an agent.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Agent {
    pub agent_id: String,
    pub session: String,
    pub name: String,
    pub backend: String,
    pub model: String,
    pub effort: String,
    pub is_janitor: bool,
    /// It was put away.
    pub archived: bool,
    /// A turn of it runs or waits, or its tool still has a process.
    pub busy: bool,
}

/// What the book asks of the Host around it.
pub trait World {
    /// The agent a session name or an id names.
    fn agent(&self, key: &str) -> Option<Agent>;
    /// Whether a time zone of this name is one the machine has.
    fn zone(&self, name: &str) -> bool;
    /// An id for an attachment that names none.
    fn draw(&mut self) -> String;
    /// Whether the Host has a tool of this name.
    fn backend(&self, name: &str) -> bool;
    /// Whether a tool has a model of this name. No name is the tool's own choice.
    fn model(&self, backend: &str, model: &str) -> bool;
    /// The efforts a tool takes.
    fn efforts(&self, backend: &str) -> Vec<String>;
    /// The time between the next two runs of a schedule, in seconds.
    fn base_interval(&self, expression: &str, zone: &str, now_ms: i64) -> i64;
    /// The first time after `from_ms` that a schedule names, if it names one within a year.
    fn next_run(&self, expression: &str, zone: &str, from_ms: i64) -> Option<i64>;
    /// A directory that is there: the one a path names, or, where it names none that is,
    /// the home of whoever runs the Host.
    fn directory(&self, path: &str) -> String;
    /// Twelve signs nobody has drawn before, for the name of a session.
    fn drawn(&mut self) -> String;
    /// Where the picture of this name is fetched.
    fn face(&self, file: &str) -> String;
}

/// What has to become of an agent, for the Host to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    /// The agent is a janitor from now on.
    Convert { agent_id: String },
    /// The agent is put away.
    PutAway { agent_id: String },
    /// The agent is a janitor no more: an agent like any other again.
    Release { agent_id: String },
    /// The agent's tool, model or effort is another. What is `None` stays.
    Tune {
        agent_id: String,
        backend: Option<String>,
        model: Option<String>,
        effort: Option<String>,
    },
}

/// One janitor's job, as it is kept.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub template_id: String,
    pub enabled: bool,
    pub revision: u64,
    pub generation: u64,
    /// As it was taken: both sides named for one a request made, nothing for one of the
    /// Host's own.
    pub scope: Map<String, Value>,
    pub execution: Map<String, Value>,
    /// What was chosen. What was not is the job's default, and is not kept.
    pub options: Map<String, Value>,
    pub last_error: String,
    pub last_run_at: Option<i64>,
    pub last_change_at: Option<i64>,
    pub created_at: i64,
    pub updated_at: i64,
}

/// One trigger of a janitor, as it is kept: also after it was taken away, so that its id is
/// never another's.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Trigger {
    pub agent_id: String,
    pub trigger_id: String,
    pub trigger_version: i64,
    pub enabled: bool,
    pub config: Map<String, Value>,
    pub next_run_at: Option<i64>,
    pub created_at: i64,
    pub retired_at: Option<i64>,
}

/// What a Host keeps of its janitors.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Book {
    /// Each janitor's job, by the agent's id.
    pub configs: BTreeMap<String, Config>,
    /// Every trigger there ever was, by its id.
    pub triggers: BTreeMap<String, Trigger>,
    /// The Host's own janitors: the job each was installed for, by the agent's id.
    pub builtins: BTreeMap<String, String>,
    /// The runs of the janitors, by their ids: every one that is out, and of those that are
    /// over the newest hundred of each janitor.
    pub runs: BTreeMap<String, Run>,
    /// What the runner keeps of each trigger between two looks, by the trigger's id.
    pub progress: BTreeMap<String, Progress>,
    /// The requests for a janitor that is made as a new agent, by the id each came under.
    pub creations: BTreeMap<String, Creation>,
}

/// A change a request asks for: the fields it names. One it does not name, or names as
/// nothing, stays as it is; only options that are named as nothing are refused.
pub type Asked = Map<String, Value>;

pub(super) fn refused(status: u16, code: &'static str, message: &str) -> Refused {
    Refused::new(status, code, message)
}

fn conflict() -> Refused {
    let why = "This configuration changed. Refresh it and repeat your change.";
    refused(409, "revision_conflict", why)
}

/// A field a request names as something.
fn said<'a>(asked: &'a Asked, key: &str) -> Option<&'a Value> {
    asked.get(key).filter(|value| !value.is_null())
}

/// The agent a key names, or why there is none.
pub(super) fn agent_of(key: &str, world: &dyn World) -> Result<Agent, Refused> {
    if key.is_empty() || key.chars().count() > KEY_MOST {
        return Err(Refused::new(
            400,
            "invalid_janitor",
            "Invalid agent identity",
        ));
    }
    world
        .agent(key)
        .ok_or_else(|| refused(404, "agent_not_found", "Agent not found"))
}

/// Whoever keeps attachments, for the rules that check them: the book, and the world.
struct Keeping<'a> {
    book: &'a Book,
    world: &'a mut dyn World,
}

impl Kept for Keeping<'_> {
    fn attachment_free(&self, attachment_id: &str, agent_id: &str) -> bool {
        self.book
            .triggers
            .get(attachment_id)
            .is_none_or(|kept| kept.agent_id == agent_id && kept.retired_at.is_none())
    }

    fn zone(&self, name: &str) -> bool {
        self.world.zone(name)
    }

    fn draw(&mut self) -> String {
        self.world.draw()
    }
}

/// Whether a model and an effort are ones the job can be run with.
pub(super) fn model_fits(
    world: &dyn World,
    backend: &str,
    model: Option<&Value>,
    effort: Option<&Value>,
    provider: Option<&str>,
) -> Result<(), Refused> {
    let invalid = |why: &str| Err(Refused::new(400, "invalid_janitor", why));
    let nothing = |value: Option<&Value>| value.is_none_or(|value| value == &json!(""));
    if provider == Some("local") {
        if !nothing(model) || !nothing(effort) {
            return invalid("Deterministic bookkeeping does not use a model or reasoning effort");
        }
        return Ok(());
    }
    if let Some(model) = model {
        let known = model
            .as_str()
            .is_some_and(|name| name.chars().count() <= KEY_MOST && world.model(backend, name));
        if !known {
            return invalid("Invalid model for this agent");
        }
    }
    if let Some(effort) = effort {
        let taken: Vec<String> = match provider {
            Some("openai") => PROVIDER_EFFORTS
                .iter()
                .map(|word| (*word).to_string())
                .collect(),
            _ => world.efforts(backend),
        };
        let fits = effort
            .as_str()
            .is_some_and(|word| word.is_empty() || taken.iter().any(|known| known == word));
        if !fits {
            return invalid("Invalid reasoning effort for this agent");
        }
    }
    Ok(())
}

/// The first trigger of a job, as a request would ask for it.
pub(super) fn default_trigger(template_id: &str) -> Result<Value, Refused> {
    Ok(json!([{ "trigger_id": template(template_id)?["default_trigger_id"] }]))
}

impl Book {
    fn config(&self, agent_id: &str) -> Result<&Config, Refused> {
        self.configs
            .get(agent_id)
            .ok_or_else(|| refused(404, "janitor_not_found", "Janitor not found"))
    }

    /// The triggers a janitor has, in the order they were given it.
    pub fn triggers_of(&self, agent_id: &str) -> Vec<(&String, &Trigger)> {
        let mut own: Vec<(&String, &Trigger)> = self
            .triggers
            .iter()
            .filter(|(_, kept)| kept.agent_id == agent_id && kept.retired_at.is_none())
            .collect();
        own.sort_by_key(|(id, kept)| (kept.created_at, (*id).clone()));
        own
    }

    pub(super) fn scope_of(
        &self,
        given: &Value,
        world: &dyn World,
    ) -> Result<Map<String, Value>, Refused> {
        let agent = |id: &str| {
            world
                .agent(id)
                .filter(|found| found.agent_id == id)
                .map(|found| Named {
                    is_janitor: found.is_janitor,
                })
        };
        let taken = scope(given, &agent)?;
        Ok(taken.shown().as_object().cloned().unwrap_or_default())
    }

    pub(super) fn attachments_of(
        &self,
        given: &Value,
        agent_id: &str,
        template_id: &str,
        world: &mut dyn World,
    ) -> Result<Vec<Attachment>, Refused> {
        let mut keeping = Keeping { book: self, world };
        attachment_values(given, agent_id, template_id, &mut keeping)
    }

    /// Takes away the triggers that are not among `taken`, and keeps the rest as they are
    /// now. A trigger that is kept again waits for its next run anew.
    fn keep_triggers(&mut self, agent_id: &str, taken: Vec<Attachment>, now_ms: i64) {
        for (id, kept) in &mut self.triggers {
            let stays = taken.iter().any(|one| one.attachment_id == *id);
            if kept.agent_id == agent_id && kept.retired_at.is_none() && !stays {
                kept.enabled = false;
                kept.retired_at = Some(now_ms);
                kept.next_run_at = None;
            }
        }
        for one in taken {
            // A trigger that is another than it was starts anew: what the runner kept of it
            // is dropped.
            let as_it_was = self.triggers.get(&one.attachment_id).is_some_and(|kept| {
                kept.trigger_id == one.trigger_id
                    && kept.trigger_version == one.trigger_version
                    && kept.config == one.config
            });
            if !as_it_was {
                self.progress.remove(&one.attachment_id);
            }
            let kept = self
                .triggers
                .entry(one.attachment_id)
                .or_insert_with(|| Trigger {
                    agent_id: agent_id.to_string(),
                    created_at: now_ms,
                    ..Trigger::default()
                });
            kept.trigger_id = one.trigger_id;
            kept.trigger_version = one.trigger_version;
            kept.enabled = one.enabled;
            kept.config = one.config;
            kept.next_run_at = None;
        }
    }

    /// Nothing that was started for the configuration as it was may act any more: a run
    /// that is out is cut off, and no run is due.
    fn fence(&mut self, agent_id: &str, now_ms: i64) {
        for kept in self.triggers.values_mut() {
            if kept.agent_id == agent_id {
                kept.next_run_at = None;
            }
        }
        for run in self.runs.values_mut() {
            if run.agent_id == agent_id {
                run.cut_off(now_ms);
            }
        }
    }

    /// A new generation and a new revision.
    fn moved(&mut self, agent_id: &str, now_ms: i64) -> Option<&mut Config> {
        let config = self.configs.get_mut(agent_id)?;
        config.generation += 1;
        config.revision += 1;
        config.updated_at = now_ms;
        Some(config)
    }

    /// Makes a janitor of an agent, paused, or reads the one it is. `asked` may name the
    /// job (`template_id`), a scope, attachments, options and how the job is run.
    pub fn create(
        &mut self,
        key: &str,
        asked: &Asked,
        world: &mut dyn World,
        now_ms: i64,
    ) -> Result<Vec<Effect>, Refused> {
        let agent = agent_of(key, world)?;
        let convert = || {
            vec![Effect::Convert {
                agent_id: agent.agent_id.clone(),
            }]
        };
        let there = self.configs.contains_key(&agent.agent_id);
        if there && agent.is_janitor {
            return Ok(Vec::new());
        }
        if agent.archived {
            let why = "Restore this agent before configuring maintenance";
            return Err(refused(409, "agent_archived", why));
        }
        if agent.busy {
            let why = "Wait for this agent's current work and queue to finish before converting it";
            return Err(refused(409, "agent_busy", why));
        }
        if there {
            // It was a janitor once and was let go: it is one again, as it was.
            return Ok(convert());
        }
        let template_id = match asked.get("template_id") {
            None => "task-labels",
            Some(named) => named.as_str().unwrap_or_default(),
        };
        let scope = self.scope_of(said(asked, "scope").unwrap_or(&json!({})), world)?;
        let given = match said(asked, "attachments") {
            Some(given) => given.clone(),
            None => default_trigger(template_id)?,
        };
        let taken = self.attachments_of(&given, &agent.agent_id, template_id, world)?;
        let run = execution(template_id, said(asked, "execution"), &agent.backend)?;
        let provider = run.get("provider").and_then(Value::as_str);
        let (model, effort) = (json!(agent.model), json!(agent.effort));
        model_fits(world, &agent.backend, Some(&model), Some(&effort), provider)?;
        let options = match asked.get("options") {
            None => Map::new(),
            Some(given) => option_patch(template_id, given, None)?,
        };
        template(template_id)?;
        let config = Config {
            template_id: template_id.to_string(),
            revision: 1,
            generation: 1,
            scope,
            execution: run,
            options,
            created_at: now_ms,
            updated_at: now_ms,
            ..Config::default()
        };
        self.configs.insert(agent.agent_id.clone(), config);
        self.keep_triggers(&agent.agent_id, taken, now_ms);
        Ok(convert())
    }

    /// Installs one of the Host's own janitors: its job as it comes, for an agent the Host
    /// made for it. Nothing is installed twice.
    pub fn install(&mut self, role: &str, agent_id: &str, config: Config, taken: Vec<Attachment>) {
        if self.builtins.values().any(|kept| kept == role) || self.configs.contains_key(agent_id) {
            return;
        }
        let now_ms = config.created_at;
        self.builtins.insert(agent_id.to_string(), role.to_string());
        self.configs.insert(agent_id.to_string(), config);
        self.keep_triggers(agent_id, taken, now_ms);
    }

    /// Changes a janitor: its job, scope, attachments, options, how it is run, its tool,
    /// model or effort. A change pauses it and moves its generation and its revision.
    pub fn configure(
        &mut self,
        key: &str,
        expected_revision: &Value,
        asked: &Asked,
        world: &mut dyn World,
        now_ms: i64,
    ) -> Result<Vec<Effect>, Refused> {
        let agent = agent_of(key, world)?;
        let id = agent.agent_id.clone();
        let config = self.config(&id)?.clone();
        if expected_revision.as_u64() != Some(config.revision) {
            return Err(conflict());
        }
        let invalid = |why: &str| Refused::new(400, "invalid_janitor", why);
        let backend = match said(asked, "backend") {
            None => agent.backend.clone(),
            Some(named) => {
                let named = named.as_str().filter(|name| world.backend(name));
                let named = named.ok_or_else(|| invalid("Unsupported Janitor backend"))?;
                named.to_string()
            }
        };
        let scope = match said(asked, "scope") {
            Some(given) => self.scope_of(given, world)?,
            None => config.scope.clone(),
        };
        let template_id = match said(asked, "template_id") {
            Some(named) => {
                let named = named.as_str().unwrap_or_default();
                template(named)?;
                named.to_string()
            }
            None => config.template_id.clone(),
        };
        let same_job = template_id == config.template_id;
        if self.builtins.contains_key(&id) && !same_job {
            let why = "A built-in Janitor cannot change its job";
            return Err(refused(409, "builtin_janitor", why));
        }
        let as_it_was = Value::Object(config.execution.clone());
        let run = match said(asked, "execution") {
            Some(given) => execution(&template_id, Some(given), &backend)?,
            None if same_job => execution(&template_id, Some(&as_it_was), &backend)?,
            None => execution(&template_id, None, &backend)?,
        };
        let provider = run.get("provider").and_then(Value::as_str);
        let (own_model, own_effort) = (json!(agent.model), json!(agent.effort));
        let model = said(asked, "model").unwrap_or(&own_model);
        let effort = said(asked, "effort").unwrap_or(&own_effort);
        model_fits(world, &backend, Some(model), Some(effort), provider)?;
        let mut options = if same_job {
            config.options.clone()
        } else {
            Map::new()
        };
        if let Some(given) = asked.get("options") {
            options = option_patch(&template_id, given, Some(&options))?;
        }
        let taken = match said(asked, "attachments") {
            Some(given) => Some(self.attachments_of(given, &id, &template_id, world)?),
            None if !same_job => {
                let first = default_trigger(&template_id)?;
                Some(self.attachments_of(&first, &id, &template_id, world)?)
            }
            None => None,
        };
        self.fence(&id, now_ms);
        // Watching other agents, or doing another job: what the runner kept is void.
        if scope != config.scope || !same_job {
            let triggers = &self.triggers;
            self.progress.retain(|attachment_id, _| {
                triggers
                    .get(attachment_id)
                    .is_none_or(|kept| kept.agent_id != id)
            });
        }
        if let Some(kept) = self.moved(&id, now_ms) {
            kept.enabled = false;
            kept.template_id = template_id;
            kept.scope = scope;
            kept.execution = run;
            kept.options = options;
            kept.last_error = String::new();
        }
        if let Some(taken) = taken {
            self.keep_triggers(&id, taken, now_ms);
        }
        let named = |key: &str| said(asked, key).and_then(Value::as_str).map(str::to_string);
        let tune = Effect::Tune {
            agent_id: id,
            backend: named("backend"),
            model: named("model"),
            effort: named("effort"),
        };
        let nothing = matches!(
            &tune,
            Effect::Tune {
                backend: None,
                model: None,
                effort: None,
                ..
            }
        );
        Ok(if nothing { Vec::new() } else { vec![tune] })
    }

    /// Turns a janitor on or off. What it is already is no change.
    pub fn set_enabled(
        &mut self,
        key: &str,
        expected_revision: &Value,
        enabled: bool,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<(), Refused> {
        let agent = agent_of(key, world)?;
        let id = &agent.agent_id;
        let config = self.config(id)?.clone();
        if expected_revision.as_u64() != Some(config.revision) {
            return Err(conflict());
        }
        if config.enabled == enabled {
            return Ok(());
        }
        if enabled {
            if !agent.is_janitor {
                let why = "This Janitor was released; convert it again before enabling";
                return Err(refused(409, "janitor_released", why));
            }
            if agent.archived {
                let why = "Archived maintenance cannot be enabled";
                return Err(refused(409, "agent_archived", why));
            }
            let kept = Value::Object(config.options.clone());
            let in_force = option_values(&config.template_id, Some(&kept))?;
            let scheduled = config.template_id == "scheduled-prompt";
            let instructed = in_force.get("instructions").and_then(Value::as_str);
            if scheduled && instructed.is_none_or(|text| crate::python::strip(text).is_empty()) {
                let why = "Give the scheduled task its instructions first";
                return Err(refused(400, "invalid_option", why));
            }
            let effects = |template_id: &str| -> Result<Vec<Value>, Refused> {
                let effects = template(template_id)?["allowed_effects"].as_array();
                Ok(effects.cloned().unwrap_or_default())
            };
            let own = effects(&config.template_id)?;
            for (other_id, other) in &self.configs {
                // Another janitor that is on, of an agent that is there and not put away.
                let lives = world.agent(other_id).is_some_and(|other| !other.archived);
                if other_id == id || !other.enabled || !lives {
                    continue;
                }
                // A scheduled task acts where it works, not on the agents it watches.
                if scheduled || other.template_id == "scheduled-prompt" {
                    continue;
                }
                let theirs = effects(&other.template_id)?;
                let same_work = own.iter().any(|effect| theirs.contains(effect));
                if same_work && overlap(&config.scope, &other.scope) {
                    let why = "Another enabled Janitor already maintains this job in this scope";
                    return Err(refused(409, "scope_conflict", why));
                }
            }
            if !self.triggers_of(id).iter().any(|(_, kept)| kept.enabled) {
                let why = "Enable at least one trigger first";
                return Err(Refused::new(400, "invalid_janitor", why));
            }
        }
        self.fence(id, now_ms);
        if let Some(kept) = self.moved(id, now_ms) {
            kept.enabled = enabled;
            kept.last_error = String::new();
        }
        Ok(())
    }

    /// Removes a janitor: it is paused, what was started for it may not act any more, and
    /// its agent is put away. Its conversation and what it did stay.
    pub fn remove(
        &mut self,
        key: &str,
        expected_revision: &Value,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<Vec<Effect>, Refused> {
        let agent = agent_of(key, world)?;
        let id = agent.agent_id;
        if expected_revision.as_u64() != Some(self.config(&id)?.revision) {
            return Err(conflict());
        }
        if self.builtins.contains_key(&id) {
            let why = "Pause a built-in Janitor instead of removing it";
            return Err(refused(409, "builtin_janitor", why));
        }
        self.fence(&id, now_ms);
        if let Some(kept) = self.moved(&id, now_ms) {
            kept.enabled = false;
        }
        Ok(vec![Effect::PutAway { agent_id: id }])
    }

    /// Lets a janitor go: it is an agent like any other again, and what it was as a janitor
    /// stays kept, paused, for the day it is made one again. It has to be paused and at
    /// rest. One of the Host's own is not let go. Handing what it kept to a successor
    /// (`successor`) is for a job this Host does not run yet.
    pub fn release(
        &mut self,
        key: &str,
        expected_revision: &Value,
        successor: (Option<&Value>, Option<&Value>),
        world: &dyn World,
        now_ms: i64,
    ) -> Result<Vec<Effect>, Refused> {
        let named = |said: Option<&Value>| said.is_some_and(|said| !said.is_null());
        if named(successor.0) != named(successor.1) {
            let why = "A maintenance successor needs its session and current revision";
            return Err(Refused::new(400, "invalid_janitor", why));
        }
        let agent = agent_of(key, world)?;
        let id = agent.agent_id.clone();
        let config = self.config(&id)?.clone();
        if expected_revision.as_u64() != Some(config.revision) {
            return Err(conflict());
        }
        if self.builtins.contains_key(&id) {
            let why = "A built-in Janitor cannot be released";
            return Err(refused(409, "builtin_janitor", why));
        }
        if config.enabled {
            let why = "Pause this Janitor before releasing or handing off maintenance";
            return Err(refused(409, "janitor_enabled", why));
        }
        if agent.busy {
            let why = "Wait for current maintenance and cancellation to finish";
            return Err(refused(409, "agent_busy", why));
        }
        if !agent.is_janitor {
            if named(successor.0) {
                let why = "This Janitor was already released";
                return Err(refused(409, "janitor_released", why));
            }
            return Ok(Vec::new());
        }
        if named(successor.0) {
            // What a janitor kept for the agents it watched (their labels) is handed to
            // another: nothing is kept for them here yet.
            let why = "Handing a janitor's work to a successor is not built on this Host yet";
            return Err(refused(501, "not_built", why));
        }
        self.fence(&id, now_ms);
        self.moved(&id, now_ms);
        Ok(vec![Effect::Release { agent_id: id }])
    }

    /// Puts a janitor's tuning back as its job has it: its triggers' settings, its options,
    /// and the model its job recommends where it runs on the tool its job recommends. Who
    /// it is, what it watches and what it did stay.
    pub fn reset_defaults(
        &mut self,
        key: &str,
        expected_revision: &Value,
        world: &mut dyn World,
        now_ms: i64,
    ) -> Result<Vec<Effect>, Refused> {
        let agent = agent_of(key, world)?;
        let config = self.config(&agent.agent_id)?.clone();
        let job = template(&config.template_id)?;
        let mut attachments = Vec::new();
        for (id, kept) in self.triggers_of(&agent.agent_id) {
            let defaults = super::triggers().iter().find(|trigger| {
                trigger["trigger_id"] == kept.trigger_id
                    && trigger["version"] == kept.trigger_version
            });
            attachments.push(json!({
                "attachment_id": id, "trigger_id": kept.trigger_id,
                "trigger_version": kept.trigger_version, "enabled": kept.enabled,
                "config": defaults.map_or(json!({}), |trigger| trigger["defaults"].clone()),
            }));
        }
        let mut options = Map::new();
        for option in job["options"].as_array().map_or(&[][..], Vec::as_slice) {
            if let Some(key) = option["key"].as_str() {
                options.insert(key.to_string(), option["default"].clone());
            }
        }
        let recommended = job["recommended_backend"] == json!(agent.backend);
        let model = recommended
            .then(|| job["recommended_model"].clone())
            .filter(|model| model.as_str().is_some_and(|name| !name.is_empty()));
        let mut asked = Asked::new();
        asked.insert("attachments".to_string(), json!(attachments));
        asked.insert("execution".to_string(), json!({}));
        asked.insert("options".to_string(), Value::Object(options));
        if let Some(model) = model {
            asked.insert("model".to_string(), model);
            asked.insert("effort".to_string(), job["recommended_effort"].clone());
        }
        self.configure(key, expected_revision, &asked, world, now_ms)
    }

    /// Takes over, once, the choice a device made of how tools are explained before the
    /// janitor that explains them had options. A choice that is made already stands.
    pub fn adopt_options(
        &mut self,
        key: &str,
        expected_revision: &Value,
        options: &Value,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<(), Refused> {
        let only = options
            .as_object()
            .filter(|given| given.len() == 1 && given.contains_key("detail_level"));
        let Some(given) = only else {
            let why = "Only the legacy detail_level option may be adopted";
            return Err(Refused::new(400, "invalid_janitor", why));
        };
        option_patch("tool-explainer", options, None)?;
        let agent = agent_of(key, world)?;
        let id = &agent.agent_id;
        let config = self.config(id)?.clone();
        let explains = self
            .builtins
            .get(id)
            .is_some_and(|role| role == "tool-explainer")
            && agent.is_janitor
            && !agent.archived
            && config.template_id == "tool-explainer";
        if !explains {
            let why = "Legacy options can only be adopted by the built-in tool explainer";
            return Err(refused(409, "invalid_adoption", why));
        }
        if config.options.contains_key("detail_level") {
            return Ok(());
        }
        if expected_revision.as_u64() != Some(config.revision) {
            return Err(conflict());
        }
        let options = option_patch(
            "tool-explainer",
            &Value::Object(given.clone()),
            Some(&config.options),
        )?;
        self.fence(id, now_ms);
        if let Some(kept) = self.moved(id, now_ms) {
            kept.options = options;
        }
        Ok(())
    }

    /// The janitors of the Host: the agents that are janitors, have a job and were not put
    /// away, by their ids.
    pub fn listed(&self, world: &dyn World) -> Vec<String> {
        let mut ids: Vec<String> = Vec::new();
        for id in self.configs.keys() {
            if world
                .agent(id)
                .is_some_and(|agent| agent.is_janitor && !agent.archived)
            {
                ids.push(id.clone());
            }
        }
        ids
    }

    /// A janitor as an app reads it.
    pub fn shown(&self, key: &str, world: &dyn World, now_ms: i64) -> Result<Value, Refused> {
        let agent = agent_of(key, world)?;
        let id = &agent.agent_id;
        let config = self.config(id)?;
        let role = self.builtins.get(id);
        let job = template(&config.template_id)?;
        let kept = Value::Object(config.options.clone());
        let options = option_values(&config.template_id, Some(&kept))?;
        let mut chosen: Vec<&String> = config.options.keys().collect();
        chosen.sort();
        let own = role.is_none();
        let face = if agent.is_janitor {
            world.face(portrait(&config.template_id))
        } else {
            String::new()
        };
        let current = self.out_of(id).map(|run| run.run_id.clone());
        let health = match (config.enabled, &current, config.last_error.is_empty()) {
            (false, _, _) => "paused",
            (true, Some(_), _) => "running",
            (true, None, false) => "needs_attention",
            (true, None, true) => "ready",
        };
        let attachments: Vec<Value> = self
            .triggers_of(id)
            .into_iter()
            .map(|(attachment_id, kept)| {
                json!({
                    "attachment_id": attachment_id, "trigger_id": kept.trigger_id,
                    "trigger_version": kept.trigger_version, "enabled": kept.enabled,
                    "config": kept.config, "next_run_at": kept.next_run_at,
                })
            })
            .collect();
        Ok(json!({
            "agent_id": id, "session": agent.session, "name": agent.name,
            "is_janitor": agent.is_janitor, "enabled": config.enabled,
            "revision": config.revision, "builtin_role": role,
            "execution": config.execution, "options": options,
            "configured_option_keys": chosen, "avatar_url": face,
            "capabilities": { "can_change_template": own, "can_remove": own, "can_release": own },
            "supported_trigger_ids": supported_triggers(job),
            "cancellation_pending": !config.enabled
                && (agent.busy || self.pending_claim(id, now_ms)),
            "generation": config.generation, "template_id": config.template_id,
            "model": agent.model, "effort": agent.effort, "backend": agent.backend,
            "scope": config.scope, "health": health, "last_error": config.last_error,
            "last_run_at": config.last_run_at, "last_change_at": config.last_change_at,
            "current_run_id": current, "attachments": attachments,
            "cadence": self.cadence(id, config, &options, world, now_ms),
        }))
    }

    /// For each thing a janitor does at intervals: how long it waits between two runs as it
    /// is set, as it is now, and at the longest, and when it runs next.
    fn cadence(
        &self,
        agent_id: &str,
        config: &Config,
        options: &Map<String, Value>,
        world: &dyn World,
        now_ms: i64,
    ) -> Vec<Value> {
        let longest = options.get("max_interval_seconds").and_then(Value::as_i64);
        let fresh = State::default();
        // The janitors the Host runs by itself wait as their options say.
        let own = |lane: &str, option: &str| {
            let base = options.get(option).and_then(Value::as_i64).unwrap_or(0);
            let provider=lane.strip_prefix("hotseat-switcher.");
            let progress=provider.and_then(|tool|self.progress.get(&format!("account-switch/{agent_id}/{tool}"))).filter(|p|p.generation==config.generation);
            adaptive::cadence(progress.and_then(|p|p.adaptive.as_ref()).unwrap_or(&fresh), (base,longest),progress.and_then(|p|p.next_run_at).filter(|_|config.enabled),lane)
        };
        match config.template_id.as_str() {
            "label-auditor" => return vec![own("label-auditor", "interval_seconds")],
            "quota-monitor" => return vec![own("quota-keeper", "interval_seconds")],
            "account-hotseat" => {
                return vec![
                    own("hotseat-switcher.claude", "interval_seconds"),
                    own("hotseat-switcher.codex", "codex_interval_seconds"),
                ];
            }
            _ => {}
        }
        let mut lanes = Vec::new();
        for (id, kept) in self.triggers_of(agent_id) {
            // What the runner kept of the trigger, where it was kept for the job as it is.
            let state = self.progress.get(id);
            let state = state.filter(|state| state.generation == config.generation);
            let base = match kept.trigger_id.as_str() {
                "active-interval" => kept.config.get("interval_seconds").and_then(Value::as_i64),
                "schedule" => {
                    let measured = state.and_then(|state| state.cron_base_seconds);
                    let said = |key: &str| kept.config.get(key).and_then(Value::as_str);
                    measured.filter(|base| *base != 0).or_else(|| {
                        let (expression, zone) = (said("cron")?, said("timezone")?);
                        Some(world.base_interval(expression, zone, now_ms))
                    })
                }
                _ => continue,
            };
            let waited = state.and_then(|state| state.adaptive.as_ref());
            let next = kept.next_run_at.filter(|_| config.enabled);
            let lane = format!("{}:{id}", kept.trigger_id);
            let set = (base.unwrap_or(900), longest);
            lanes.push(adaptive::cadence(
                waited.unwrap_or(&fresh),
                set,
                next,
                &lane,
            ));
        }
        lanes
    }
}

/// Whether two scopes, as they are kept, have an agent in common.
fn overlap(ours: &Map<String, Value>, theirs: &Map<String, Value>) -> bool {
    let side = |scope: &Map<String, Value>, key: &str| -> Vec<String> {
        let ids = scope.get(key).and_then(Value::as_array);
        let ids = ids.map(|ids| {
            ids.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        });
        ids.unwrap_or_default()
    };
    let as_scope = |scope: &Map<String, Value>| super::Scope {
        agent_ids: side(scope, "agent_ids"),
        exclude_agent_ids: side(scope, "exclude_agent_ids"),
    };
    as_scope(ours).overlaps(&as_scope(theirs))
}
