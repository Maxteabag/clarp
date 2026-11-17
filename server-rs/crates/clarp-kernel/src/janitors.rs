//! The janitors of a Host: agents that are given a job of maintenance.
//!
//! A janitor is an ordinary agent with a job: a template (what it does), a scope (the agents
//! it watches), attachments (the triggers that start it) and options. The rules are the
//! Python Host's (`server/lib/janitors.py`), without a Host, a tool or a disk: what a Host
//! offers (the catalogue of templates and triggers), and what it makes of options, of a
//! scope, of attachments and of how a job is run, each as it is taken or why it is refused,
//! in the Python Host's status, word for the reason and words.
//!
//! Held to that module by `tests/fixtures/janitor-vectors.json`, which
//! `server-rs/tools/janitor-vectors.py` writes by running the module itself; the same tool
//! writes the catalogue this file carries (`janitor-catalogue.json`).
//!
//! Where the Python Host breaks on what it is given (a schedule whose expression is no
//! text, a provider that is no text: an error of its own making, answered as a failure of
//! the Host), this refuses in the words it has for that kind of thing.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use serde_json::{Map, Value, json};

use crate::schedules;

pub mod adaptive;
pub mod book;
pub mod creation;
pub mod demand;
pub mod runs;
pub mod schedule;

const CATALOGUE: &str = include_str!("janitor-catalogue.json");

/// The most triggers one janitor may have.
const MOST_TRIGGERS: usize = 8;
/// The most agents a scope may name on either side.
const MOST_IN_SCOPE: usize = 1000;
/// How long a text of instructions may be when its option does not say.
const INSTRUCTIONS_MOST: u64 = 8000;
/// How long a short text may be, and an attachment's id.
const SHORT_MOST: usize = 160;
const ATTACHMENT_ID_MOST: usize = 100;

/// Why something about a janitor is not done: the status, a word for the reason, and words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub status: u16,
    pub code: &'static str,
    pub message: String,
}

impl Refused {
    pub fn new(status: u16, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }

    /// What the Python Host says where it names no reason of its own.
    fn invalid(message: impl Into<String>) -> Self {
        Self::new(400, "invalid_janitor", message)
    }

    /// What an app is answered.
    pub fn told(&self) -> Value {
        json!({ "error": self.message, "code": self.code })
    }
}

/// What a Host offers, as the Python Host's own modules list it.
#[derive(Debug, Default)]
struct Catalogue {
    templates: Vec<Value>,
    triggers: Vec<Value>,
    turn_templates: Vec<String>,
    routing: Vec<String>,
    isolating: Vec<String>,
    backend_of: Map<String, Value>,
    default_models: Map<String, Value>,
    names: Map<String, Value>,
    roles: Vec<String>,
    optional_roles: Vec<String>,
    own_ids: Map<String, Value>,
    portraits: Map<String, Value>,
}

fn catalogue() -> &'static Catalogue {
    static READ: OnceLock<Catalogue> = OnceLock::new();
    READ.get_or_init(|| {
        let whole: Value = serde_json::from_str(CATALOGUE).unwrap_or_default();
        let list = |value: &Value| value.as_array().cloned().unwrap_or_default();
        let words = |value: &Value| -> Vec<String> {
            let words = list(value).into_iter();
            words
                .filter_map(|word| word.as_str().map(str::to_string))
                .collect()
        };
        let map = |value: &Value| value.as_object().cloned().unwrap_or_default();
        Catalogue {
            templates: list(&whole["templates"]),
            triggers: list(&whole["triggers"]),
            turn_templates: words(&whole["turn_templates"]),
            routing: words(&whole["providers"]["routing"]),
            isolating: words(&whole["providers"]["isolating"]),
            backend_of: map(&whole["providers"]["backend_of"]),
            default_models: map(&whole["providers"]["default_models"]),
            names: map(&whole["providers"]["names"]),
            roles: words(&whole["roles"]),
            optional_roles: words(&whole["optional_roles"]),
            own_ids: map(&whole["own_ids"]),
            portraits: map(&whole["portraits"]),
        }
    })
}

