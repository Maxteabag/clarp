//! The runs of a janitor's scheduled task.
//!
//! A janitor whose job is a scheduled task runs when its schedule says: one run for each
//! time that comes due, and each run a turn in the janitor's own conversation. Nothing
//! here starts a turn. The Host looks at each trigger every few seconds ([`Book::look`]),
//! with how the turn of the run that is out ended, if it has; the book answers what is to
//! be handed to the Host's own queue, and is told whether the queue took it
//! ([`Book::handed`]). In between it keeps, for every trigger, what the runner knows
//! ([`Progress`]): when the task runs next, the run that is out, whether its turn was
//! taken, and how long the task waits now.
//!
//! A run that is out belongs to the configuration it was made for. A change of that
//! configuration (a pause, a save, a removal) cuts it off: it may not say anything any
//! more, and its turn may not start.
//!
//! The rules are the Python Host's (`server/lib/janitor_runner.py`, `_scheduled_tick`, and
//! the runs of `server/lib/janitors.py`), held to that runner itself, look by look, by
//! `tests/fixtures/janitor-run-vectors.json` (`server-rs/tools/janitor-run-vectors.py`).
//! In one thing this Host does otherwise, see [`Book::look`]: a task that keeps its pace
//! runs at every time its schedule names.

use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::adaptive::{self, State};
use super::book::{Book, Config, Trigger, World, agent_of, refused};
use super::{Refused, option_values, turn_templates};
use crate::python;

/// How long after the Host's queue did not take a run it is offered again, and how long a
/// run may be out before it is given up.
pub const OFFERED_AGAIN_AFTER_MS: i64 = 30_000;
pub const GIVEN_UP_AFTER_MS: i64 = 2 * 60 * 60 * 1000;
/// How many runs of a janitor are shown at most, how many when nothing says, and how many
/// that are over are kept.
pub const RUNS_MOST: usize = 100;
pub const RUNS_SHOWN: i64 = 30;
/// How much of why a run failed is kept, in signs.
const ERROR_MOST: usize = 500;

const QUEUED: &str = "queued";
const RUNNING: &str = "running";
const COMPLETED: &str = "completed";
const FAILED: &str = "failed";
const CANCELLED: &str = "cancelled";
const ERROR: &str = "error";
/// The job whose runs these are, and the trigger that starts them.
pub const SCHEDULED_TASK: &str = "scheduled-prompt";

const NO_RUN: &str = "Maintenance run not found";
const RUN_OVER: &str = "This maintenance run is no longer active";
const CUT_OFF: &str = "This maintenance run was paused or superseded";
const OTHER_MODEL: &str = "This maintenance run's model policy changed";
const OTHER_OPTIONS: &str = "This maintenance run's options were superseded";
const RUN_MISSING: &str = "The admitted run is missing; configuration requires review";
const GIVEN_UP: &str = "Run expired without a terminal";
const TASK_FAILED: &str = "The scheduled task failed";
const NO_RECEIPTS: &str = "Maintenance ended without all review receipts";

/// One run of a janitor.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Run {
    pub run_id: String,
    pub agent_id: String,
    pub session: String,
    pub attachment_id: String,
    pub generation: u64,
    /// The id its turn has: the run's own.
    pub trace_id: String,
    /// `queued`, `running`, `completed`, `failed` or `cancelled`.
    pub status: String,
    /// Nothing while it is out; then `completed`, `error` or `cancelled`.
    pub outcome: String,
    /// The job as it was when the run was made.
    pub configuration: Map<String, Value>,
    pub created_at: i64,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub error: String,
    /// What the run said of itself: `worked`, `idle` or `failed`, and one line.
    pub activity: String,
    pub activity_summary: String,
}

impl Run {
    /// Whether the run is out: made, and not over.
    pub fn out(&self) -> bool {
        self.status == QUEUED || self.status == RUNNING
    }

    /// Cuts the run off, if it is out: the configuration it was made for is no more.
    pub(super) fn cut_off(&mut self, now_ms: i64) {
        if self.out() {
            self.status = CANCELLED.to_string();
            self.outcome = CANCELLED.to_string();
            self.finished_at = Some(now_ms);
        }
    }

