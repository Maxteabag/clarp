//! The "agents working" chip: what a phone shows of the fleet on its lock screen.
//!
//! A phone that shows the fleet's work as a Live Activity leaves the Host a token for it,
//! and the Host keeps the activity current by pushes: how many agents work, how many need
//! the owner, the agent whose turn started last, and up to five rows. This is the Python
//! Host's contract (`docs/live-items.md` §8, `server/lib/live_activity.py`) as rules
//! without a store, a clock or a push service:
//!
//! - what the chip says of a fleet ([`content`]): top-level agents only; a helper's work
//!   and its wait belong to the agent it works under;
//! - when it is said ([`Pacer`]): Apple budgets these pushes, so routine change (a tool
//!   after a tool) goes out at most every fifteen seconds; a turn that starts or ends, or
//!   another lead, at once but ten seconds after the last push at the earliest; and a
//!   rising count of what needs the owner at once and loud;
//! - which activities a phone left ([`Register`]).
//!
//! Held to the Python Host's own module by `tests/fixtures/chip-vectors.json`.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{python, tools};

/// How many rows the chip has.
pub const MOST_ROWS: usize = 5;
/// The least time between two pushes of routine change, and of a change of the chip's shape.
pub const EVERY_MS: i64 = 15_000;
pub const SHAPE_FLOOR_MS: i64 = 10_000;
/// How long a phone may show a state before it says that it is old.
pub const STALE_AFTER_S: i64 = 15 * 60;

const WORKING: [&str; 5] = ["thinking", "responding", "tool", "compacting", "limited"];
const WAITING: &str = "waiting";

/// The name an agent is shown by: a person's name, not the name of a session.
/// `clarp-memory-expert` and `CLARP-MEMORY-EXPERT` read `Clarp Memory Expert`; `ECIT Rachel`
/// stays as it is.
pub fn shown_name(persona: &str) -> String {
    let separated = persona.contains(['-', '_']);
    let shouted = persona.chars().count() > 3
        && persona == persona.to_uppercase()
        && persona != persona.to_lowercase()
        && !persona.contains(' ');
    if !(separated || shouted) {
        return persona.to_string();
    }
    let spaced = persona.replace(['-', '_'], " ");
    let words = spaced.split(' ').filter(|word| !word.is_empty());
    let named: Vec<String> = words
        .map(|word| {
            let mut signs = word.chars();
            let first = signs.next().map(|sign| sign.to_uppercase().to_string());
            first.unwrap_or_default() + &signs.as_str().to_lowercase()
        })
        .collect();
    named.join(" ")
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(yes) => *yes,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(list) => !list.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// A text field as Python makes a text of it (`str(value or "")`).
fn text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other if truthy(other) => python::dumps(other),
        _ => String::new(),
    }
}

/// The tool an activity says is running, when it says so as a record.
fn tool_of(activity: &Value) -> Option<&Value> {
    Some(&activity["tool"]).filter(|tool| tool.is_object())
}

fn tool_headline(tool: &Value) -> String {
    let (name, mut label) = (text(&tool["name"]), text(&tool["label"]));
    let category = match &tool["category"] {
        category if truthy(category) => text(category),
        _ => tools::describe(&name, &json!({})).category.to_string(),
    };
    let of_a_path = matches!(category.as_str(), "read" | "edit" | "write" | "list");
    if of_a_path && label.contains('/') {
        let whole = label.trim_end_matches('/');
        label = whole.rsplit('/').next().unwrap_or(whole).to_string();
    }
    let verb = match category.as_str() {
        "exec" => "Running",
        "read" => "Reading",
        "list" => "Listing",
        "search" => "Searching",
        "edit" => "Editing",
        "write" => "Writing",
        "fetch" => "Fetching",
        "todo" => "Planning",
        "agent" => "Delegating",
        _ => "Calling",
    };
    let what = if label.is_empty() { &name } else { &label };
    if what.is_empty() {
        return "Using a tool".to_string();
    }
    python::strip(&format!("{verb} {what}")).to_string()
}