/// The model a tool runs a janitor on when a request for a new one names none. Nothing: the
/// tool's own choice.
pub fn default_model(backend: &str) -> &'static str {
    let named = catalogue().default_models.get(backend);
    named.and_then(Value::as_str).unwrap_or_default()
}

/// The tool a name stands for: its own name, or another it goes by (`antigravity`).
pub fn tool_named(name: &str) -> Option<&'static str> {
    catalogue().names.get(name).and_then(Value::as_str)
}

/// The jobs a janitor can be given.
pub fn templates() -> &'static [Value] {
    &catalogue().templates
}

/// What can start a janitor.
pub fn triggers() -> &'static [Value] {
    &catalogue().triggers
}

/// The jobs that are done as a turn in the janitor's own conversation.
pub fn turn_templates() -> &'static [String] {
    &catalogue().turn_templates
}

/// The jobs a Host installs a janitor of its own for.
pub fn roles() -> &'static [String] {
    &catalogue().roles
}

/// The jobs whose runs may answer one request: the Host's own, and the one a Host has only
/// where its owner makes it (the switching of account seats).
pub fn demand_roles() -> impl Iterator<Item = &'static String> {
    let listed = catalogue();
    listed.roles.iter().chain(&listed.optional_roles)
}

/// The id the janitor a Host installs for a job has, on every Host: the Python Host makes
/// it from the job's name, so that the same janitor is the same agent wherever it is.
pub fn own_id(role: &str) -> Option<&'static str> {
    catalogue().own_ids.get(role).and_then(Value::as_str)
}

/// The picture a janitor with this job is shown with, by its file's name.
pub fn portrait(template_id: &str) -> &'static str {
    let named = catalogue().portraits.get(template_id);
    named
        .and_then(Value::as_str)
        .unwrap_or("janitor-default.jpg")
}

/// One job, by its id.
pub fn template(id: &str) -> Result<&'static Value, Refused> {
    let found = templates().iter().find(|template| template["id"] == id);
    found.ok_or_else(|| Refused::new(400, "unsupported_template", "Unsupported maintenance job"))
}

/// The triggers a job can be started by.
pub fn supported_triggers(template: &Value) -> Vec<&str> {
    let ids = template["supported_trigger_ids"].as_array();
    ids.map(|ids| ids.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

fn options_of(template: &'static Value) -> &'static [Value] {
    template["options"].as_array().map_or(&[], Vec::as_slice)
}

fn described<'a>(options: &'a [Value], key: &str) -> Option<&'a Value> {
    options.iter().find(|option| option["key"] == key)
}

/// A whole number, as Python has one: no fraction written, and neither yes nor no.
fn whole(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) if number.is_i64() || number.is_u64() => number.as_i64(),
        _ => None,
    }
}

fn no_control(text: &str, but: &[char]) -> bool {
    !text
        .chars()
        .any(|sign| u32::from(sign) < 32 && !but.contains(&sign))
}

/// Whether a value is one an option takes.
fn valid_option(option: &Value, value: &Value) -> bool {
    match option["type"].as_str() {
        Some("boolean") => value.is_boolean(),
        Some(kind @ ("integer" | "number")) => {
            let Value::Number(number) = value else {
                return false;
            };
            if kind == "integer" && whole(value).is_none() {
                return false;
            }
            let Some(given) = number.as_f64() else {
                return false;
            };
            let least = option.get("min").and_then(Value::as_f64);
            let most = option.get("max").and_then(Value::as_f64);
            given.is_finite()
                && least.is_none_or(|least| least <= given)
                && most.is_none_or(|most| given <= most)
        }
        Some("string") => value
            .as_str()
            .is_some_and(|text| text.chars().count() <= SHORT_MOST && no_control(text, &[])),
        Some("text") => {
            let most = option.get("max_length").and_then(Value::as_u64);
            let most = usize::try_from(most.unwrap_or(INSTRUCTIONS_MOST)).unwrap_or(usize::MAX);
            value
                .as_str()
                .is_some_and(|text| text.chars().count() <= most && no_control(text, &['\n', '\t']))
        }
        // The same kind of value and the same value: a number with a fraction written is
        // not the whole number, and yes is not one.
        Some("choice") => option["choices"]
            .as_array()
            .is_some_and(|choices| choices.iter().any(|choice| choice["value"] == *value)),
        _ => false,
    }
}