    /// The run as an app reads it.
    pub fn shown(&self) -> Value {
        json!({
            "run_id": self.run_id, "agent_id": self.agent_id, "session": self.session,
            "attachment_id": self.attachment_id, "generation": self.generation,
            "trace_id": self.trace_id, "status": self.status, "outcome": self.outcome,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error, "activity": self.activity,
            "activity_summary": self.activity_summary, "queue_id": self.run_id,
            "client_msg_id": self.run_id, "candidates": [],
            "configuration": self.configuration, "demand_result": null, "results": [],
        })
    }
}

/// One time a schedule came due, from the run made for it until that run is over.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Occurrence {
    /// When it was due, not when a look noticed.
    pub at: i64,
    pub runs: i64,
    pub activities: Vec<String>,
    pub summary: String,
}

/// What the runner keeps of one trigger between two looks.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Progress {
    /// The generation of the configuration this was kept for. Another: all of it is void.
    pub generation: u64,
    pub pending: Map<String, Value>,
    /// When the task runs next. Nothing: its schedule names no time within a year.
    pub next_run_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub active_run_id: Option<String>,
    /// Whether the Host's queue took the turn of the run that is out.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delivery_accepted: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_delivery_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_admitted_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_delivery_error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub occurrence: Option<Occurrence>,
    /// How long the task waits now.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub adaptive: Option<State>,
    /// How far apart two runs of the schedule are, as it was measured once.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cron_base_seconds: Option<i64>,
}

/// How the turn of a run ended.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Ended {
    pub failed: bool,
    /// Why it failed, in the turn's own words. May be nothing.
    pub why: String,
}

/// What the Host's queue answered when it was handed a run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Delivery {
    Taken,
    NotTaken,
    /// The queue broke: the name of what broke.
    Broke(String),
}

/// A run to hand to the Host's queue: a message to the janitor's own conversation, under
/// the run's id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handed {
    pub run_id: String,
    pub session: String,
    pub prompt: String,
}

/// The id of the run of one time a schedule came due: the same for the same trigger,
/// generation and time, so that a look that is repeated makes no second run.
pub fn run_id(attachment_id: &str, generation: u64, due_ms: i64) -> String {
    let name = python::compact_ascii(&json!(attachment_id));
    let named = format!("[{name}, {generation}, \"scheduled\", {due_ms}]");
    let digest = Sha256::digest(named.as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("janitor-{}", &hex[..32])
}

/// What the janitor is told when a run of its scheduled task begins: the owner's
/// instructions, and how the run is closed.
pub fn prompt(run: &Run) -> String {
    let options = run.configuration.get("options");
    let instructions = options.and_then(|options| options["instructions"].as_str());
    let instructions = python::strip(instructions.unwrap_or_default());
    let id = &run.run_id;
    format!(
        "Scheduled Janitor task. Registered run: {id}. Carry out the instructions below, \
         which the owner configured as this Janitor's job. You are a quiet maintenance \
         agent: your replies are kept in your own chat and never notify the owner. Do not \
         create schedules.\n\n{instructions}\n\nFinish by closing the run: clarp-admin \
         janitor outcome {id} --status STATUS --summary SUMMARY, where STATUS is worked (you \
         found and did something), idle (there was nothing to do) or failed (you could not \
         complete it), and SUMMARY is one line. Then give a concise run summary."
    )
}

/// A text with what is plainly a secret taken out: a private key, an API key, the token of
/// an `Authorization` header. What a failed turn said goes through this before it is kept.
pub fn redact(text: &str) -> String {
    static PRIVATE_KEY: LazyLock<Option<Regex>> = LazyLock::new(|| {
        let key = r"(?s)-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\z)";
        Regex::new(key).ok()
    });
    // `sk-` and what follows it, where no letter or digit stands before.
    static API_KEY: LazyLock<Option<Regex>> =
        LazyLock::new(|| Regex::new(r"(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]+").ok());
    static BEARER: LazyLock<Option<Regex>> = LazyLock::new(|| {
        let blank = r"[\s\x1c-\x1f]";
        let header = format!(
            r#"(?i)(\bAuthorization["']?{blank}*[:=]{blank}*["']?Bearer{blank}+)([^\s\x1c-\x1f,;"'<>]+)"#
        );
        Regex::new(&header).ok()
    });
    let (Some(private_key), Some(api_key), Some(bearer)) = (&*PRIVATE_KEY, &*API_KEY, &*BEARER)
    else {
        return String::new();
    };
    let text = private_key.replace_all(text, "[REDACTED PRIVATE KEY]");
    let text = api_key.replace_all(&text, "${1}[REDACTED API KEY]");
    let told = "[REDACTED TOKEN]";
    let text = bearer.replace_all(&text, |found: &Captures<'_>| {
        // A token that was taken out before is left as it stands.
        let after = &text[found.get(2).map_or(0, |token| token.start())..];
        let taken_out = after
            .get(..told.len())
            .is_some_and(|start| start.eq_ignore_ascii_case(told));
        if taken_out {
            found[0].to_string()
        } else {
            format!("{}{told}", &found[1])
        }
    });
    text.into_owned()
}