/// What an agent is doing, in the words the apps use. Never empty.
pub fn headline(activity: &Value) -> String {
    let state = match &activity["state"] {
        state if truthy(state) => text(state),
        _ => "idle".to_string(),
    };
    if state == "tool" {
        return tool_of(activity).map_or_else(|| "Using a tool".to_string(), tool_headline);
    }
    let said = text(&activity["headline"]);
    if !said.is_empty() {
        return said;
    }
    let word = match state.as_str() {
        "thinking" => "Thinking",
        "responding" => "Responding",
        "compacting" => "Compacting",
        "waiting" => "Waiting for you",
        "limited" => "Waiting for the usage limit",
        "interrupted" => "Interrupted",
        "background" => "Background work",
        "idle" => "Idle",
        _ => "Working",
    };
    word.to_string()
}

/// An agent as the chip may show it: who it is, under whom it works, and what its turn
/// shows now, when the stream of turns told.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Shown {
    pub session: String,
    pub persona: String,
    /// The agent it is a helper of, by id or by session; empty for an agent of its own.
    pub helper_of: String,
    pub archived: bool,
    /// One that only tidies up: its work is not "agents working".
    pub janitor: bool,
    /// What its turn shows: the record every `status` of the stream replaces whole.
    pub activity: Value,
}

impl Shown {
    fn listed(&self) -> bool {
        !self.archived && !self.janitor
    }
}

/// One row of the chip.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Row {
    pub agent_id: String,
    pub session: String,
    pub persona: String,
    pub state: String,
    pub headline: String,
    pub tool: Option<String>,
    pub started_at_ms: Option<i64>,
    pub turn_started_ms: Value,
}

impl Row {
    /// A row for an agent with what its turn shows.
    pub fn of(agent_id: &str, agent: &Shown) -> Self {
        let activity = &agent.activity;
        let tool = tool_of(activity);
        let started = [
            tool.map_or(&Value::Null, |tool| &tool["started_at_ms"]),
            &activity["since_ms"],
            &activity["turn_started_ms"],
        ];
        let started = started.into_iter().find(|moment| truthy(moment));
        let name = if agent.persona.is_empty() {
            &agent.session
        } else {
            &agent.persona
        };
        Self {
            agent_id: agent_id.to_string(),
            session: agent.session.clone(),
            persona: shown_name(name),
            state: match &activity["state"] {
                state if truthy(state) => text(state),
                _ => "idle".to_string(),
            },
            headline: headline(activity),
            tool: tool.and_then(|tool| match &tool["name"] {
                Value::Null => None,
                name => Some(text(name)),
            }),
            started_at_ms: started.and_then(whole),
            turn_started_ms: activity["turn_started_ms"].clone(),
        }
    }

    fn works(&self) -> bool {
        WORKING.contains(&self.state.as_str())
    }

    /// The newest work first, and of two that began together the first by name.
    fn newest_first(&self) -> (i64, &str) {
        let began = whole(&self.turn_started_ms)
            .filter(|began| *began != 0)
            .or(self.started_at_ms)
            .unwrap_or(0);
        (-began, &self.persona)
    }

    fn waiting(mut self) -> Self {
        self.state = WAITING.to_string();
        self.headline = "Waiting for you".to_string();
        self.tool = None;
        self.started_at_ms = None;
        self.turn_started_ms = Value::Null;
        self
    }
}

fn whole(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_f64().map(|number| number as i64))
}

/// What the apps' badge counts: requests that wait for the owner, things that block on
/// the owner, failures of what tidies up.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Attention {
    pub count: i64,
    /// The agent each item belongs to, one for each item, empty for an item of nobody's.
    /// `None`: told the old way, as a count and the agents it names.
    pub items: Option<Vec<String>>,
    pub agent_ids: BTreeSet<String>,
}

impl Attention {
    /// One agent for each item of the badge.
    fn each(&self) -> Vec<String> {
        if let Some(items) = &self.items {
            return items.clone();
        }
        let mut named: Vec<String> = self
            .agent_ids
            .iter()
            .filter(|agent| !agent.is_empty())
            .cloned()
            .collect();
        let rest = usize::try_from(self.count).unwrap_or(0);
        let nobody = rest.saturating_sub(named.len());
        named.extend(std::iter::repeat_n(String::new(), nobody));
        named
    }
}

/// The agents of a fleet by their ids, in the order they came.
pub type Fleet = Vec<(String, Shown)>;