fn invalid_option(key: &str) -> Refused {
    Refused::new(400, "invalid_option", format!("Invalid option: {key}"))
}

/// A job's options as they are in force: what is kept, and for everything that is not kept
/// what the job has by default. What is kept is not thereby chosen by anybody.
pub fn option_values(
    template_id: &str,
    kept: Option<&Value>,
) -> Result<Map<String, Value>, Refused> {
    let kept = match kept {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(kept)) => kept.clone(),
        Some(_) => {
            let why = "Stored options must be an object";
            return Err(Refused::new(400, "invalid_option", why));
        }
    };
    let options = options_of(template(template_id)?);
    for (key, value) in &kept {
        if described(options, key).is_some_and(|option| !valid_option(option, value)) {
            return Err(invalid_option(key));
        }
    }
    let mut in_force = Map::new();
    for option in options {
        if let Some(key) = option["key"].as_str() {
            in_force.insert(key.to_string(), option["default"].clone());
        }
    }
    in_force.extend(kept);
    Ok(in_force)
}

/// What is kept of a job's options once `given` is laid over what was kept: each option the
/// job has is checked, and one it has not is taken only as what is kept of it already.
pub fn option_patch(
    template_id: &str,
    given: &Value,
    kept: Option<&Map<String, Value>>,
) -> Result<Map<String, Value>, Refused> {
    let Some(given) = given.as_object() else {
        let why = "Job options must be an object";
        return Err(Refused::new(400, "invalid_option", why));
    };
    let mut result = kept.cloned().unwrap_or_default();
    let options = options_of(template(template_id)?);
    for (key, value) in given {
        match described(options, key) {
            Some(option) if !valid_option(option, value) => return Err(invalid_option(key)),
            Some(_) => {}
            None if result.get(key) == Some(value) => {}
            None => {
                let why = format!("Unsupported option: {key}");
                return Err(Refused::new(400, "unsupported_option", why));
            }
        }
    }
    result.extend(given.clone());
    option_values(template_id, Some(&Value::Object(result.clone())))?;
    Ok(result)
}

/// One trigger of a janitor, as it is kept.
#[derive(Debug, Clone, PartialEq)]
pub struct Attachment {
    pub attachment_id: String,
    pub trigger_id: String,
    pub trigger_version: i64,
    pub enabled: bool,
    pub config: Map<String, Value>,
}

impl Attachment {
    pub fn shown(&self) -> Value {
        json!({
            "attachment_id": self.attachment_id, "trigger_id": self.trigger_id,
            "trigger_version": self.trigger_version, "enabled": self.enabled,
            "config": self.config,
        })
    }
}

/// What the rules for attachments ask of whoever keeps them.
pub trait Kept {
    /// Whether an attachment of this id may be this agent's: nobody has it, or this agent
    /// has it and it was not taken away.
    fn attachment_free(&self, attachment_id: &str, agent_id: &str) -> bool;
    /// Whether a time zone of this name is one the machine has.
    fn zone(&self, name: &str) -> bool;
    /// An id for an attachment that names none.
    fn draw(&mut self) -> String;
}

fn definition(trigger_id: &str, version: i64) -> Option<&'static Value> {
    let mut all = triggers().iter();
    all.find(|trigger| trigger["trigger_id"] == trigger_id && trigger["version"] == version)
}