/// A text on one line: every stretch of blanks one space, none at its ends.
fn one_line(text: &str) -> String {
    let words: Vec<&str> = text
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .collect();
    words.join(" ")
}

impl Book {
    /// The run of a janitor that is out, if one is. A janitor has one at most.
    pub fn out_of(&self, agent_id: &str) -> Option<&Run> {
        let mut own = self.runs.values().filter(|run| run.agent_id == agent_id);
        own.find(|run| run.out())
    }

    /// The job a trigger belongs to, when the trigger may act: it is on and was not taken
    /// away, its janitor is on, in the generation asked for (any, where none is asked
    /// for), and its agent is a janitor that was not put away.
    fn acting(
        &self,
        attachment_id: &str,
        generation: Option<u64>,
        world: &dyn World,
    ) -> Option<&Config> {
        let trigger = self.triggers.get(attachment_id)?;
        let config = self.configs.get(&trigger.agent_id)?;
        let agent = world.agent(&trigger.agent_id)?;
        let acts = trigger.enabled
            && trigger.retired_at.is_none()
            && config.enabled
            && agent.is_janitor
            && !agent.archived;
        (acts && generation.is_none_or(|asked| asked == config.generation)).then_some(config)
    }

    /// The triggers the runner looks at: those that may act, of janitors whose job is a
    /// scheduled task, in the order they were made (those made together, by their ids).
    pub fn scheduled(&self, world: &dyn World) -> Vec<String> {
        let ours = self.triggers.iter().filter(|(id, _)| {
            let job = self.acting(id, None, world);
            job.is_some_and(|config| config.template_id == SCHEDULED_TASK)
        });
        let mut ours: Vec<(i64, &String)> = ours.map(|(id, kept)| (kept.created_at, id)).collect();
        ours.sort();
        ours.into_iter().map(|(_, id)| id.clone()).collect()
    }

    /// The run that is out for a trigger, as the runner keeps it.
    pub fn out_for(&self, attachment_id: &str) -> Option<&Run> {
        let out = self.progress.get(attachment_id)?.active_run_id.as_ref()?;
        self.runs.get(out).filter(|run| run.out())
    }

    /// Keeps what the runner knows of a trigger, and says on the trigger when it runs next.
    fn keep(&mut self, attachment_id: &str, state: Progress) {
        if let Some(trigger) = self.triggers.get_mut(attachment_id) {
            trigger.next_run_at = state.next_run_at;
        }
        self.progress.insert(attachment_id.to_string(), state);
    }

