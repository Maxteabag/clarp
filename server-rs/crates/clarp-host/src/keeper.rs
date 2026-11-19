//! The Goal keeper at the Host's edge: the loop that keeps the books of durable goals
//! beside the agents that do the work.
//!
//! The rules are the kernel's ([`clarp_kernel::goals::keeper`]): where a goal's next update
//! reads from, what happened since, whether the goal has stalled, the prompt, what is taken
//! from a model's answer, and which goals are due; and the goal book's own write
//! ([`clarp_kernel::goals::GoalBook::updater`]). Here is what they need of a Host, as the
//! Python Host gives it (`janitor_autonomy.goal_keeper_once`, `goal_keeper.update` and
//! `run_batch`).
//!
//! **The loop.** Every fifteen seconds, for each keeper that is on, the open goals of the
//! agents in its scope are looked at; the ones whose folded triggers are ripe, six at most,
//! are updated in one run of the janitor, side by side: each is read, asked of the
//! janitor's model once, and written through the thread of the agent that owns it, as the
//! owner's own tool writes. One run at a time. The owner is never prompted, told or
//! interrupted, and what a goal's world held is shown to a model that has nothing to act
//! with ([`crate::once::read`]).
//!
//! **A goal's world.** What the owner and its helpers said since is read from their
//! conversations: the revision each had at the time the update reads from is found once
//! ([`AgentHandle::revision_at`](crate::agent::AgentHandle::revision_at)), and from then on
//! only what changed is asked for. Of each agent no more is held than a prompt has room
//! for. Their jobs, how the owner's last turn ended and what was done to the goal are read
//! off what the agents publish, without asking them.
//!
//! **What is kept**, with the fleet: each keeper's own state of its goals (when a fold
//! began, when a failed update is tried again, which goals were read from their start),
//! and a receipt of what was done with a goal in a run, the newest two hundred of a goal. A
//! receipt is kept after the write it tells of, in another store: a Host that dies between
//! the two has the write and no receipt of it.

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};

use clarp_actor::conversation::{Versioned, Which};
use clarp_backend::Reading;
use clarp_kernel::conversation::Entry;
use clarp_kernel::goals::Goal;
use clarp_kernel::goals::keeper::{self as rules, ROLE};
use clarp_kernel::janitors::book::Agent;
use clarp_kernel::janitors::demand::in_scope;
use clarp_kernel::python;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::agent::Unavailable;
use crate::edge::App;
use crate::fleet::{Directory, Member};
use crate::goaltool::{self, Ask, Written};
use crate::model_fallbacks::{Failure, execute};
use crate::once;
use crate::tools::ToolAnswer;
use crate::wire;

/// The kinds of thing the keeper leaves with the fleet: a keeper's own state of its goals,
/// under the attachment it is kept for, and a receipt, under its id.
const PROGRESS: &str = "keeper-progress";
const RECEIPTS: &str = "keeper-receipts";
/// How many receipts of one goal are kept.
const RECEIPTS_OF_A_GOAL: usize = 200;
/// How long a model is given for one goal. A run is given up after three minutes.
const WITHIN: Duration = Duration::from_secs(150);
/// How many rows are asked of an agent at once.
const PAGE: usize = 200;
/// How much of what one agent said is held for a goal, in signs: what a prompt holds of
/// all of them together, counting of each row no more than a prompt takes of it.
const HELD_SIGNS: usize = 40_000;
const ROW_SIGNS: usize = 1_500;
/// How many goals a run updates where its janitor says nothing, and at most.
const GOALS_AT_ONCE: i64 = 5;
const GOALS_MOST: i64 = 6;
/// The trigger a keeper's own state is kept under.
const TURN_ENDS: &str = "agent-work-completed";
/// What a stalled goal waits for. The Python Host names its owner here; this Host does not
/// know who its user is called.
const STALLED_NEXT: &str =
    "Waiting for the owner to resume it, or for the user to resume or close the goal.";
/// What a receipt says of a step called done with nothing shown.
const HELD: &str = "Completed claimed without evidence; left in progress";
/// What a goal shows of its newest receipt.
const SHOWN_OF_A_RECEIPT: [&str; 10] = [
    "run_id",
    "outcome",
    "reason",
    "steps",
    "model",
    "input_tokens",
    "output_tokens",
    "duration_ms",
    "error",
    "created_at",
];

/// The Goal keeper of this Host: what it keeps (`None` until it was read where it is kept),
/// and what it has read of each goal's world since where the goal's update reads from.
#[derive(Default)]
pub struct Desk {
    held: tokio::sync::Mutex<Option<Kept>>,
    seen: std::sync::Mutex<HashMap<String, Seen>>,
    /// One pass at a time: a run that still asks its model is not overtaken.
    passing: tokio::sync::Mutex<()>,
}

#[derive(Default)]
struct Kept {
    /// Each keeper's own state of its goals, by the attachment it is kept under.
    progress: BTreeMap<String, Value>,
    /// The receipts of each goal, the oldest first.
    receipts: BTreeMap<String, Vec<Value>>,
}

/// What was read of a goal's world since the time its update reads from.
struct Seen {
    since_ms: i64,
    agents: HashMap<String, Read>,
}

/// How far an agent's conversation was read, and what of it was written since: each row
/// once, as it last was, in the order they changed.
struct Read {
    revision: u64,
    rows: Vec<rules::Row>,
}

fn order(receipt: &Value) -> (i64, String) {
    let id = receipt["receipt_id"].as_str().unwrap_or_default();
    (receipt["created_at"].as_i64().unwrap_or(0), id.to_string())
}

fn clip(text: &str, most: usize) -> String {
    text.chars().take(most).collect()
}

