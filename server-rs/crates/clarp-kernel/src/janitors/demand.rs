//! Runs of a janitor that answer one request.
//!
//! Two of a Host's own janitors do not run at set times: the one that explains the use of
//! a tool and the one that finds whom a spoken message is for answer one request each.
//! Such a request is a run of its janitor, and nothing here calls a model. Whoever serves
//! the request asks which janitor answers ([`Book::resolve`]), begins the run under an id
//! made from the request's own ([`Book::begin_run`]: the same request is the same run,
//! never a second call of a model), takes the claim once before it calls one
//! ([`Book::claim_run`]), and says what came of it ([`Book::complete_run`]: kept once, and
//! only while the janitor is still as it was when the run began). A run nobody finishes
//! is given up after three minutes ([`Book::recover_expired`]).
//!
//! What a run keeps of its request and of its result is a few counts and short words,
//! never what was said or what a tool did.
//!
//! The rules are the Python Host's (`server/lib/janitor_builtins.py`), held step by step by
//! `tests/fixtures/janitor-demand-vectors.json`
//! (`server-rs/tools/janitor-demand-vectors.py`).

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::book::{Agent, Book, World, model_fits, refused};
use super::runs::{Run, activity_values};
use super::{Refused, demand_roles, execution, option_values, template};

/// How long a run that answers a request may be out before it is given up.
pub const GIVEN_UP_AFTER_MS: i64 = 180_000;
/// How long the id of a request and a text of what is kept may be, and how large a number.
const REQUEST_MOST: usize = 160;
const TEXT_MOST: usize = 500;
const NUMBER_MOST: i64 = 1_000_000_000_000;
/// What a request and a result may say of themselves.
const SAID: [&str; 18] = [
    "input_hash",
    "request_hash",
    "candidate_count",
    "item_count",
    "detail_level",
    "target_count",
    "target_agent_id",
    "policy_revision",
    "summary",
    "status",
    "reason",
    "decision_id",
    "http_status",
    // What a routing run's first answer said, so that the same request is answered the same.
    "answer_session",
    "answer_dispatch",
    "answer_status",
    "decision_target",
    "decision_action",
];
const GIVEN_UP: &str = "Demand run expired before completion";
const EPHEMERAL: &str = "ephemeral";

fn invalid(why: &str) -> Refused {
    Refused::new(400, "invalid_janitor", why)
}

/// A job that answers requests, or the refusal for a name that is none.
fn role_of(role: &str) -> Result<&str, Refused> {
    let known = demand_roles().any(|known| known == role);
    known
        .then_some(role)
        .ok_or_else(|| invalid("Unsupported built-in Janitor role"))
}

/// What a request or a result says of itself: counts and short words under a few names.
fn said(value: Option<&Value>) -> Result<Map<String, Value>, Refused> {
    let bounded = || invalid("Demand history accepts bounded metadata only");
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(Map::new());
    };
    let fields = value.as_object().ok_or_else(bounded)?;
    for (name, item) in fields {
        let fits = match item {
            Value::Null | Value::Bool(_) => true,
            Value::String(text) => text.chars().count() <= TEXT_MOST,
            Value::Number(number) => number
                .as_i64()
                .is_some_and(|number| number.abs() <= NUMBER_MOST),
            Value::Array(_) | Value::Object(_) => false,
        };
        if !SAID.contains(&name.as_str()) || !fits {
            return Err(bounded());
        }
    }
    Ok(fields.clone())
}

/// Whether the history takes this as what a request or a result says of itself: named fields of
/// short words, numbers and yes or no (the Python Host's `fits_metadata`).
pub fn fits(value: &Value) -> bool {
    said(Some(value)).is_ok()
}

/// The id of the run of a request: the same for the same job and request.
pub fn request_run_id(role: &str, request_id: &Value) -> Result<String, Refused> {
    let role = role_of(role)?;
    let id = request_id.as_str();
    let id = id.filter(|id| (1..=REQUEST_MOST).contains(&id.chars().count()));
    let id = id.ok_or_else(|| invalid("Invalid demand request identity"))?;
    let digest = Sha256::digest(format!("{role}:{id}").as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!("janitor-demand-{hex}"))
}

/// Whether a janitor with this scope answers for an agent.
pub fn in_scope(scope: &Map<String, Value>, target: &Agent) -> bool {
    let named = |side: &str| scope.get(side).and_then(Value::as_array);
    let id = json!(target.agent_id);
    let only = named("agent_ids").is_none_or(|only| only.is_empty() || only.contains(&id));
    let left_out = named("exclude_agent_ids").is_some_and(|never| never.contains(&id));
    !target.is_janitor && !target.archived && only && !left_out
}