    /// One look at one trigger of a scheduled task. `ended`: how the turn of the run that
    /// is out ended, if it has. Answers the run to hand to the Host's queue, if there is
    /// one to hand over now; the Host says what the queue answered with [`Book::handed`].
    ///
    /// A time that is due and has no run gets one: one run for the time, never one for
    /// each time that was missed. A run that is out is offered until the queue takes it,
    /// and ends when its turn has: `completed`, or `error` with why. A run whose turn
    /// never ends is given up after two hours. When the run of a time is over, what it
    /// said of itself sets how long the task waits ([`adaptive`]), and from that when it
    /// runs next.
    ///
    /// **Where this Host does otherwise.** The Python Host asks, always, for the first
    /// time of the schedule that is at least the wait after the time that ran, and takes
    /// as the wait of a task that keeps its pace the gap between the two runs that
    /// followed its first. Where the schedule's times are not evenly apart, that gap is
    /// longer than the one that follows, and times are passed over: a task of working
    /// days whose first run was a Thursday runs on Mondays and Thursdays only, a daily
    /// one first run two days before the clocks are put back runs every other day, and
    /// every daily one loses the day the clocks are put forward (`docs/reference-defects.md`,
    /// R31). Here a task that keeps its pace runs at the next time its schedule names;
    /// only a wait that idle runs have stretched is counted from the time that ran.
    pub fn look(
        &mut self,
        attachment_id: &str,
        ended: Option<&Ended>,
        world: &dyn World,
        now_ms: i64,
    ) -> Option<Handed> {
        let config = self.acting(attachment_id, None, world)?.clone();
        if config.template_id != SCHEDULED_TASK {
            return None;
        }
        let trigger = self.triggers.get(attachment_id)?.clone();
        let said = |key: &str| trigger.config.get(key).and_then(Value::as_str);
        let (cron, zone) = (said("cron")?, said("timezone")?);
        // What was kept for another generation of the job is void: the task starts anew.
        let kept = self.progress.get(attachment_id);
        let kept = kept.filter(|state| state.generation == config.generation);
        let mut state = kept.cloned().unwrap_or_else(|| Progress {
            generation: config.generation,
            next_run_at: world.next_run(cron, zone, now_ms),
            ..Progress::default()
        });
        if let Some(out) = state.active_run_id.clone() {
            let Some(run) = self.runs.get(&out).cloned() else {
                state.last_error = Some(RUN_MISSING.to_string());
                self.keep(attachment_id, state);
                return None;
            };
            if run.out() {
                let given_up = now_ms - run.created_at >= GIVEN_UP_AFTER_MS;
                let given_up = given_up.then(|| Ended {
                    failed: true,
                    why: GIVEN_UP.to_string(),
                });
                let Some(ended) = ended.cloned().or(given_up) else {
                    self.keep(attachment_id, state.clone());
                    return self.offer(&state, &run, world, now_ms);
                };
                let (outcome, why) = if ended.failed {
                    let why = if ended.why.is_empty() {
                        TASK_FAILED
                    } else {
                        &ended.why
                    };
                    (ERROR, redact(why))
                } else {
                    (COMPLETED, String::new())
                };
                // A run that may not end any more (its configuration is another) stays
                // as it is, and this look changes nothing.
                self.finish(&run.run_id, outcome, &why, world, now_ms)
                    .ok()?;
            }
            state.active_run_id = None;
            state.delivery_accepted = None;
            if let Some(occurrence) = state.occurrence.as_mut()
                && run.status != CANCELLED
            {
                let over = self.runs.get(&run.run_id).unwrap_or(&run);
                let (activity, summary, _) = adaptive::outcome(
                    &over.status,
                    &over.outcome,
                    &over.activity,
                    &over.activity_summary,
                    &over.error,
                );
                occurrence.activities.push(activity.to_string());
                if !summary.is_empty() {
                    occurrence.summary = summary;
                }
            }
            self.close(&config, &mut state, (cron, zone), world, now_ms);
            self.keep(attachment_id, state);
            return None;
        }
        let due = state.next_run_at.filter(|due| now_ms >= *due);
        let free = self.out_of(&trigger.agent_id).is_none();
        let Some(due) = due.filter(|_| free) else {
            self.keep(attachment_id, state);
            return None;
        };
        // One run for this time. When the task runs after it is set when the run is over;
        // until then, the schedule's next time stands in.
        state.next_run_at = world.next_run(cron, zone, now_ms);
        let occurrence = state.occurrence.get_or_insert_with(|| Occurrence {
            at: due,
            ..Occurrence::default()
        });
        occurrence.runs = 1;
        let id = run_id(attachment_id, config.generation, due);
        state.active_run_id = Some(id.clone());
        state.delivery_accepted = Some(false);
        state.next_delivery_at = Some(now_ms);
        state.last_admitted_at = Some(now_ms);
        let run = self.admit(attachment_id, &trigger, &config, id, world, now_ms)?;
        self.keep(attachment_id, state.clone());
        self.offer(&state, &run, world, now_ms)
    }