fn of<'a>(fleet: &'a [(String, Shown)], agent_id: &str) -> Option<&'a Shown> {
    let mut all = fleet.iter();
    all.find(|(id, _)| id == agent_id).map(|(_, agent)| agent)
}

/// The top-level agent of every agent that may show: itself, or the agent it works under,
/// however many helpers deep. A helper names its parent by id, else by session. One whose
/// parent may not show is its own, and so is every one of helpers that name each other.
fn owners<'a>(listed: &[(&'a str, &'a Shown)]) -> BTreeMap<&'a str, &'a str> {
    let mut alias: BTreeMap<&str, &str> = BTreeMap::new();
    for (agent_id, _) in listed {
        alias.entry(*agent_id).or_insert(*agent_id);
    }
    for (agent_id, agent) in listed {
        if !agent.session.is_empty() {
            alias.entry(agent.session.as_str()).or_insert(*agent_id);
        }
    }
    let agent_of: BTreeMap<&str, &Shown> = listed.iter().copied().collect();
    let parent = |agent_id: &str| -> Option<&'a str> {
        let agent = agent_of.get(agent_id)?;
        if agent.helper_of.is_empty() {
            return None;
        }
        let found = alias.get(agent.helper_of.as_str()).copied()?;
        (found != agent_id).then_some(found)
    };
    let mut owners = BTreeMap::new();
    for (agent_id, _) in listed {
        let mut seen = BTreeSet::from([*agent_id]);
        let mut top = *agent_id;
        while let Some(up) = parent(top) {
            if !seen.insert(up) {
                top = *agent_id;
                break;
            }
            top = up;
        }
        owners.insert(*agent_id, top);
    }
    owners
}

/// What the chip says of a fleet. `seen` are the agents the stream of turns told of, with
/// what their turns show; `roster` is what the Host knows of every agent (one that was
/// seen and is not in it is taken as the stream knew it). Top-level agents only: a helper
/// rolls up into the agent it works under. What needs the owner counts each such agent once
/// that has a badge item or waits, itself or a helper under it, and each badge item of
/// nobody on the chip.
pub fn content(
    seen: &[(String, Shown)],
    roster: &[(String, Shown)],
    attention: &Attention,
    now_ms: i64,
) -> Value {
    let mut all: Vec<(&str, &Shown)> = roster
        .iter()
        .map(|(agent_id, agent)| (agent_id.as_str(), agent))
        .collect();
    for (agent_id, agent) in seen {
        if of(roster, agent_id).is_none() {
            all.push((agent_id, agent));
        }
    }
    let listed: Vec<(&str, &Shown)> = all
        .into_iter()
        .filter(|(_, agent)| agent.listed())
        .collect();
    let is_listed = |agent_id: &str| listed.iter().any(|(id, _)| *id == agent_id);
    let owners = owners(&listed);
    let items = attention.each();
    let flagged: BTreeSet<&str> = items
        .iter()
        .map(String::as_str)
        .filter(|agent| is_listed(agent))
        .collect();
    let other = items.iter().filter(|agent| !is_listed(agent)).count();
    let row_of = |agent_id: &str| -> Row {
        match (
            of(seen, agent_id),
            listed.iter().find(|(id, _)| *id == agent_id),
        ) {
            (Some(agent), _) => Row::of(agent_id, agent),
            (None, Some((_, known))) => {
                let quiet = Shown {
                    session: known.session.clone(),
                    persona: known.persona.clone(),
                    ..Shown::default()
                };
                Row::of(agent_id, &quiet)
            }
            (None, None) => Row::of(agent_id, &Shown::default()),
        }
    };
    // Each top-level agent with the agents under it that the stream told of or the badge
    // names, in the order the agents came.
    let mut teams: Vec<(&str, Vec<&str>)> = Vec::new();
    for (agent_id, _) in &listed {
        if of(seen, agent_id).is_none() && !flagged.contains(agent_id) {
            continue;
        }
        let owner = owners.get(agent_id).copied().unwrap_or(*agent_id);
        match teams.iter_mut().find(|(top, _)| *top == owner) {
            Some((_, members)) => members.push(*agent_id),
            None => teams.push((owner, vec![*agent_id])),
        }
    }
    let mut tops: Vec<(Row, bool)> = Vec::new();
    for (owner, members) in &teams {
        let mut top = row_of(owner);
        let under: Vec<Row> = members
            .iter()
            .filter(|member| *member != owner)
            .map(|member| row_of(member))
            .collect();
        let needs = members.iter().any(|member| flagged.contains(member))
            || under.iter().any(|row| row.state == WAITING)
            || top.state == WAITING;
        let mut busy: Vec<&Row> = under.iter().filter(|row| row.works()).collect();
        busy.sort_by(|a, b| a.newest_first().cmp(&b.newest_first()));
        if let (false, Some(newest)) = (top.works(), busy.first()) {
            top.state.clone_from(&newest.state);
            top.tool = None;
            top.headline = match busy.len() {
                1 => "1 sub-agent working".to_string(),
                several => format!("{several} sub-agents working"),
            };
            top.started_at_ms = newest.started_at_ms;
            top.turn_started_ms = newest.turn_started_ms.clone();
        }
        tops.push((top, needs));
    }
    let needing = tops.iter().filter(|(_, needs)| *needs).count();
    let mut working: Vec<Row> = tops
        .iter()
        .filter(|(top, _)| top.works())
        .map(|(top, _)| top.clone())
        .collect();
    working.sort_by(|a, b| a.newest_first().cmp(&b.newest_first()));
    let mut waiting: Vec<Row> = tops
        .into_iter()
        .filter(|(top, needs)| *needs && !top.works())
        .map(|(top, _)| top)
        .collect();
    waiting.sort_by(|a, b| a.newest_first().cmp(&b.newest_first()));
    let lead = working.first().cloned();
    let rows: Vec<Row> = waiting
        .into_iter()
        .map(Row::waiting)
        .chain(working.iter().cloned())
        .take(MOST_ROWS)
        .collect();
    json!({
        "working": working.len(), "needs_you": other + needing, "decisions": items.len(),
        "lead": lead, "agents": rows, "updated_at_ms": now_ms,
    })
}