fn in_range(config: &Map<String, Value>, field: &str, least: i64, most: i64) -> bool {
    config
        .get(field)
        .and_then(whole)
        .is_some_and(|given| least <= given && given <= most)
}

/// The attachments a request asks for, each with what its trigger has by default under
/// what the request says of it; or why they are not taken.
pub fn attachment_values(
    given: &Value,
    agent_id: &str,
    template_id: &str,
    kept: &mut dyn Kept,
) -> Result<Vec<Attachment>, Refused> {
    let given = given.as_array().map_or(&[][..], Vec::as_slice);
    if given.is_empty() || given.len() > MOST_TRIGGERS {
        return Err(Refused::invalid("Choose between one and eight triggers"));
    }
    let mut taken: Vec<Attachment> = Vec::new();
    for value in given {
        let Some(value) = value.as_object() else {
            return Err(Refused::invalid("Invalid trigger configuration"));
        };
        let version = match value.get("trigger_version") {
            None => Some(1),
            Some(version) => whole(version),
        };
        let (Some(trigger_id), Some(version)) =
            (value.get("trigger_id").and_then(Value::as_str), version)
        else {
            return Err(Refused::invalid("Invalid trigger identity/version"));
        };
        let Some(definition) = definition(trigger_id, version) else {
            let why = "Unsupported trigger version";
            return Err(Refused::new(400, "unsupported_trigger", why));
        };
        if !supported_triggers(template(template_id)?).contains(&trigger_id) {
            let why = "This trigger is not supported by the maintenance job";
            return Err(Refused::new(400, "incompatible_trigger", why));
        }
        // An id that says nothing is none: the Host draws one.
        let said_nothing = |id: &Value| match id {
            Value::Null | Value::Bool(false) => true,
            Value::String(id) => id.is_empty(),
            Value::Number(number) => number.as_f64() == Some(0.0),
            Value::Array(items) => items.is_empty(),
            Value::Object(fields) => fields.is_empty(),
            Value::Bool(true) => false,
        };
        let attachment_id = match value.get("attachment_id") {
            Some(id) if !said_nothing(id) => id.as_str().map(str::to_string),
            _ => Some(kept.draw()),
        };
        let attachment_id = attachment_id.filter(|id| {
            id.chars().count() <= ATTACHMENT_ID_MOST
                && !taken.iter().any(|other| other.attachment_id == *id)
        });
        let Some(attachment_id) = attachment_id else {
            return Err(Refused::invalid("Invalid or duplicate trigger attachment"));
        };
        if !kept.attachment_free(&attachment_id, agent_id) {
            let why = "Trigger attachment belongs to another configuration";
            return Err(Refused::new(409, "attachment_conflict", why));
        }
        let said = match value.get("config") {
            None => Map::new(),
            Some(Value::Object(said)) => said.clone(),
            Some(_) => return Err(Refused::invalid("Trigger settings must be an object")),
        };
        let mut config = definition["defaults"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        config.extend(said);
        let kind = definition["kind"].as_str().unwrap_or_default();
        let mut allowed: Vec<&str> = match kind {
            "demand" => Vec::new(),
            _ => vec!["max_targets", "coalesce_seconds", "min_interval_seconds"],
        };
        if kind == "schedule" {
            allowed.extend(["cron", "timezone"]);
        }
        let while_used = trigger_id == "active-interval";
        if while_used {
            allowed.extend(["interval_seconds", "idle_timeout_seconds", "run_on_resume"]);
        }
        if config.keys().any(|key| !allowed.contains(&key.as_str())) {
            return Err(Refused::invalid("Unsupported trigger setting"));
        }
        if while_used {
            for field in ["interval_seconds", "idle_timeout_seconds"] {
                if !in_range(&config, field, 60, 86_400) {
                    let why = format!("{field} must be between 60 and 86400 seconds");
                    return Err(Refused::invalid(why));
                }
            }
            if !config.get("run_on_resume").is_some_and(Value::is_boolean) {
                return Err(Refused::invalid("run_on_resume must be boolean"));
            }
        }
        for (field, least, most) in [
            ("max_targets", 1, 3),
            ("coalesce_seconds", 0, 300),
            ("min_interval_seconds", 0, 3600),
        ] {
            if config.contains_key(field) && !in_range(&config, field, least, most) {
                let why = format!("Invalid {}", field.replace('_', " "));
                return Err(Refused::invalid(why));
            }
        }
        if kind == "schedule" {
            let expression = config.get("cron").and_then(Value::as_str);
            let zone = config.get("timezone").and_then(Value::as_str);
            let sound = expression.is_some_and(|expression| schedules::parse(expression).is_ok())
                && zone.is_some_and(|zone| kept.zone(zone));
            if !sound {
                let why = "Use a valid five-field schedule and IANA timezone";
                return Err(Refused::invalid(why));
            }
        }
        let enabled = match value.get("enabled") {
            None => true,
            Some(Value::Bool(enabled)) => *enabled,
            Some(_) => return Err(Refused::invalid("Trigger enabled must be a boolean")),
        };
        taken.push(Attachment {
            attachment_id,
            trigger_id: trigger_id.to_string(),
            trigger_version: version,
            enabled,
            config,
        });
    }
    Ok(taken)
}

/// The agents a janitor watches: the ones it names, or all when it names none, without the
/// ones it leaves out.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Scope {
    pub agent_ids: Vec<String>,
    pub exclude_agent_ids: Vec<String>,
}