    /// Makes the run of a time that is due, as the job stands now. A run of this id that
    /// is there already is that run.
    fn admit(
        &mut self,
        attachment_id: &str,
        trigger: &Trigger,
        config: &Config,
        id: String,
        world: &dyn World,
        now_ms: i64,
    ) -> Option<Run> {
        if let Some(known) = self.runs.get(&id) {
            let same =
                known.attachment_id == attachment_id && known.generation == config.generation;
            return same.then(|| known.clone());
        }
        let agent = world.agent(&trigger.agent_id)?;
        let kept = Value::Object(config.options.clone());
        let options = option_values(&config.template_id, Some(&kept)).ok()?;
        let frozen = json!({
            "template_id": config.template_id, "scope": config.scope,
            "trigger_id": trigger.trigger_id, "trigger_version": trigger.trigger_version,
            "config": trigger.config, "effective_chain": chain(&agent.backend, &agent.model, config),
            "options": options,
        });
        let run = Run {
            run_id: id.clone(),
            agent_id: trigger.agent_id.clone(),
            session: agent.session,
            attachment_id: attachment_id.to_string(),
            generation: config.generation,
            trace_id: id.clone(),
            status: QUEUED.to_string(),
            configuration: frozen.as_object().cloned().unwrap_or_default(),
            created_at: now_ms,
            ..Run::default()
        };
        self.runs.insert(id, run.clone());
        Some(run)
    }

    /// The run to hand over now, if it is to be: its turn was not taken yet, the time to
    /// offer it again has come, and it may still start.
    fn offer(&self, state: &Progress, run: &Run, world: &dyn World, now_ms: i64) -> Option<Handed> {
        let taken = state.delivery_accepted == Some(true);
        if taken || now_ms < state.next_delivery_at.unwrap_or(0) {
            return None;
        }
        if !self.may_start(&run.session, &run.run_id, &run.trace_id, world) {
            return None;
        }
        Some(Handed {
            run_id: run.run_id.clone(),
            session: run.session.clone(),
            prompt: prompt(run),
        })
    }

    /// What the Host's queue answered when it was handed the run of a trigger. A run that
    /// was not taken is offered again after half a minute: the same run, never another.
    /// Answers whether its turn is taken now.
    pub fn handed(
        &mut self,
        attachment_id: &str,
        how: &Delivery,
        world: &dyn World,
        now_ms: i64,
    ) -> bool {
        let Some(mut state) = self.progress.get(attachment_id).cloned() else {
            return false;
        };
        if self
            .acting(attachment_id, Some(state.generation), world)
            .is_none()
        {
            return false;
        }
        let again = now_ms + OFFERED_AGAIN_AFTER_MS;
        match how {
            Delivery::Taken => {
                state.delivery_accepted = Some(true);
                state.last_delivery_error = Some(String::new());
            }
            Delivery::NotTaken => {
                state.next_delivery_at = Some(again);
                state.last_delivery_error = Some("Dispatch not accepted".to_string());
            }
            Delivery::Broke(what) => {
                state.next_delivery_at = Some(again);
                state.last_delivery_error = Some(what.clone());
            }
        }
        let taken = state.delivery_accepted == Some(true);
        self.keep(attachment_id, state);
        taken
    }