impl Desk {
    /// What the keeper keeps, read where it is kept the first time it is asked for.
    async fn held(
        &self,
        app: &App,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<Kept>>, Unavailable> {
        let mut held = self.held.lock().await;
        if held.is_none() {
            let mut kept = Kept::default();
            for (attachment, written) in app.fleet.kept_all(PROGRESS).await? {
                if let Ok(progress) = serde_json::from_slice::<Value>(&written) {
                    kept.progress.insert(attachment, progress);
                }
            }
            for (_, written) in app.fleet.kept_all(RECEIPTS).await? {
                let Ok(receipt) = serde_json::from_slice::<Value>(&written) else {
                    continue;
                };
                let plan_id = receipt["plan_id"].as_str().unwrap_or_default().to_string();
                kept.receipts.entry(plan_id).or_default().push(receipt);
            }
            for of_a_goal in kept.receipts.values_mut() {
                of_a_goal.sort_by_key(order);
            }
            *held = Some(kept);
        }
        Ok(held)
    }

    /// A keeper's own state of its goals. What cannot be read is no state.
    async fn progress(&self, app: &App, attachment: &str) -> Value {
        let Ok(held) = self.held(app).await else {
            return json!({});
        };
        let kept = held.as_ref().and_then(|kept| kept.progress.get(attachment));
        kept.cloned().unwrap_or_else(|| json!({}))
    }

    async fn keep_progress(&self, app: &App, attachment: &str, progress: Value) {
        let Ok(mut held) = self.held(app).await else {
            return;
        };
        let Some(kept) = held.as_mut() else {
            return;
        };
        if kept.progress.get(attachment) == Some(&progress) {
            return;
        }
        let written = serde_json::to_vec(&progress).unwrap_or_default();
        kept.progress.insert(attachment.to_string(), progress);
        drop(held);
        let _ = app
            .fleet
            .keep(PROGRESS, attachment.to_string(), Some(written))
            .await;
    }

    /// Keeps a receipt, and of its goal no more than the newest two hundred.
    async fn keep_receipt(&self, app: &App, receipt: Value) {
        let (plan_id, id) = (
            receipt["plan_id"].as_str().unwrap_or_default().to_string(),
            receipt["receipt_id"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
        );
        let written = serde_json::to_vec(&receipt).unwrap_or_default();
        let mut let_go = Vec::new();
        if let Ok(mut held) = self.held(app).await
            && let Some(kept) = held.as_mut()
        {
            let of_the_goal = kept.receipts.entry(plan_id).or_default();
            of_the_goal.retain(|known| known["receipt_id"] != receipt["receipt_id"]);
            of_the_goal.push(receipt);
            of_the_goal.sort_by_key(order);
            let over = of_the_goal.len().saturating_sub(RECEIPTS_OF_A_GOAL);
            let old = of_the_goal.drain(..over);
            let_go.extend(old.filter_map(|old| old["receipt_id"].as_str().map(str::to_string)));
        }
        let _ = app.fleet.keep(RECEIPTS, id, Some(written)).await;
        for old in let_go {
            let _ = app.fleet.keep(RECEIPTS, old, None).await;
        }
    }

    /// What was read of a goal since this time: taken out to be read on from, and put back.
    fn take(&self, plan_id: &str, since_ms: i64) -> Seen {
        let known = self
            .seen
            .lock()
            .ok()
            .and_then(|mut seen| seen.remove(plan_id));
        match known.filter(|seen| seen.since_ms == since_ms) {
            Some(seen) => seen,
            None => Seen {
                since_ms,
                agents: HashMap::new(),
            },
        }
    }

    fn put(&self, plan_id: &str, seen: Seen) {
        if let Ok(mut all) = self.seen.lock() {
            all.insert(plan_id.to_string(), seen);
        }
    }

    /// Nothing is held of a goal that is open no more.
    fn forget_all_but(&self, open: &[String]) {
        if let Ok(mut all) = self.seen.lock() {
            all.retain(|plan_id, _| open.contains(plan_id));
        }
    }
}

// ---- a goal's world --------------------------------------------------------------------------

/// A row of a conversation as the keeper's rules read it. `since_ms`: the time the reading
/// began after, so that the row was written after it, whenever it was begun.
fn row_of(agent_id: &str, version: &Versioned, since_ms: i64, now: i64) -> rules::Row {
    let shown = wire::row(version);
    let said = |key: &str| {
        let said = shown[key].as_str().filter(|said| !said.is_empty());
        said.map(str::to_string)
    };
    let closed = version.turn.as_ref();
    // A turn ends with the row that closed it. What an agent said last so far in a turn
    // that still runs is no end: tool work may follow it.
    let brought = matches!(version.entry, Entry::Imported(_));
    let phase = said("phase").filter(|phase| phase != "final" || closed.is_some() || brought);
    let written = closed.map_or(version.ts_ms, |end| end.ended_at_ms.max(version.ts_ms));
    // What a tool's own reader shows of a call stands for its card.
    let mut tools = shown["tools"].as_array().cloned().unwrap_or_default();
    if tools.is_empty() {
        for cell in shown["display_cells"].as_array().into_iter().flatten() {
            let what = match &cell["summary"] {
                Value::String(summary) if !summary.is_empty() => cell["summary"].clone(),
                _ => cell["lines"][0]["text"].clone(),
            };
            tools.push(json!({ "name": cell["title"], "summary": what }));
        }
    }
    rules::Row {
        message_id: version.entry.id().to_string(),
        agent_id: agent_id.to_string(),
        seq: i64::try_from(version.revision >> 20).unwrap_or(i64::MAX),
        role: said("role"),
        timestamp: said("timestamp"),
        text: shown["text"].as_str().map(str::to_string),
        tools,
        updated_at: if version.live {
            now
        } else {
            written.max(since_ms + 1)
        },
        origin: said("origin"),
        sender_agent_id: said("sender_agent_id"),
        phase,
    }
}

/// Of what an agent said, no more is held than a prompt has room for: the oldest goes.
fn bound(rows: &mut Vec<rules::Row>) {
    let weighs = |row: &rules::Row| {
        let said = row.text.as_deref().map_or(0, |said| said.chars().count());
        said.min(ROW_SIGNS)
    };
    let mut held: usize = rows.iter().map(weighs).sum();
    let mut gone = 0;
    while gone < rows.len() && held - weighs(&rows[gone]) >= HELD_SIGNS {
        held -= weighs(&rows[gone]);
        gone += 1;
    }
    rows.drain(..gone);
}

/// Reads on in an agent's conversation from where it was read to. The first time, from the
/// revision the conversation had at `since_ms`.
async fn read_on(
    member: &Member,
    seen: &mut Seen,
    now: i64,
) -> Result<Vec<rules::Row>, Unavailable> {
    let agent_id = &member.record.agent_id;
    if !seen.agents.contains_key(agent_id) {
        let revision = member.agent.revision_at(seen.since_ms).await?;
        let begun = Read {
            revision,
            rows: Vec::new(),
        };
        seen.agents.insert(agent_id.clone(), begun);
    }
    let since_ms = seen.since_ms;
    let Some(read) = seen.agents.get_mut(agent_id) else {
        return Ok(Vec::new());
    };
    while member.agent.view().status.head_revision > read.revision {
        let page = member.agent.after(read.revision, PAGE, Which::All).await?;
        for version in &page.entries {
            let row = row_of(agent_id, version, since_ms, now);
            read.rows.retain(|held| held.message_id != row.message_id);
            read.rows.push(row);
        }
        let moved = page.latest_revision > read.revision;
        read.revision = read.revision.max(page.latest_revision);
        if !page.has_more || !moved {
            break;
        }
    }
    bound(&mut read.rows);
    Ok(read.rows.clone())
}

/// What the keeper's rules read of a goal: its checkpoints and how its work goes on, without
/// its history.
fn light(goal: &Goal) -> Value {
    let mut read = Map::new();
    for key in ["updater", "checkpoint"] {
        if let Some(held) = goal.fields.get(key) {
            read.insert(key.to_string(), held.clone());
        }
    }
    read.insert("continuation".to_string(), json!(goal.continuation));
    Value::Object(read)
}

/// What was done to a goal: its history, each entry numbered from the goal's first. The
/// keeper's own writes are under its role.
fn events_of(goal: &Goal) -> Vec<rules::Event> {
    let before = i64::try_from(goal.history_offset).unwrap_or(0);
    let numbered = goal.history.iter().zip(1i64..);
    numbered
        .map(|(entry, number)| {
            let kind = entry["kind"].as_str().unwrap_or_default().to_string();
            let own = kind == "updater";
            rules::Event {
                event_id: before + number,
                at: entry["at"].as_i64().unwrap_or(0),
                kind,
                delegation_id: if own { ROLE.to_string() } else { String::new() },
            }
        })
        .collect()
}

/// When an agent last wrote anything: its newest row, or the end of its last turn.
fn last_written(member: &Member) -> Option<i64> {
    let view = member.agent.view();
    let row = view.newest.as_ref().map(|newest| {
        let closed = newest.turn.as_ref();
        closed.map_or(newest.ts_ms, |end| end.ended_at_ms.max(newest.ts_ms))
    });
    let turn = view.status.last.as_ref().map(|last| last.ended_at_ms);
    row.max(turn)
}

/// A goal's world as it was read, for the rules to look at.
struct Gathered {
    /// The plan as it is read, where it was read for an update.
    plan: Value,
    title: String,
    status: String,
    created_at: i64,
    revision: i64,
    goal: Value,
    owner: String,
    owner_name: String,
    helpers: Vec<rules::Helper>,
    rows: Vec<rules::Row>,
    jobs: Vec<rules::Job>,
    events: Vec<rules::Event>,
    ended: Option<rules::Ended>,
    last_written: Option<i64>,
    /// Where the update reads from: the time, and the number of what was done to the goal.
    since: (i64, i64),
}

impl Gathered {
    fn world(&self) -> rules::World<'_> {
        rules::World {
            status: &self.status,
            created_at: self.created_at,
            goal: &self.goal,
            owner: &self.owner,
            owner_name: &self.owner_name,
            helpers: &self.helpers,
            rows: &self.rows,
            jobs: &self.jobs,
            events: &self.events,
            ended: self.ended.as_ref(),
            last_written: self.last_written,
        }
    }

    /// What happened since where the update reads from.
    fn activity(&self) -> rules::Activity {
        rules::activity(&self.world(), self.since.0, self.since.1)
    }
}

/// Reads a goal's world. `full`: from the goal's own start, where it was never updated.
/// `whole`: with what each helper reported last and ended its last turn with, which an
/// update shows its model and a look does not need. `None`: there is no such goal.
async fn gather(
    app: &App,
    directory: &Directory,
    owner: &Arc<Member>,
    plan_id: &str,
    now: i64,
    (full, whole): (bool, bool),
) -> Option<Gathered> {
    let view = owner.agent.view();
    let book = &view.books.goals;
    let plan = book.plan(plan_id)?;
    let kept = plan.goal.as_ref()?;
    let goal = light(kept);
    let events = events_of(kept);
    let full = full && !python::truthy(&goal["updater"]);
    let (since_ms, since_event) = rules::cursor(&goal, plan.created_at, now, full);
    // A goal that came from another Host counts what was done to it as that Host did: what
    // it has here is numbered from its own first, and nothing of it is newer than all of it.
    let done = events.last().map_or(0, |last| last.event_id);
    let since = (since_ms, since_event.min(done));

    let owner_id = owner.record.agent_id.as_str();
    let mut helping: Vec<&Arc<Member>> = directory
        .members
        .iter()
        .filter(|member| {
            let record = &member.record;
            record.parent_agent_id.as_deref() == Some(owner_id)
                && rules::is_helper(record.created_at_ms, false, plan.created_at)
        })
        .collect();
    helping.sort_by_key(|member| member.record.created_at_ms);
    helping.truncate(rules::HELPERS_MOST);

    // What each of them said since, read on from where it was read to.
    let mut seen = app.keeper.take(plan_id, since_ms);
    let mut rows = Vec::new();
    let mut everyone = vec![owner];
    everyone.extend(helping.iter().copied());
    for member in &everyone {
        if let Ok(said) = read_on(member, &mut seen, now).await {
            rows.extend(said);
        }
    }
    seen.agents.retain(|agent_id, _| {
        let mut ids = everyone.iter().map(|member| &member.record.agent_id);
        ids.any(|id| id == agent_id)
    });
    app.keeper.put(plan_id, seen);

    // What each helper wrote to the owner last is looked for in the owner's newest rows,
    // and what its last turn ended with in its own.
    let mut reports: HashMap<String, String> = HashMap::new();
    if whole
        && !helping.is_empty()
        && let Ok(page) = owner.agent.tail(PAGE, Which::All).await
    {
        for version in &page.entries {
            let shown = wire::row(version);
            let sender = shown["sender_agent_id"].as_str().unwrap_or_default();
            if shown["role"] == "user" && !sender.is_empty() {
                let said = shown["text"].as_str().unwrap_or_default().to_string();
                reports.insert(sender.to_string(), said);
            }
        }
    }
    let mut helpers = Vec::new();
    for member in &helping {
        let record = &member.record;
        let mut last_final = None;
        if whole && let Ok(page) = member.agent.tail(PAGE / 4, Which::All).await {
            let closing = page.entries.iter().rev().filter(|row| row.turn.is_some());
            let mut ends = closing.map(wire::row).filter(|row| row["phase"] == "final");
            last_final = ends
                .next()
                .and_then(|row| row["text"].as_str().map(str::to_string));
        }
        helpers.push(rules::Helper {
            agent_id: record.agent_id.clone(),
            session: record.session.clone(),
            backend: record.backend.clone(),
            helper_state: record.helper_state.clone(),
            archived: record.archived_at_ms.is_some(),
            last_report: reports.remove(&record.agent_id),
            last_final,
        });
    }

    let mut jobs = Vec::new();
    for member in &everyone {
        for job in &member.agent.view().jobs {
            let why = &job.terminal_reason;
            jobs.push(rules::Job {
                job_id: job.id.clone(),
                agent_id: member.record.agent_id.clone(),
                kind: job.kind.clone(),
                title: Some(job.title.clone()),
                status: job.status.as_str().to_string(),
                started_at: job.started_at_ms,
                terminal_at: job.terminal_at_ms,
                terminal_reason: (!why.is_empty()).then(|| why.clone()),
            });
        }
    }
    // How the owner's last turn ended, as the Host tells its state to an app.
    let state = wire::state(
        &owner.record,
        &view.status,
        view.backend,
        view.ended.as_ref(),
    );
    let ended = rules::Ended {
        at: state["ts"].as_i64().unwrap_or(0),
        kind: state["kind"].as_str().unwrap_or_default().to_string(),
        reason: state["detail"]["reason"]
            .as_str()
            .unwrap_or_default()
            .to_string(),
    };
    let name = match owner.record.persona.as_str() {
        "" => &owner.record.session,
        persona => persona,
    };
    Some(Gathered {
        // The plan as it is read is what a model is shown: a look has no use for it.
        plan: if whole {
            book.get(plan_id, now)?
        } else {
            Value::Null
        },
        title: plan.title.clone(),
        status: plan.status.clone(),
        created_at: plan.created_at,
        revision: plan.revision,
        goal,
        owner: owner_id.to_string(),
        owner_name: name.to_string(),
        helpers,
        rows,
        jobs,
        events,
        ended: Some(ended),
        last_written: everyone
            .iter()
            .filter_map(|member| last_written(member))
            .max(),
        since,
    })
}

// ---- one goal ----------------------------------------------------------------------------------

/// The run goals are updated in, and whom its janitor asks.
struct Run {
    run_id: String,
    janitor: String,
    tool: String,
    model: String,
    effort: String,
}

/// How one goal's update ended.
enum Ended {
    /// Written: how many steps changed.
    Updated(usize),
    Skipped(&'static str),
    /// The janitor was paused or set anew while the run was out.
    Cancelled,
    /// The owner changed the plan since it was read.
    Conflict,
    /// By a short code, never by what a model or a tool said.
    Failed(&'static str),
}

/// What the run is told of one goal.
struct Done {
    plan_id: String,
    outcome: &'static str,
    error: &'static str,
    changed: usize,
}

async fn current(app: &App, run_id: &str) -> bool {
    let asked = app.janitors.read(app, |book, world, now_ms| {
        book.is_current(run_id, world, now_ms)
    });
    asked.await.unwrap_or(false)
}

/// Hands the keeper's write to the goal's owner, and fills the receipt with what was
/// written. `given`: the outcome and the reason of a write the rule made by itself.
async fn write(
    owner: &Member,
    plan_id: &str,
    written: Written,
    (held, given, previous): (&[String], Option<(&str, &str)>, &str),
    receipt: &mut Value,
) -> Ended {
    let progress = clip(python::strip(&written.progress), 2_000);
    let next_work = clip(python::strip(&written.next_work), 1_000);
    let ask = Ask::Keeper {
        plan_id: plan_id.to_string(),
        written: Box::new(written),
    };
    let said = match owner.agent.goal(ask).await {
        Ok(said) => said,
        Err(_) => return Ended::Failed("owner_unavailable"),
    };
    match said.exit {
        0 => {}
        goaltool::OWNER_AHEAD => return Ended::Conflict,
        goaltool::CLOSED => return Ended::Failed("goal_closed"),
        _ => return Ended::Failed("invalid_update"),
    }
    let result: Value = serde_json::from_str(said.stdout.trim()).unwrap_or_default();
    let changed = result["changed_steps"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut steps: Vec<Value> = changed
        .iter()
        .map(|step| {
            let note = clip(step["note"].as_str().unwrap_or_default(), 300);
            json!({ "item_id": step["item_id"], "from": step["from"], "to": step["status"], "note": note })
        })
        .collect();
    let moved = |item_id: &String| {
        changed
            .iter()
            .any(|step| step["item_id"] == item_id.as_str())
    };
    let held: Vec<&String> = held.iter().filter(|item_id| !moved(item_id)).collect();
    for item_id in &held {
        steps.push(json!({ "item_id": item_id, "from": null, "to": "held", "note": HELD }));
    }
    let outcome = match given {
        Some((outcome, _)) => outcome,
        None if !changed.is_empty() => "updated",
        None if !held.is_empty() => "held_by_evidence_rule",
        None if progress != previous => "updated",
        None => "unchanged",
    };
    receipt["steps"] = json!(steps);
    receipt["progress"] = json!(clip(&progress, 500));
    receipt["next_work"] = json!(clip(&next_work, 300));
    receipt["outcome"] = json!(outcome);
    if let Some((_, reason)) = given {
        receipt["reason"] = json!(clip(reason, 200));
    }
    Ended::Updated(changed.len())
}

/// Reads, asks and writes one goal, as the Python Host's `update` does. The receipt is
/// filled as it goes, so that an update that fails part-way still says what was known.
async fn keep(
    app: &App,
    run: &Run,
    plan_id: &str,
    (now, full): (i64, bool),
    receipt: &mut Value,
) -> Ended {
    let directory = app.fleet.directory();
    let owner_id = goaltool::owner_of(plan_id);
    let mut owners = directory.members.iter();
    let Some(owner) = owners.find(|member| member.record.agent_id == owner_id) else {
        return Ended::Skipped("owner unavailable");
    };
    let Some(read) = gather(app, &directory, owner, plan_id, now, (full, true)).await else {
        return Ended::Skipped("closed");
    };
    receipt["title"] = json!(clip(&read.title, 200));
    if !matches!(read.status.as_str(), "active" | "paused") {
        return Ended::Skipped("closed");
    }
    let world = read.world();
    let (sheet, job_refs) = rules::fact_sheet(&world, now);
    let seen = read.activity();
    let until = now.max(seen.newest_ms);
    let (said, message_refs) = rules::delta(&world, read.since.0, until);
    let mut refs: Vec<String> = message_refs
        .iter()
        .skip(message_refs.len().saturating_sub(20))
        .cloned()
        .collect();
    refs.extend(job_refs);
    if seen.newest_event != 0 {
        refs.push(format!("goal_event:{}", seen.newest_event));
    }
    receipt["source_refs"] = json!(refs.iter().take(40).collect::<Vec<_>>());
    let stalled = rules::stall(&world, now);
    let line = stalled
        .as_ref()
        .map(|found| rules::stall_line(found.since, found.cause));
    let line = line.unwrap_or_default();
    let previous = read.goal["updater"]["progress"]
        .as_str()
        .unwrap_or_default();
    let written = |steps, progress: String, next_work: &str, model: &str| Written {
        seen_revision: read.revision,
        steps,
        progress,
        next_work: next_work.to_string(),
        model: model.to_string(),
        source_refs: refs.clone(),
        through_ms: until,
        through_event: seen.newest_event,
        janitor_agent_id: run.janitor.clone(),
    };
    if said.is_empty() && seen.triggers.is_empty() {
        let Some(stalled) = stalled else {
            return Ended::Skipped("nothing new");
        };
        if previous.starts_with(&line) {
            return Ended::Skipped("already marked stalled");
        }
        // Nothing new to read: the rule says the goal has stalled. No model, no step.
        if !current(app, &run.run_id).await {
            return Ended::Cancelled;
        }
        receipt["model"] = json!("rule");
        let by_rule = written(Vec::new(), line, STALLED_NEXT, "rule");
        let given = (&[][..], Some(("stalled", stalled.code)), previous);
        return write(owner, plan_id, by_rule, given, receipt).await;
    }
    let prompt = rules::prompt(&read.plan, &sheet, &said);
    let asked = prompt.strip_prefix(rules::SYSTEM).map(str::trim_start);
    let word = |word: &str| (!word.is_empty()).then(|| word.to_string());
    let reading = Reading {
        instructions: rules::SYSTEM.to_string(),
        asked: asked.unwrap_or(&prompt).to_string(),
        schema: rules::schema(&read.plan).to_string(),
        model: word(&run.model),
        effort: word(&run.effort),
    };
    let primary = clarp_kernel::janitors::fallbacks::Pick {
        backend: run.tool.clone(),
        model: run.model.clone(),
        effort: run.effort.clone(),
    };
    let plan = &read.plan;
    let answer = execute(app, &run.run_id, plan_id, primary, |pick| {
        let mut reading = reading.clone();
        async move {
            reading.model = (!pick.model.is_empty()).then_some(pick.model);
            reading.effort = (!pick.effort.is_empty()).then_some(pick.effort);
            let answer = once::read_on(app, &pick.backend, &reading, WITHIN)
                .await
                .map_err(Failure::runner)?;
            let raw = clarp_kernel::router::extract_json(&answer)
                .map_err(|e| Failure::model("parse_error", e))?;
            let result = rules::decide(plan, &Value::Object(raw));
            if result.1.is_empty() {
                return Err(Failure::model("parse_error", "invalid keeper response"));
            }
            Ok(result)
        }
    })
    .await;
    let ((steps, progress, next_work), actual) = match answer {
        Ok(value) => value,
        Err(error) if error.code == "cancelled" => return Ended::Cancelled,
        Err(error) => return Ended::Failed(error.code),
    };
    receipt["model"] = json!(actual.model);
    let progress = if line.is_empty() || progress.starts_with(&line) {
        progress
    } else {
        format!("{line} {progress}")
    };
    if !current(app, &run.run_id).await {
        return Ended::Cancelled;
    }
    let held: Vec<String> = steps
        .iter()
        .filter(|(_, _, note)| note.starts_with(rules::HELD_NOTE))
        .map(|(item_id, _, _)| item_id.clone())
        .collect();
    let by_model = written(steps, progress, &next_work, &actual.model);
    write(owner, plan_id, by_model, (&held, None, previous), receipt).await
}

/// One goal of a run: updated, and its receipt kept whatever became of it.
async fn update(app: &App, run: &Run, plan_id: &str, at: (i64, bool)) -> Done {
    let started = Instant::now();
    let mut receipt = json!({
        "receipt_id": format!("{}:{plan_id}", run.run_id), "run_id": run.run_id,
        "plan_id": plan_id, "title": "", "outcome": "", "reason": "", "steps": [],
        "progress": "", "next_work": "", "source_refs": [], "model": run.model,
        "input_tokens": null, "output_tokens": null, "duration_ms": null, "error": "",
        "created_at": 0,
    });
    let ended = keep(app, run, plan_id, at, &mut receipt).await;
    let (outcome, error, changed) = match ended {
        Ended::Updated(changed) => ("updated", "", changed),
        Ended::Skipped(why) => {
            receipt["outcome"] = json!("skipped");
            receipt["reason"] = json!(why);
            ("skipped", "", 0)
        }
        Ended::Cancelled => {
            receipt["outcome"] = json!("cancelled");
            receipt["reason"] = json!("configuration changed");
            ("cancelled", "", 0)
        }
        Ended::Conflict => {
            receipt["outcome"] = json!("refused_owner_changed");
            receipt["error"] = json!("owner_changed_plan");
            ("conflict", "owner_changed_plan", 0)
        }
        Ended::Failed(code) => {
            receipt["outcome"] = json!("failed");
            receipt["error"] = json!(code);
            ("failed", code, 0)
        }
    };
    let took = i64::try_from(started.elapsed().as_millis()).unwrap_or(i64::MAX);
    receipt["duration_ms"] = json!(took);
    receipt["created_at"] = json!(app.clock.now_ms());
    app.keeper.keep_receipt(app, receipt).await;
    Done {
        plan_id: plan_id.to_string(),
        outcome,
        error,
        changed,
    }
}

// ---- the loop ----------------------------------------------------------------------------------

/// An open goal of an agent in a keeper's scope.
struct Open {
    owner: Arc<Member>,
    plan_id: String,
    needs_catch_up: bool,
}

/// The open goals of the agents a keeper with this scope keeps for: durable goals that are
/// active or paused, of agents that are no janitors and were not put away.
fn open_goals(directory: &Directory, scope: &Map<String, Value>, now: i64) -> Vec<Open> {
    let mut open = Vec::new();
    for member in &directory.members {
        let record = &member.record;
        let target = Agent {
            agent_id: record.agent_id.clone(),
            is_janitor: record.is_janitor,
            archived: record.archived_at_ms.is_some(),
            ..Agent::default()
        };
        if !in_scope(scope, &target) {
            continue;
        }
        let view = member.agent.view();
        for plan in view.books.goals.plans() {
            let open_now = matches!(plan.status.as_str(), "active" | "paused");
            let Some(goal) = plan.goal.as_ref().filter(|_| open_now) else {
                continue;
            };
            let read = light(goal);
            open.push(Open {
                owner: member.clone(),
                plan_id: plan.plan_id.clone(),
                needs_catch_up: rules::needs_catch_up(&read, &plan.status, plan.created_at, now),
            });
        }
    }
    open
}

/// One keeper's turn at its goals: which are due, and a run for those that are. Answers
/// whether a run was made.
async fn keep_goals(app: &App, janitor: &str, config: &Value, scope: &Map<String, Value>) -> bool {
    let watch = rules::watch(config);
    if watch.progress_attachment.is_empty() {
        return false;
    }
    let attachment = watch.progress_attachment.as_str();
    let now = app.clock.now_ms();
    // A keeper that was set anew begins its own state anew.
    let generation = config["generation"].clone();
    let fresh = || json!({ "generation": generation });
    let mut progress = app.keeper.progress(app, attachment).await;
    if progress["generation"] != generation {
        progress = fresh();
    }
    let directory = app.fleet.directory();
    let open = open_goals(&directory, scope, now);
    let ids: Vec<String> = open.iter().map(|goal| goal.plan_id.clone()).collect();
    app.keeper.forget_all_but(&ids);
    // Each goal's world is read before the rule looks: the rule does not wait. A goal that
    // waits to be tried again is not looked at.
    let mut worlds: HashMap<&str, Gathered> = HashMap::new();
    let mut listed = Vec::new();
    for goal in &open {
        let plan_id = goal.plan_id.as_str();
        let state = &progress["goals"][plan_id];
        let waits = python::truthy(&state["retry_at"])
            && state["retry_at"].as_i64().is_some_and(|at| now < at);
        let full = goal.needs_catch_up && progress["caught_up"].get(plan_id).is_none();
        if !waits {
            let read = gather(app, &directory, &goal.owner, plan_id, now, (full, false)).await;
            let Some(read) = read else {
                continue;
            };
            worlds.insert(plan_id, read);
        }
        listed.push(rules::Open {
            plan_id,
            needs_catch_up: goal.needs_catch_up,
        });
    }
    let mut ready = {
        let mut look = |plan_id: &str, _: bool| match worlds.get(plan_id) {
            Some(read) => rules::watched(read.activity(), &watch),
            None => rules::Activity::default(),
        };
        let mut stalled = |plan_id: &str| {
            let read = worlds.get(plan_id);
            read.is_some_and(|read| rules::needs_stall_flag(&read.world(), now))
        };
        rules::due(&watch, &mut progress, now, &listed, &mut look, &mut stalled)
    };
    let from_their_start: Vec<String> = ready
        .iter()
        .filter(|plan_id| python::truthy(&progress["goals"][plan_id.as_str()]["catch_up"]))
        .cloned()
        .collect();
    app.keeper.keep_progress(app, attachment, progress).await;
    if ready.is_empty() {
        return false;
    }
    let at_once = config["options"]["max_goals_per_run"].as_i64();
    let at_once = at_once.unwrap_or(GOALS_AT_ONCE).clamp(1, GOALS_MOST);
    ready.truncate(usize::try_from(at_once).unwrap_or(1));

    // One run of the janitor for all of them: the same goals at the same moment are the
    // same run.
    let request = rules::request_id(janitor, generation.as_i64().unwrap_or(0), &ready, now);
    let named = serde_json::to_string(&ready).unwrap_or_default();
    let digest: String = Sha256::digest(named.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let context = json!({ "input_hash": digest, "target_count": ready.len() });
    let begun = app.janitors.with(app, |book, world, now_ms| {
        // The run of this keeper, if it is still on: a request that names no agent would be
        // given to a keeper that watches everyone, and to nobody where this one has a scope.
        let mut on = book.eligible(ROLE, world).into_iter();
        let keeper = on.find(|agent_id| agent_id == janitor);
        let (request, said) = (json!(request), Some(&context));
        let run = book.begin_run_of(keeper, ROLE, &request, said, None, world, now_ms);
        let run = run.ok().flatten()?;
        let run_id = run["run_id"].as_str()?.to_string();
        book.claim_run(&run_id, world, now_ms).then_some(run)
    });
    let Some(begun) = begun.await.ok().flatten() else {
        return false;
    };
    let frozen = |key: &str| {
        let word = begun["configuration"][key].as_str();
        word.unwrap_or_default().to_string()
    };
    let run = Run {
        run_id: begun["run_id"].as_str().unwrap_or_default().to_string(),
        janitor: begun["agent_id"].as_str().unwrap_or(janitor).to_string(),
        tool: frozen("backend"),
        model: frozen("model"),
        effort: frozen("effort"),
    };
    let updates = ready.iter().map(|plan_id| {
        let full = from_their_start.contains(plan_id);
        update(app, &run, plan_id, (now, full))
    });
    let done = futures_util::future::join_all(updates).await;

    // How each ended is written into the keeper's own state, as it is now.
    let mut progress = app.keeper.progress(app, attachment).await;
    if progress["generation"] != generation {
        progress = fresh();
    }
    let settled = app.clock.now_ms();
    for goal in &done {
        rules::settle(
            &mut progress,
            &goal.plan_id,
            goal.outcome,
            settled,
            goal.error,
        );
    }
    app.keeper.keep_progress(app, attachment, progress).await;

    let count = |outcome: &str| done.iter().filter(|goal| goal.outcome == outcome).count();
    let (updated, failed) = (count("updated"), count("failed"));
    let changed: usize = done.iter().map(|goal| goal.changed).sum();
    let mut summary = format!(
        "Updated {updated} of {} goals; {changed} step changes",
        ready.len()
    );
    if failed > 0 {
        summary.push_str(&format!("; {failed} failed"));
    }
    let activity = match (failed == ready.len(), changed > 0 || updated > 0) {
        (true, _) => "failed",
        (false, true) => "worked",
        (false, false) => "idle",
    };
    let result = json!({ "summary": summary, "item_count": updated, "target_count": ready.len() });
    let told = app.janitors.with(app, |book, world, now_ms| {
        let (outcome, error) = match activity {
            "failed" => ("failed", summary.as_str()),
            _ => ("completed", ""),
        };
        let said = Some((activity, summary.as_str()));
        book.complete_run(
            &run.run_id,
            outcome,
            Some(&result),
            error,
            said,
            world,
            now_ms,
        )
    });
    let _ = told.await;
    true
}

/// One pass over the keepers that are on. One run is made at most, as on the Python Host.
async fn pass(app: &App) {
    let Ok(_one) = app.keeper.passing.try_lock() else {
        return;
    };
    let keepers = app.janitors.read(app, |book, world, now_ms| {
        let on = book.eligible(ROLE, world).into_iter();
        let read = on.filter_map(|agent_id| {
            let shown = book.shown(&agent_id, world, now_ms).ok()?;
            let scope = book.configs.get(&agent_id)?.scope.clone();
            Some((agent_id, shown, scope))
        });
        read.collect::<Vec<_>>()
    });
    for (janitor, config, scope) in keepers.await.unwrap_or_default() {
        if keep_goals(app, &janitor, &config, &scope).await {
            return;
        }
    }
}

/// Starts the keeper's loop.
pub fn run(app: &App) {
    let app = app.clone();
    let every = Duration::from_millis(app.config.goals_kept_every_ms.max(50));
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(every).await;
            pass(&app).await;
        }
    });
}

// ---- what is told of the keeper ----------------------------------------------------------------

/// Says on each plan that has a goal how the Goal keeper stands to it (`plan.updater`, the
/// Python Host's `goal_keeper.public_state`): the switch and the last run of the keeper the
/// Host has of its own, this goal's last failed update and when it is tried again, and the
/// newest receipt of the goal.
pub(crate) async fn tell<'a>(app: &App, plans: impl Iterator<Item = &'a mut Value>) {
    let own = app.janitors.read(app, |book, _, _| {
        let agent_id = clarp_kernel::janitors::own_id(ROLE)?;
        let config = book.configs.get(agent_id)?;
        let mut kept_under = book.triggers_of(agent_id).into_iter();
        let kept_under = kept_under.find(|(_, kept)| kept.enabled && kept.trigger_id == TURN_ENDS);
        let last_error = (!config.last_error.is_empty()).then(|| config.last_error.clone());
        let general = json!({
            "enabled": config.enabled, "janitor_agent_id": agent_id,
            "last_run_at": config.last_run_at, "last_error": last_error,
        });
        Some((general, kept_under.map(|(id, _)| id.clone())))
    });
    let own = own.await.ok().flatten();
    let Ok(held) = app.keeper.held(app).await else {
        return;
    };
    let Some(kept) = held.as_ref() else {
        return;
    };
    for plan in plans {
        if !plan.get("updater").is_some_and(Value::is_object) {
            continue;
        }
        let plan_id = plan["plan_id"].as_str().unwrap_or_default().to_string();
        let newest = kept.receipts.get(&plan_id).and_then(|all| all.last());
        let last_receipt = newest.map(|receipt| {
            let shown = SHOWN_OF_A_RECEIPT.iter();
            let shown = shown.map(|key| ((*key).to_string(), receipt[*key].clone()));
            Value::Object(shown.collect())
        });
        let mut told = json!({
            "enabled": false, "janitor_agent_id": null, "last_run_at": null,
            "last_error": null, "goal_error": null, "retry_at": null,
            "last_receipt": last_receipt,
        });
        if let Some((general, kept_under)) = &own {
            for key in ["enabled", "janitor_agent_id", "last_run_at", "last_error"] {
                told[key] = general[key].clone();
            }
            let progress = kept_under.as_ref().and_then(|id| kept.progress.get(id));
            if let Some(goal) = progress.map(|progress| &progress["goals"][plan_id.as_str()]) {
                if python::truthy(&goal["last_error"]) {
                    told["goal_error"] = goal["last_error"].clone();
                }
                told["retry_at"] = goal["retry_at"].clone();
            }
        }
        plan["updater"] = told;
    }
}

/// What the goal tool printed, with the keeper told on every plan in it.
pub(crate) async fn told_in(app: &App, mut said: ToolAnswer) -> ToolAnswer {
    if said.exit != 0 || !said.stdout.contains("\"updater\"") {
        return said;
    }
    let Ok(mut printed) = serde_json::from_str::<Value>(said.stdout.trim()) else {
        return said;
    };
    match &mut printed {
        Value::Array(plans) => tell(app, plans.iter_mut()).await,
        plan => tell(app, std::iter::once(plan)).await,
    }
    said.stdout = format!("{printed}\n");
    said
}

/// `clarp-goal receipts PLAN_ID [LIMIT]`: the receipts of a goal, the newest first, twenty
/// where no number is given and two hundred at most.
pub(crate) async fn receipts_said(app: &App, plan_id: &str, limit: Option<i64>) -> ToolAnswer {
    let most = usize::try_from(limit.unwrap_or(20).clamp(1, 200)).unwrap_or(1);
    let mut found = Vec::new();
    if let Ok(held) = app.keeper.held(app).await
        && let Some(of_the_goal) = held.as_ref().and_then(|kept| kept.receipts.get(plan_id))
    {
        found.extend(of_the_goal.iter().rev().take(most).cloned());
    }
    ToolAnswer::printed(0, Value::Array(found).to_string())
}

/// Puts on each run of a keeper the receipts of the goals it handled (`receipts`, as the
/// Python Host's runs have them; a run of another janitor has none).
pub(crate) async fn with_receipts(app: &App, runs: &mut [Value]) {
    let Ok(held) = app.keeper.held(app).await else {
        return;
    };
    let Some(kept) = held.as_ref() else {
        return;
    };
    for run in runs {
        let mut of_the_run: Vec<&Value> = kept
            .receipts
            .values()
            .flatten()
            .filter(|receipt| receipt["run_id"] == run["run_id"])
            .collect();
        if !of_the_run.is_empty() {
            of_the_run.sort_by_key(|receipt| order(receipt));
            run["receipts"] = json!(of_the_run);
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn row(id: &str, said: &str) -> rules::Row {
        rules::Row {
            message_id: id.to_string(),
            agent_id: "a".to_string(),
            seq: 0,
            role: Some("assistant".to_string()),
            timestamp: None,
            text: Some(said.to_string()),
            tools: Vec::new(),
            updated_at: 1,
            origin: None,
            sender_agent_id: None,
            phase: None,
        }
    }

    #[test]
    fn no_more_is_held_of_an_agent_than_a_prompt_has_room_for() {
        // Thirty rows of 1,500 signs counted each: 45,000. The oldest three can go and what
        // is left still fills a prompt; a fourth could not.
        let long = "x".repeat(4_000);
        let mut rows: Vec<rules::Row> = (0..30).map(|n| row(&format!("m-{n}"), &long)).collect();
        bound(&mut rows);
        assert_eq!(rows.len(), 27);
        assert_eq!(rows[0].message_id, "m-3");
        // What does not fill a prompt is all held.
        let mut few: Vec<rules::Row> = (0..5).map(|n| row(&format!("m-{n}"), "said")).collect();
        bound(&mut few);
        assert_eq!(few.len(), 5);
    }

    #[test]
    fn what_was_done_to_a_goal_is_numbered_from_its_first() {
        let goal = Goal {
            history: vec![
                json!({ "at": 10, "kind": "pause", "revision": 3, "detail": {} }),
                json!({ "at": 20, "kind": "updater", "revision": 3, "detail": {} }),
            ],
            history_offset: 40,
            ..Goal::default()
        };
        let events = events_of(&goal);
        let read: Vec<(i64, i64, &str, &str)> = events
            .iter()
            .map(|event| {
                let (kind, by) = (event.kind.as_str(), event.delegation_id.as_str());
                (event.event_id, event.at, kind, by)
            })
            .collect();
        assert_eq!(read, [(41, 10, "pause", ""), (42, 20, "updater", ROLE)]);
    }
}