impl Scope {
    pub fn shown(&self) -> Value {
        json!({ "agent_ids": self.agent_ids, "exclude_agent_ids": self.exclude_agent_ids })
    }

    /// Whether two scopes have an agent in common: one of today's, or one that may come.
    pub fn overlaps(&self, other: &Self) -> bool {
        let (ours, theirs) = (&self.agent_ids, &other.agent_ids);
        if ours.is_empty() && theirs.is_empty() {
            // Both watch every agent, also the ones that are made later.
            return true;
        }
        let left_out = |id: &String| {
            self.exclude_agent_ids.contains(id) || other.exclude_agent_ids.contains(id)
        };
        let both: Vec<&String> = match (ours.is_empty(), theirs.is_empty()) {
            (false, false) => ours.iter().filter(|id| theirs.contains(id)).collect(),
            (false, true) => ours.iter().collect(),
            _ => theirs.iter().collect(),
        };
        both.into_iter().any(|id| !left_out(id))
    }
}

/// What a scope has to know of an agent it names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Named {
    pub is_janitor: bool,
}

/// The scope a request asks for, or why it is not taken. `agent` answers for an id whether
/// an agent has it, and whether that agent is a janitor itself.
pub fn scope(given: &Value, agent: &dyn Fn(&str) -> Option<Named>) -> Result<Scope, Refused> {
    let sides = ["agent_ids", "exclude_agent_ids"];
    let Some(given) = given
        .as_object()
        .filter(|given| given.keys().all(|key| sides.contains(&key.as_str())))
    else {
        let why = "Scope must contain agent_ids and exclude_agent_ids";
        return Err(Refused::invalid(why));
    };
    let mut taken: [Vec<String>; 2] = [Vec::new(), Vec::new()];
    for (side, into) in sides.iter().zip(taken.iter_mut()) {
        let named: Option<BTreeSet<&str>> = match given.get(*side) {
            None => Some(BTreeSet::new()),
            Some(Value::Array(entries)) if entries.len() <= MOST_IN_SCOPE => {
                let named = entries.iter().map(|entry| {
                    let named = entry.as_str();
                    named.filter(|id| !crate::python::strip(id).is_empty())
                });
                named.collect()
            }
            Some(_) => None,
        };
        let Some(named) = named else {
            return Err(Refused::invalid("Scope agent IDs must be a bounded list"));
        };
        for id in named {
            let watched = *side == "agent_ids";
            match agent(id) {
                Some(found) if !(watched && found.is_janitor) => into.push(id.to_string()),
                _ => {
                    let why = "Choose existing task agents for the watched scope";
                    return Err(Refused::new(400, "invalid_scope", why));
                }
            }
        }
    }
    let [agent_ids, exclude_agent_ids] = taken;
    Ok(Scope {
        agent_ids,
        exclude_agent_ids,
    })
}