/// The push a phone's activity is sent for a state of the chip.
pub fn payload(state: &Value, now_ms: i64) -> Value {
    let now = now_ms.div_euclid(1000);
    json!({
        "aps": {
            "timestamp": now, "event": "update", "content-state": state,
            "stale-date": now + STALE_AFTER_S,
        },
    })
}

/// A state of the chip that is to be pushed now.
#[derive(Debug, Clone, PartialEq)]
pub struct Push {
    pub state: Value,
    /// `10` when more needs the owner than the phone was told, else `5`.
    pub priority: &'static str,
    /// The chip changed its shape, or more needs the owner: not routine.
    pub high: bool,
}

/// What of a state decides whether it is told again: its shape (how many work, how many
/// need the owner, who leads), and what the lead does and who shows in which state.
#[derive(Debug, Clone, PartialEq)]
struct Told {
    shape: (u64, u64, String),
    lead: (Value, Value),
    rows: Vec<(String, String)>,
}

/// Watches what the turns of a fleet show, and says when the chip is to be pushed.
#[derive(Debug, Clone, Default)]
pub struct Pacer {
    /// The agents the stream of turns told of, in the order they were first seen.
    seen: Fleet,
    /// Those whose work is not shown: they are not looked at again.
    quiet: BTreeSet<String>,
    attention: Attention,
    told: Option<Told>,
    told_needs: u64,
    last_ms: i64,
    /// When what was held back is to be looked at again.
    wake_ms: Option<i64>,
}

impl Pacer {
    /// When the pacer is to be woken ([`Pacer::woken`]): a change was held back until then.
    pub fn wake_at(&self) -> Option<i64> {
        self.wake_ms
    }

    /// The chip as it is now.
    pub fn now(&self, roster: &[(String, Shown)], now_ms: i64) -> Value {
        content(&self.seen, roster, &self.attention, now_ms)
    }