    /// The time a schedule came due is over: what its run said of itself sets how long
    /// the task waits, and from that when it runs next.
    fn close(
        &self,
        config: &Config,
        state: &mut Progress,
        (cron, zone): (&str, &str),
        world: &dyn World,
        now_ms: i64,
    ) {
        let Some(occurrence) = state.occurrence.clone() else {
            return;
        };
        if !state.pending.is_empty() || state.active_run_id.is_some() {
            return;
        }
        let ended_as = |word: &str| occurrence.activities.iter().any(|one| one == word);
        let activity = if ended_as(adaptive::WORKED) {
            adaptive::WORKED
        } else if ended_as(adaptive::FAILED) {
            adaptive::FAILED
        } else {
            adaptive::IDLE
        };
        let summary = match (
            occurrence.summary.is_empty(),
            occurrence.activities.is_empty(),
        ) {
            (false, _) => occurrence.summary.as_str(),
            (true, true) => "Nothing to review",
            (true, false) => "",
        };
        let measured = state.cron_base_seconds.filter(|base| *base != 0);
        let base = measured.unwrap_or_else(|| world.base_interval(cron, zone, now_ms));
        state.cron_base_seconds = Some(base);
        let kept = Value::Object(config.options.clone());
        let options = option_values(&config.template_id, Some(&kept)).unwrap_or_default();
        let longest = options.get("max_interval_seconds").and_then(Value::as_i64);
        let waited = state.adaptive.get_or_insert_with(State::default);
        let set = (base, longest);
        let interval = adaptive::advance(waited, activity, summary, set, Some(now_ms));
        let (pace, _) = adaptive::bounds(base, longest);
        let interval = interval.unwrap_or(pace);
        state.occurrence = None;
        // A task that keeps its pace runs at the next time its schedule names. Only a wait
        // that was stretched is counted from the time that ran.
        let earliest = if interval > pace {
            now_ms.max(occurrence.at + interval * 1000 - 1)
        } else {
            now_ms.max(occurrence.at)
        };
        state.next_run_at = world.next_run(cron, zone, earliest);
    }

    /// A run that is out and may still act: the trigger and the job it was made for are
    /// as they were. Otherwise why not, in the Python Host's words.
    fn acting_run(&self, run_id: &str, world: &dyn World) -> Result<&Run, Refused> {
        let run = self.runs.get(run_id);
        let run = run.ok_or_else(|| refused(404, "run_not_found", NO_RUN))?;
        if !run.out() {
            return Err(refused(409, "run_terminal", RUN_OVER));
        }
        let stale = |why: &str| refused(409, "stale_generation", why);
        let config = self.acting(&run.attachment_id, Some(run.generation), world);
        let config = config.ok_or_else(|| stale(CUT_OFF))?;
        let agent = agent_of(&run.agent_id, world)?;
        let frozen = run.configuration.get("effective_chain");
        let frozen =
            frozen.filter(|chain| chain.as_object().is_some_and(|chain| !chain.is_empty()));
        if frozen.is_some_and(|frozen| *frozen != chain(&agent.backend, &agent.model, config)) {
            return Err(stale(OTHER_MODEL));
        }
        let kept = Value::Object(config.options.clone());
        let options = option_values(&config.template_id, Some(&kept))?;
        let empty = Map::new();
        let frozen = run.configuration.get("options").and_then(Value::as_object);
        if *frozen.unwrap_or(&empty) != options {
            return Err(stale(OTHER_OPTIONS));
        }
        Ok(run)
    }

    /// Whether the turn of a run may start: asked by the Host when the turn is taken from
    /// its queue, and once more when it starts.
    pub fn may_start(
        &self,
        session: &str,
        run_id: &str,
        trace_id: &str,
        world: &dyn World,
    ) -> bool {
        let Ok(run) = self.acting_run(run_id, world) else {
            return false;
        };
        let job = run.configuration.get("template_id").and_then(Value::as_str);
        let a_turn = job.is_some_and(|job| turn_templates().iter().any(|one| one == job));
        run.session == session && run.trace_id == trace_id && a_turn
    }

    /// What a run says of itself before it ends: `worked`, `idle` or `failed`, and one
    /// line. Said again, the later word holds.
    pub fn report(
        &mut self,
        run_id: &str,
        activity: &str,
        summary: &str,
        world: &dyn World,
    ) -> Result<Value, Refused> {
        let invalid = |why: String| Refused::new(400, "invalid_activity", why);
        if !adaptive::known(activity) {
            return Err(invalid(
                "Activity must be worked, idle or failed".to_string(),
            ));
        }
        let summary = one_line(summary);
        let most = adaptive::SUMMARY_MOST;
        if summary.chars().count() > most {
            let why = format!("The summary is one line of at most {most} characters");
            return Err(invalid(why));
        }
        self.acting_run(run_id, world)?;
        if let Some(run) = self.runs.get_mut(run_id) {
            run.activity = activity.to_string();
            run.activity_summary.clone_from(&summary);
        }
        Ok(json!({ "run_id": run_id, "activity": activity, "summary": summary }))
    }