/// How a job is run, as a request asks for it and the job allows: nothing for the job that
/// is a turn of the janitor's own tool, by the Host alone for the one that needs no model,
/// and otherwise one request to a provider, by itself.
pub fn execution(
    template_id: &str,
    given: Option<&Value>,
    backend: &str,
) -> Result<Map<String, Value>, Refused> {
    let given = given.filter(|given| !given.is_null());
    let only = |given: &Value, keys: &[&str]| {
        given
            .as_object()
            .filter(|given| given.keys().all(|key| keys.contains(&key.as_str())))
            .cloned()
    };
    let as_run = |executor: &str, provider: &str| {
        let mut run = Map::new();
        run.insert("executor".to_string(), json!(executor));
        run.insert("provider".to_string(), json!(provider));
        run
    };
    if template_id == "audio-bookkeeper" {
        if given.is_some_and(|given| only(given, &["executor", "provider"]).is_none()) {
            let why = "Invalid deterministic execution configuration";
            return Err(Refused::invalid(why));
        }
        return Ok(as_run("deterministic", "local"));
    }
    if template_id == "task-labels" {
        if given.is_some_and(|given| given.as_object().is_none_or(|given| !given.is_empty())) {
            let why = "Task labels use the managed agent executor";
            return Err(Refused::invalid(why));
        }
        return Ok(Map::new());
    }
    let asked = match given {
        None => as_run("ephemeral", backend),
        Some(given) => only(given, &["executor", "provider", "isolated"])
            .ok_or_else(|| Refused::invalid("Invalid demand execution configuration"))?,
    };
    let facts = catalogue();
    let provider = match asked.get("provider") {
        None => Some(backend),
        Some(provider) => provider.as_str(),
    };
    let isolated = match asked.get("isolated") {
        None => Some(false),
        Some(isolated) => isolated.as_bool(),
    };
    let can_isolate =
        provider.is_some_and(|provider| facts.isolating.iter().any(|id| id == provider));
    let Some(isolated) = isolated.filter(|isolated| !*isolated || can_isolate) else {
        // Antigravity today: a home with nothing in it, an empty directory, a time limit.
        let why = "Isolated execution is an Antigravity option";
        return Err(Refused::invalid(why));
    };
    let executor = asked
        .get("executor")
        .map_or(Some("ephemeral"), Value::as_str);
    let (Some("ephemeral"), Some(provider)) = (executor, provider) else {
        return Err(Refused::invalid(
            "Demand jobs require an ephemeral executor",
        ));
    };
    if provider != "openai" && !facts.routing.iter().any(|id| id == provider) {
        let why = "Unsupported demand execution provider";
        return Err(Refused::invalid(why));
    }
    let stands_for = facts.backend_of.get(provider).and_then(Value::as_str);
    if backend != stands_for.unwrap_or(provider) {
        let why = "Execution provider must match the Janitor backend";
        return Err(Refused::invalid(why));
    }
    if template_id == "tool-explainer" && provider != backend {
        let why = "Tool explanation runs on the Janitor's own backend";
        return Err(Refused::invalid(why));
    }
    let mut run = as_run("ephemeral", provider);
    if isolated {
        run.insert("isolated".to_string(), json!(true));
    }
    Ok(run)
}