/// When a run that answers a request is given up.
fn expires_at(run: &Run) -> i64 {
    let said = run.configuration.get("expires_at").and_then(Value::as_i64);
    said.unwrap_or(run.created_at + GIVEN_UP_AFTER_MS)
}

fn answers_a_request(run: &Run) -> bool {
    run.configuration.get("executor").and_then(Value::as_str) == Some(EPHEMERAL)
}

impl Book {
    /// The janitors that may answer requests of a job: each on, with the job's own trigger
    /// on once, and a way to run it that holds.
    pub fn eligible(&self, role: &str, world: &dyn World) -> Vec<String> {
        let first = template(role).ok();
        let first = first.and_then(|job| job["default_trigger_id"].as_str());
        let mut found = Vec::new();
        for (agent_id, config) in &self.configs {
            let Some(agent) = world.agent(agent_id) else {
                continue;
            };
            if config.template_id != role || !config.enabled {
                continue;
            }
            let own = self.triggers_of(agent_id).into_iter();
            let matching = own.filter(|(_, kept)| {
                kept.enabled && kept.trigger_version == 1 && Some(kept.trigger_id.as_str()) == first
            });
            if matching.count() != 1 {
                continue;
            }
            let as_kept = Value::Object(config.execution.clone());
            let Ok(run) = execution(role, Some(&as_kept), &agent.backend) else {
                continue;
            };
            let provider = run.get("provider").and_then(Value::as_str);
            let (model, effort) = (json!(agent.model), json!(agent.effort));
            if model_fits(world, &agent.backend, Some(&model), Some(&effort), provider).is_ok() {
                found.push(agent_id.clone());
            }
        }
        found
    }

    /// The janitor that answers requests of a job for an agent (`target`: its id), or for
    /// nobody in particular: the one whose scope has the agent, or the one that watches
    /// everyone. Where none does, or more than one, nobody answers.
    pub fn resolve(
        &self,
        role: &str,
        target: Option<&str>,
        world: &dyn World,
    ) -> Result<Option<String>, Refused> {
        let role = role_of(role)?;
        let target = match target {
            None => None,
            Some(id) => match world.agent(id).filter(|agent| agent.agent_id == id) {
                Some(agent) => Some(agent),
                None => return Ok(None),
            },
        };
        let mut chosen = self.eligible(role, world);
        chosen.retain(|agent_id| {
            let Some(config) = self.configs.get(agent_id) else {
                return false;
            };
            let listed = |side: &str| {
                let side = config.scope.get(side).and_then(Value::as_array);
                side.is_some_and(|side| !side.is_empty())
            };
            match &target {
                Some(target) => in_scope(&config.scope, target),
                None => !listed("agent_ids") && !listed("exclude_agent_ids"),
            }
        });
        Ok(match chosen.as_slice() {
            [one] => Some(one.clone()),
            _ => None,
        })
    }

    /// A run that was claimed and whose claim is not over, within its time: its janitor is
    /// busy with it, also when it was paused meanwhile.
    pub(super) fn pending_claim(&self, agent_id: &str, now_ms: i64) -> bool {
        self.runs.values().any(|run| {
            let claimed = run.claimed_at.is_some() && run.claim_finished_at.is_none();
            run.agent_id == agent_id && claimed && expires_at(run) > now_ms
        })
    }

    /// The run of a request, by the request's id.
    pub fn request_run(&self, role: &str, request_id: &Value) -> Result<Option<Value>, Refused> {
        let run_id = request_run_id(role, request_id)?;
        Ok(self.runs.get(&run_id).map(Run::shown))
    }