    /// An event of the stream of turns. Only what tells an agent's status is looked at.
    /// `roster` is what the Host knows of every agent now.
    pub fn observe(
        &mut self,
        event: &Value,
        roster: &[(String, Shown)],
        now_ms: i64,
    ) -> Option<Push> {
        if event["type"] != "live" {
            return None;
        }
        let ops = event["ops"].as_array()?;
        let status = ops.iter().rfind(|op| op["op"] == "status")?;
        let agent_id = text(&event["agent_id"]);
        if self.quiet.contains(&agent_id) {
            return None;
        }
        let known = of(roster, &agent_id);
        if known.is_some_and(|agent| agent.janitor) {
            self.quiet.insert(agent_id);
            return None;
        }
        let activity = match &status["activity"] {
            activity if truthy(activity) => activity.clone(),
            _ => json!({}),
        };
        let agent = Shown {
            session: text(&event["session"]),
            persona: known.map_or_else(String::new, |agent| agent.persona.clone()),
            helper_of: known.map_or_else(String::new, |agent| agent.helper_of.clone()),
            archived: known.is_some_and(|agent| agent.archived),
            janitor: false,
            activity,
        };
        match self.seen.iter_mut().find(|(id, _)| *id == agent_id) {
            Some((_, seen)) => *seen = agent,
            None => self.seen.push((agent_id, agent)),
        }
        self.consider(roster, now_ms)
    }

    /// The badge changed. Before anybody was seen working and before anything was pushed
    /// it is kept and tells nothing by itself.
    pub fn attention_is(
        &mut self,
        attention: Attention,
        roster: &[(String, Shown)],
        now_ms: i64,
    ) -> Option<Push> {
        self.attention = Attention {
            count: attention.count.max(0),
            ..attention
        };
        if self.seen.is_empty() && self.told.is_none() {
            return None;
        }
        self.consider(roster, now_ms)
    }

    /// The time the pacer asked to be woken at has come.
    pub fn woken(&mut self, roster: &[(String, Shown)], now_ms: i64) -> Option<Push> {
        self.wake_ms = None;
        self.consider(roster, now_ms)
    }

    fn consider(&mut self, roster: &[(String, Shown)], now_ms: i64) -> Option<Push> {
        let state = content(&self.seen, roster, &self.attention, now_ms);
        let lead = &state["lead"];
        let (working, needs) = (
            state["working"].as_u64().unwrap_or(0),
            state["needs_you"].as_u64().unwrap_or(0),
        );
        let rows = state["agents"].as_array().into_iter().flatten();
        let now = Told {
            shape: (working, needs, text(&lead["agent_id"])),
            lead: (lead["headline"].clone(), lead["tool"].clone()),
            rows: rows
                .map(|row| (text(&row["agent_id"]), text(&row["state"])))
                .collect(),
        };
        if self.told.as_ref() == Some(&now) {
            return None;
        }
        let urgent = needs > self.told_needs;
        let reshaped = self
            .told
            .as_ref()
            .is_none_or(|told| told.shape != now.shape);
        // A change of shape waits ten seconds after the last push, routine change fifteen:
        // either way the latest state goes out when the time has come.
        let floor = if reshaped {
            SHAPE_FLOOR_MS.min(EVERY_MS)
        } else {
            EVERY_MS
        };
        if urgent || now_ms - self.last_ms >= floor {
            self.wake_ms = None;
            self.told = Some(now);
            self.told_needs = needs;
            self.last_ms = now_ms;
            return Some(Push {
                state,
                priority: if urgent { "10" } else { "5" },
                high: urgent || reshaped,
            });
        }
        let due = self.last_ms + floor;
        if self.wake_ms.is_none_or(|wake| due < wake) {
            self.wake_ms = Some(due);
        }
        None
    }
}

// ---- the chip's share of the push service's budget -------------------------------------------

/// How many pushes of the chip may go at once, how long one more takes to come back, and
/// how many may go in a day. The push service counts the chip's pushes and the alerts
/// against one window for the whole Host: with these, alerts always keep the larger part.
pub const BURST: f64 = 10.0;
pub const REFILL_S: f64 = 86_400.0 / 300.0;
pub const DAY_MOST: i64 = 300;
/// How long the chip leaves a push service alone that said "too many": from a minute,
/// doubling each time it says so again, to half an hour; a delivered push begins anew.
const FLOOR_LEAST_S: f64 = 60.0;
const FLOOR_MOST_S: f64 = 1800.0;
const DAY_S: f64 = 86_400.0;
/// The last second a reading of the clock may name: the end of the year 9999.
const LAST_WALL_S: f64 = 253_402_300_799.0;