    /// What a run is given to read when its turn begins.
    pub fn context(&self, run_id: &str, world: &dyn World) -> Result<Value, Refused> {
        let run = self.acting_run(run_id, world)?;
        Ok(json!({ "run_id": run_id, "generation": run.generation, "candidates": [] }))
    }

    /// The turn of a run has started.
    pub fn started(
        &mut self,
        run_id: &str,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<Value, Refused> {
        self.acting_run(run_id, world)?;
        let run = self.runs.get_mut(run_id);
        let run = run.ok_or_else(|| refused(404, "run_not_found", NO_RUN))?;
        run.status = RUNNING.to_string();
        run.started_at.get_or_insert(now_ms);
        Ok(run.shown())
    }

    /// Ends a run that is out: `completed`, or `error` with why. One that is over stays as
    /// it is.
    fn finish(
        &mut self,
        run_id: &str,
        outcome: &str,
        why: &str,
        world: &dyn World,
        now_ms: i64,
    ) -> Result<(), Refused> {
        let run = self.runs.get(run_id);
        let run = run.ok_or_else(|| refused(404, "run_not_found", NO_RUN))?;
        if !run.out() {
            return Ok(());
        }
        let agent_id = self.acting_run(run_id, world)?.agent_id.clone();
        let why = python::first(why, ERROR_MOST);
        let why = if why.is_empty() && outcome == ERROR {
            NO_RECEIPTS
        } else {
            why
        };
        let status = match outcome {
            ERROR => FAILED,
            CANCELLED => CANCELLED,
            _ => COMPLETED,
        };
        if let Some(run) = self.runs.get_mut(run_id) {
            run.status = status.to_string();
            run.outcome = outcome.to_string();
            run.finished_at = Some(now_ms);
            run.error = why.to_string();
        }
        if let Some(config) = self.configs.get_mut(&agent_id) {
            config.last_run_at = Some(now_ms);
            config.last_error = why.to_string();
            config.updated_at = now_ms;
        }
        self.forget_old(&agent_id);
        Ok(())
    }

    /// The runs of a janitor, the newest first.
    fn newest_first(&self, agent_id: &str) -> Vec<&Run> {
        let own = self.runs.values().filter(|run| run.agent_id == agent_id);
        let mut own: Vec<&Run> = own.collect();
        own.sort_by(|one, other| {
            let (one, other) = (
                (one.created_at, &one.run_id),
                (other.created_at, &other.run_id),
            );
            other.cmp(&one)
        });
        own
    }

    /// Of the runs of a janitor that are over, the newest hundred are kept.
    fn forget_old(&mut self, agent_id: &str) {
        let over = self.newest_first(agent_id).into_iter();
        let over = over.filter(|run| !run.out()).skip(RUNS_MOST);
        let old: Vec<String> = over.map(|run| run.run_id.clone()).collect();
        for run_id in old {
            self.runs.remove(&run_id);
        }
    }

    /// The runs of a janitor as an app reads them: the newest first, `limit` at most (one
    /// at least, a hundred at most).
    pub fn runs_of(&self, key: &str, limit: i64, world: &dyn World) -> Result<Vec<Value>, Refused> {
        let agent = agent_of(key, world)?;
        let most = usize::try_from(limit.clamp(1, 100)).unwrap_or(1);
        let own = self.newest_first(&agent.agent_id).into_iter();
        Ok(own.take(most).map(Run::shown).collect())
    }
}

/// Whose model a run is run by, as it was when the run was made. A job the Host does by
/// itself has none.
fn chain(backend: &str, model: &str, config: &Config) -> Value {
    let named =
        |key: &str, word: &str| config.execution.get(key).and_then(Value::as_str) == Some(word);
    if named("executor", "deterministic") || named("provider", "local") {
        return json!({ "source": "deterministic", "chain": [], "revision": config.revision });
    }
    json!({
        "source": "explicit", "chain": [{ "provider": backend, "model": model }],
        "revision": config.revision,
    })
}