    /// Begins the run of a request, or answers the run the request has. `None`: nobody
    /// answers requests of this job for this agent right now (no janitor does, or the one
    /// that does is at another request).
    pub fn begin_run(
        &mut self,
        role: &str,
        request_id: &Value,
        context: Option<&Value>,
        target: Option<&str>,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<Option<Value>, Refused> {
        let janitor = self.resolve(role, target, world)?;
        self.begin_run_of(janitor, role, request_id, context, target, world, now_ms)
    }

    /// Begins the run of a request whose janitor is already found (`None`: nobody answers),
    /// or answers the run the request has. For whoever knows whose run it is: the Goal
    /// keeper's run is the run of the keeper whose goals are due, whatever its scope, where
    /// a request that names no agent finds only a janitor that watches everyone (the Python
    /// Host's defect R42: a keeper with a scope never runs there).
    #[allow(clippy::too_many_arguments)]
    pub fn begin_run_of(
        &mut self,
        janitor: Option<String>,
        role: &str,
        request_id: &Value,
        context: Option<&Value>,
        target: Option<&str>,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<Option<Value>, Refused> {
        let run_id = request_run_id(role, request_id)?;
        let context = said(context)?;
        if let Some(known) = self.runs.get(&run_id) {
            let kept = |key: &str| known.configuration.get(key).unwrap_or(&Value::Null);
            if *kept("context") != Value::Object(context)
                || *kept("target_agent_id") != json!(target)
            {
                let why = "Request identity already refers to different work";
                return Err(refused(409, "run_conflict", why));
            }
            return Ok(Some(known.shown()));
        }
        self.recover_expired(now_ms);
        let Some(agent_id) = janitor else {
            return Ok(None);
        };
        let (Some(config), Some(agent)) = (self.configs.get(&agent_id), world.agent(&agent_id))
        else {
            return Ok(None);
        };
        let word = |key: &str| config.execution.get(key).and_then(Value::as_str);
        let busy = self.out_of(&agent_id).is_some() || self.pending_claim(&agent_id, now_ms);
        if word("executor") != Some(EPHEMERAL) || busy {
            return Ok(None);
        }
        let first = template(role)?["default_trigger_id"].as_str();
        let own = self.triggers_of(&agent_id).into_iter();
        let mut own =
            own.filter(|(_, kept)| kept.enabled && Some(kept.trigger_id.as_str()) == first);
        let Some((attachment_id, trigger)) = own.next() else {
            return Ok(None);
        };
        let kept = Value::Object(config.options.clone());
        let frozen = json!({
            "template_id": role, "executor": EPHEMERAL, "provider": word("provider"),
            "isolated": config.execution.get("isolated") == Some(&json!(true)),
            "backend": agent.backend, "model": agent.model, "effort": agent.effort,
            "scope": config.scope, "options": option_values(role, Some(&kept))?,
            "trigger_id": trigger.trigger_id, "trigger_version": trigger.trigger_version,
            "config": trigger.config, "context": context, "target_agent_id": target,
            "expires_at": now_ms + GIVEN_UP_AFTER_MS,
            "effective_chain": self.model_policy.effective(&agent.session,&agent,config),
        });
        let run = Run {
            run_id: run_id.clone(),
            agent_id: agent_id.clone(),
            session: agent.session,
            attachment_id: attachment_id.clone(),
            generation: config.generation,
            trace_id: run_id.clone(),
            status: "running".to_string(),
            configuration: frozen.as_object().cloned().unwrap_or_default(),
            created_at: now_ms,
            started_at: Some(now_ms),
            ..Run::default()
        };
        let shown = run.shown();
        self.runs.insert(run_id, run);
        Ok(Some(shown))
    }

    /// Gives up every run that answers a request and was not finished in its time.
    /// Answers how many.
    pub fn recover_expired(&mut self, now_ms: i64) -> usize {
        let late = |run: &&Run| run.out() && answers_a_request(run) && expires_at(run) <= now_ms;
        let late: Vec<(String, String)> = self
            .runs
            .values()
            .filter(late)
            .map(|run| (run.run_id.clone(), run.agent_id.clone()))
            .collect();
        for (run_id, agent_id) in &late {
            if let Some(run) = self.runs.get_mut(run_id) {
                run.cut_off(now_ms);
                run.error = GIVEN_UP.to_string();
            }
            if let Some(config) = self.configs.get_mut(agent_id) {
                config.last_run_at = Some(now_ms);
                config.last_error = GIVEN_UP.to_string();
                config.updated_at = now_ms;
            }
        }
        late.len()
    }

    /// Whether a run that answers a request may still act: it is out and within its time,
    /// and its janitor, the janitor's tool, job, scope, options and trigger are as they
    /// were when the run began.
    pub fn is_current(&self, run_id: &str, world: &dyn World, now_ms: i64) -> bool {
        let Ok(run) = self.acting_run(run_id, world) else {
            return false;
        };
        let frozen = |key: &str| run.configuration.get(key).unwrap_or(&Value::Null);
        let job = frozen("template_id").as_str().unwrap_or_default();
        if !answers_a_request(run) || role_of(job).is_err() || now_ms >= expires_at(run) {
            return false;
        }
        let (Some(config), Some(agent)) =
            (self.configs.get(&run.agent_id), world.agent(&run.agent_id))
        else {
            return false;
        };
        let same_tool = json!(agent.backend) == *frozen("backend")
            && json!(agent.model) == *frozen("model")
            && json!(agent.effort) == *frozen("effort")
            && config.template_id == job;
        let mut run_by = json!({ "executor": EPHEMERAL, "provider": frozen("provider") });
        if *frozen("isolated") == json!(true) {
            run_by["isolated"] = json!(true);
        }
        let same_job = Value::Object(config.execution.clone()) == run_by
            && Value::Object(config.scope.clone()) == *frozen("scope");
        let same_trigger = self.triggers.get(&run.attachment_id).is_some_and(|kept| {
            json!(kept.trigger_id) == *frozen("trigger_id")
                && json!(kept.trigger_version) == *frozen("trigger_version")
                && Value::Object(kept.config.clone()) == *frozen("config")
        });
        if !same_tool || !same_job || !same_trigger {
            return false;
        }
        // The agent the request was for is still one its janitor answers for.
        match frozen("target_agent_id")
            .as_str()
            .filter(|id| !id.is_empty())
        {
            None => true,
            Some(id) => {
                let scope = frozen("scope").as_object();
                let target = world.agent(id);
                scope
                    .zip(target)
                    .is_some_and(|(scope, target)| in_scope(scope, &target))
            }
        }
    }

    /// Takes the claim of a run: once, before a model is called for it. Whoever has the
    /// claim calls; whoever does not, waits for the result.
    pub fn claim_run(&mut self, run_id: &str, world: &dyn World, now_ms: i64) -> bool {
        if !self.is_current(run_id, world, now_ms) {
            return false;
        }
        match self.runs.get_mut(run_id) {
            Some(run) if run.claimed_at.is_none() => {
                run.claimed_at = Some(now_ms);
                true
            }
            _ => false,
        }
    }

    /// What came of a run: kept once, and only while the run may still act. A result that
    /// comes when its janitor was paused or set anew is thrown away, and the run is cut
    /// off. `reported`: what the run says of itself, `worked`, `idle` or `failed`, and one
    /// line. Answers whether the result was kept.
    #[allow(clippy::too_many_arguments)]
    pub fn complete_run(
        &mut self,
        run_id: &str,
        outcome: &str,
        result: Option<&Value>,
        error: &str,
        reported: Option<(&str, &str)>,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<bool, Refused> {
        if !["completed", "failed", "cancelled"].contains(&outcome) {
            return Err(invalid("Invalid demand outcome"));
        }
        let reported = match reported {
            Some((activity, summary)) => Some(activity_values(activity, summary)?),
            None => None,
        };
        let result = Value::Object(said(result)?);
        if error.chars().count() > TEXT_MOST {
            return Err(invalid("Demand errors must be bounded metadata"));
        }
        let Some(run) = self
            .runs
            .get_mut(run_id)
            .filter(|run| answers_a_request(run))
        else {
            return Ok(false);
        };
        // Its claim is over, whatever becomes of the result.
        if run.claimed_at.is_some() {
            run.claim_finished_at.get_or_insert(now_ms);
        }
        if let Some(prior) = &run.demand_result {
            if *prior != result || run.outcome != outcome || run.error != error {
                let why = "Demand result already refers to different work";
                return Err(refused(409, "result_conflict", why));
            }
            return Ok(true);
        }
        let agent_id = run.agent_id.clone();
        if !self.is_current(run_id, world, now_ms) {
            if let Some(run) = self.runs.get_mut(run_id) {
                run.cut_off(now_ms);
            }
            return Ok(false);
        }
        if let Some(run) = self.runs.get_mut(run_id) {
            run.demand_result = Some(result);
            run.status = outcome.to_string();
            run.outcome = outcome.to_string();
            run.finished_at = Some(now_ms);
            run.error = error.to_string();
            if let Some((activity, summary)) = reported {
                run.activity = activity;
                run.activity_summary = summary;
            }
        }
        if let Some(config) = self.configs.get_mut(&agent_id) {
            config.last_run_at = Some(now_ms);
            config.last_error = error.to_string();
            config.updated_at = now_ms;
        }
        self.forget_old(&agent_id);
        Ok(true)
    }
}