fn a_reading(wall: f64) -> bool {
    wall.is_finite() && (0.0..=LAST_WALL_S).contains(&wall)
}

/// How much of a day's budget is spent, as it is kept: the day (counted from 1970), the
/// pushes that went in it, until when the push service asked to be left alone, and the
/// reading of the clock it was last written at.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Day {
    pub count: i64,
    pub day: i64,
    pub last_wall: f64,
    pub paused_until: f64,
    pub version: i64,
}

impl Day {
    /// Whether what was kept can be believed at this reading of the clock. A clock that
    /// went back behind the last writing is not believed: nothing is spent until it is past.
    fn sound(&self, now: f64) -> bool {
        self.version == 1
            && self.day >= 0
            && (0..=DAY_MOST).contains(&self.count)
            && a_reading(self.last_wall)
            && a_reading(self.paused_until)
            && self.day == (self.last_wall / DAY_S).floor() as i64
            && self.last_wall <= now
            && !(self.paused_until != 0.0 && self.paused_until < self.day as f64 * DAY_S)
    }
}

/// What is kept of a day's budget: nothing yet, a day, or something that cannot be read.
/// What cannot be read spends nothing: no push goes on a budget nobody can count.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum Spent {
    #[default]
    Nothing,
    Of(Day),
    Unreadable,
}

impl Spent {
    /// From what is kept, as it was written.
    pub fn kept(raw: Option<&[u8]>) -> Self {
        match raw {
            None => Self::Nothing,
            Some(raw) if raw.len() > 1024 => Self::Unreadable,
            Some(raw) => serde_json::from_slice(raw).map_or(Self::Unreadable, Self::Of),
        }
    }

    /// The day, to be kept.
    pub fn day(&self) -> Option<&Day> {
        match self {
            Self::Of(day) => Some(day),
            _ => None,
        }
    }

    /// One push is asked for (`reserve`), or the push service asked to be left alone until
    /// `pause_until`, or both. Answers whether the push may go, and how long to wait.
    /// `Err`: what is kept cannot be believed now, and nothing was written.
    fn update(&mut self, reserve: bool, pause_until: f64, now: f64) -> Result<(bool, f64), ()> {
        if !a_reading(pause_until) || !a_reading(now) {
            return Err(());
        }
        let today = (now / DAY_S).floor() as i64;
        let mut day = match self {
            Self::Nothing => Day {
                count: 0,
                day: today,
                last_wall: now,
                paused_until: 0.0,
                version: 1,
            },
            Self::Of(day) if day.sound(now) => day.clone(),
            _ => return Err(()),
        };
        if day.day < today {
            day.day = today;
            day.count = 0;
            if day.paused_until <= now {
                day.paused_until = 0.0;
            }
        }
        day.last_wall = now;
        day.paused_until = day.paused_until.max(pause_until);
        let allowed = reserve && day.paused_until <= now && day.count < DAY_MOST;
        if allowed {
            day.count += 1;
        }
        let mut wait = (day.paused_until - now).max(0.0);
        if reserve && day.count >= DAY_MOST && !allowed {
            wait = wait.max((today + 1) as f64 * DAY_S - now);
        }
        *self = Self::Of(day);
        Ok((allowed, wait))
    }
}

/// The gate the chip's pushes go through. What is held back is the newest thing for each
/// token, in the order the tokens came to wait; it goes when the budget allows, and the
/// gate says when to look again ([`Budget::wake_at`]). `T` is what a push carries.
///
/// The clock is read in seconds, as the push service counts.
#[derive(Debug, Clone)]
pub struct Budget<T> {
    /// How many pushes may go at once, and when that was reckoned. `None`: all of them.
    level: Option<(f64, f64)>,
    waiting: Vec<(String, T)>,
    paused_until: f64,
    /// A pause the day's budget has not been told of yet.
    pause_to_keep: f64,
    floor: f64,
    wake: Option<f64>,
    spent: Spent,
}

impl<T: Clone> Budget<T> {
    /// A gate, with what was kept of the day's budget.
    pub fn new(spent: Spent) -> Self {
        Self {
            level: None,
            waiting: Vec::new(),
            paused_until: 0.0,
            pause_to_keep: 0.0,
            floor: 0.0,
            wake: None,
            spent,
        }
    }

    /// What is kept of the day's budget.
    pub fn spent(&self) -> &Spent {
        &self.spent
    }

    /// The tokens that wait, in their order.
    pub fn waiting(&self) -> impl Iterator<Item = &str> {
        self.waiting.iter().map(|(token, _)| token.as_str())
    }

    /// How long the push service is left alone after it next says "too many", as it stands.
    pub fn floor_s(&self) -> f64 {
        self.floor
    }

    /// When the gate is to be looked at again ([`Budget::woken`], then [`Budget::due`]).
    pub fn wake_at(&self) -> Option<f64> {
        self.wake
    }

    /// The time the gate asked to be looked at again has come.
    pub fn woken(&mut self) {
        self.wake = None;
    }

    fn refilled(&self, now: f64) -> f64 {
        match self.level {
            None => BURST,
            Some((level, at)) => BURST.min(level + (now - at) / REFILL_S),
        }
    }

    /// The newest thing for every token there is. Tokens that still wait keep their places;
    /// one that was just sent joins at the back, so none starves; one that is no more drops out.
    pub fn offer(&mut self, tokens: &[String], what: &T) {
        let mut waiting: Vec<(String, T)> = self
            .waiting
            .iter()
            .filter(|(token, _)| tokens.contains(token))
            .map(|(token, _)| (token.clone(), what.clone()))
            .collect();
        for token in tokens {
            if !waiting.iter().any(|(known, _)| known == token) {
                waiting.push((token.clone(), what.clone()));
            }
        }
        self.waiting = waiting;
    }

    fn arm(&mut self, now: f64) {
        if self.waiting.is_empty() || self.wake.is_some() {
            return;
        }
        let level = self.refilled(now);
        let wait = (self.paused_until - now)
            .max((1.0 - level) * REFILL_S)
            .max(0.0);
        self.wake = Some(now + wait);
    }

    /// What may go now, taken from what waits. For the rest the gate asks to be looked at
    /// again.
    pub fn due(&mut self, now: f64) -> Vec<(String, T)> {
        let mut ready = Vec::new();
        while !self.waiting.is_empty() {
            let level = self.refilled(now);
            if now < self.paused_until || level < 1.0 {
                break;
            }
            self.level = Some((level - 1.0, now));
            ready.push(self.waiting.remove(0));
        }
        self.arm(now);
        ready
    }

    /// Whether the push service is being left alone now.
    pub fn paused(&self, now: f64) -> bool {
        now < self.paused_until
    }

    /// One push is about to go: whether the day's budget has it. `changed` is told when
    /// what is kept of the day is to be kept again.
    pub fn reserve(&mut self, now: f64, changed: &mut bool) -> bool {
        let told = self.pause_to_keep;
        let (allowed, wait) = match self.spent.update(true, told, now) {
            Ok(answer) => {
                *changed = true;
                if self.pause_to_keep <= told {
                    self.pause_to_keep = 0.0;
                }
                answer
            }
            // Nothing that can be believed is kept: no push, and another look in a minute.
            Err(()) => (false, 60.0),
        };
        if !allowed {
            self.paused_until = self.paused_until.max(now + wait.max(1.0));
        }
        allowed
    }

    /// A push went through: the push service is not angry any more.
    pub fn delivered(&mut self) {
        self.floor = 0.0;
    }

    /// The push service said "too many" (to the chip or to an alert) and to come back after
    /// `retry_after` seconds. `whole_day`: it is the day's window that is spent, so it is
    /// left alone until the day is over at the least. Never sooner than it asked.
    pub fn rate_limited(
        &mut self,
        retry_after: f64,
        whole_day: bool,
        now: f64,
        changed: &mut bool,
    ) {
        let mut wait = if retry_after.is_finite() && retry_after > 0.0 {
            retry_after
        } else {
            0.0
        };
        if whole_day {
            wait = wait.max(DAY_S - now.rem_euclid(DAY_S));
        }
        self.floor = if self.floor == 0.0 {
            FLOOR_LEAST_S
        } else {
            (self.floor * 2.0).min(FLOOR_MOST_S)
        };
        self.paused_until = self.paused_until.max(now + wait.max(self.floor));
        if whole_day {
            self.pause_to_keep = self.pause_to_keep.max(now + wait);
            let told = self.pause_to_keep;
            if self.spent.update(false, told, now).is_ok() {
                *changed = true;
                if self.pause_to_keep <= told {
                    self.pause_to_keep = 0.0;
                }
            }
        }
    }

    /// What was taken and did not go is put back (a newer thing for its token stays), and
    /// its place in the budget with it.
    pub fn requeue(&mut self, items: Vec<(String, T)>, now: f64) {
        let back = items.len() as f64;
        for (token, what) in items {
            if !self.waiting.iter().any(|(known, _)| *known == token) {
                self.waiting.push((token, what));
            }
        }
        if let Some((level, at)) = self.level {
            self.level = Some((BURST.min(level + back), at));
        }
        self.arm(now);
    }
}

/// An activity a phone left: the token its pushes go to.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Activity {
    pub token: String,
    pub activity_id: String,
    pub kind: String,
    #[serde(default)]
    pub environment: String,
    /// The paired phone that left it; empty when nobody's. A phone that was taken off the
    /// Host gets no more pushes for it.
    #[serde(default)]
    pub principal: String,
    #[serde(default)]
    pub registered_at: i64,
}

/// What a phone says to leave an activity.
#[derive(Debug, Clone, Copy, Default)]
pub struct Left<'a> {
    pub token: &'a str,
    pub activity_id: &'a str,
    pub kind: &'a str,
    pub environment: &'a str,
    pub principal: &'a str,
}

/// The activities phones left, each under its id.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Register {
    activities: BTreeMap<String, Activity>,
}

impl Register {
    /// From what is kept.
    pub fn kept(activities: Vec<Activity>) -> Self {
        let by_id = activities
            .into_iter()
            .filter(|activity| !activity.token.is_empty())
            .map(|activity| (activity.activity_id.clone(), activity));
        Self {
            activities: by_id.collect(),
        }
    }

    /// The activities, by their ids.
    pub fn activities(&self) -> impl Iterator<Item = &Activity> {
        self.activities.values()
    }

    /// A phone leaves an activity, or the token an activity it left has now: the same id
    /// takes the place of what was there. `Err` says why not, in the Python Host's words.
    pub fn register(&mut self, left: &Left, now_ms: i64) -> Result<&Activity, &'static str> {
        let token = python::strip(left.token).to_lowercase();
        let hex = token
            .chars()
            .all(|sign| sign.is_ascii_digit() || ('a'..='f').contains(&sign));
        if token.is_empty() || !hex || token.chars().count() > 400 {
            return Err("token must be a hex ActivityKit push token");
        }
        if !matches!(left.kind, "" | "agents-working" | "agents") {
            return Err("kind must be agents-working");
        }
        if !matches!(
            left.environment,
            "" | "sandbox" | "production" | "development"
        ) {
            return Err("environment must be sandbox or production");
        }
        let named: String = python::strip(left.activity_id).chars().take(128).collect();
        let activity_id = if named.is_empty() {
            token.chars().take(32).collect()
        } else {
            named
        };
        let before = self.activities.get(&activity_id);
        let environment = match left.environment {
            "" => before.map_or_else(String::new, |before| before.environment.clone()),
            given => given.to_string(),
        };
        let activity = Activity {
            token,
            activity_id: activity_id.clone(),
            kind: "agents-working".to_string(),
            environment,
            principal: left.principal.to_string(),
            registered_at: now_ms,
        };
        self.activities.insert(activity_id.clone(), activity);
        self.activities
            .get(&activity_id)
            .ok_or("token must be a hex ActivityKit push token")
    }

    /// The activity of this id, and every activity with this token, are a phone's no more.
    /// Answers how many were taken off.
    pub fn unregister(&mut self, activity_id: &str, token: &str) -> usize {
        let token = python::strip(token).to_lowercase();
        let before = self.activities.len();
        self.activities.retain(|_, activity| {
            let by_id = !activity_id.is_empty() && activity.activity_id == activity_id;
            let by_token = !token.is_empty() && activity.token == token;
            !(by_id || by_token)
        });
        before - self.activities.len()
    }
}
